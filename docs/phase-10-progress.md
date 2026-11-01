# Phase 10 Progress Log: D2C Operations AI

Scope decision (stated to the user before starting): depth-first vertical slice. The full
operations engine (issues, tasks, priority, SLA, approvals, audit, AI, automation bridge, demo
seed, dashboard) is built for real; ORDER + SHIPPING/NDR/RTO + INVENTORY are the fully-detected
categories; PAYMENT/RETURN/REFUND/EXCHANGE/FRAUD are covered on top of existing data (no new
tables where Shopify fields already answer it) rather than built as deeply. See
`docs/operations-architecture.md` "Known limitations" for the exact cut lines.

## Steps completed (brief section 40's order)

1. **Repository audit** -- read `db/models/*`, `db/base.py`, `db/scoping.py`,
   `services/automation.py`, `services/support/{identity,timeline,conversations}.py`,
   `services/ai/{provider,factory}.py`, `api/deps.py`, `api/automations.py`,
   `main.py`, migration `0010`, frontend `lib/{dal,api,form,types,format}.ts`, `components/
   {app-header,status-badge,priority-badge}.tsx`, `app/support/*` -- to confirm reuse points
   before writing anything.
2. **Architecture documented** -- `docs/operations-architecture.md` written alongside the models
   (reuse-vs-new-table decisions recorded as they were made, not after).
3. **Database/model foundation** -- `db/models/operations.py` (Shipment, ReturnCase,
   OperationalSLA/Issue/Task/Approval/Action/AuditEvent), migration `0011_operations.py`.
   Verified: SQLite `Base.metadata.create_all` clean; real Postgres upgrade/downgrade/upgrade
   roundtrip; `alembic check` clean (one real drift found and fixed -- three FK columns missing
   `index=True` in the model vs. the migration's explicit indexes).
4. **Issue/task engine** -- `issues.py`, `tasks.py` (race-safe `claim_task` via atomic
   `UPDATE ... WHERE status=OPEN AND assigned_user_id IS NULL`).
5. **Detectors** -- `detectors.py`, full depth for ORDER/SHIPPING/NDR/RTO/INVENTORY, lighter for
   PAYMENT (reuses `Order.financial_status`) and RETURN/REFUND/EXCHANGE (new lean `ReturnCase`
   table). `DETECTOR_REQUIREMENTS` declares each detector's required columns per brief section 4.
6. **Priority + SLA** -- `priorities.py` (explainable point-based scoring), `sla.py` (catalog +
   evaluator, auto-seeded on first use so ordering never matters regardless of caller).
7. **Operations dashboard + API** -- `api/operations/router.py` (28 routes), registered in
   `main.py`.
8. **Support -> Operations bridge** -- `bridge.py`, pushes onto the existing Phase 9
   `SupportTimelineEvent` ledger (added 4 new event types there).
9. **Operations AI** -- `ai.py`. Mid-build correction: the headline `summary` was originally LLM-
   generated; discovered via live-testing that the repo's offline `fake` LLM provider can't answer
   a non-chat-shaped prompt usefully, so moved the trustworthy fact into a deterministic
   `_deterministic_summary` and demoted the model's output to `reasoning_summary`. Documented in
   `docs/operations-ai.md`.
10. **Safe action + approval system** -- `actions.py`, `approvals.py`.
11. **Automation integration** -- extended `services/automation.py`: 8 new `EVENT_TYPES`
    (`order_needs_attention`, `shipment_delayed`, `ndr_detected`, `rto_detected`, `inventory_low`,
    `refund_pending`, `sla_breached`, `operational_task_created`), one new `ACTION_TYPES`
    (`create_issue`) with its executor branch in `run_execution`. Minimal, additive diff to a
    security/crash-recovery-sensitive file -- no existing branch touched.
12. **Analytics** -- `analytics.py`, every number a real aggregate query.
13. **Demo seed** -- `backend/tools/seed_operations_demo.py`, idempotent, synthetic Shopify-id
    range, runs the real detection sweep at the end.
14. (seed covered above)
15. **Mutation testing** -- `backend/mutation/catalog_10.py`, 12 mutants covering tenant scoping,
    authorization, priority, SLA, approval, the destructive action gate (x2), issue state
    transitions, task claim (x2), AI grounding, AI tenant isolation. **All 12 killed, zero
    survivors.** (In passing: fixed a stale, pre-existing Phase 9 catalog entry (`p9-08`) that was
    blocking the mutation runner's `check()` gate for everyone -- a whitespace-only drift from a
    later reformat, unrelated to Phase 10 logic.)
16. **Tests** -- `tests/operations/` (detectors, priorities, SLA, issues, tasks, approvals/
    actions, AI, API) and additions to `tests/test_tenant_api_matrix.py` (28 new routes classified
    in the policy table, exercised by its existing generic auth/role/tenant-isolation test suite).
17. **Frontend** -- `/operations`, `/operations/issues/[id]`, `/operations/tasks`,
    `/operations/orders`, `/operations/inventory`, `/operations/shipping`, `/operations/payments`,
    `/operations/analytics`, `/operations/ai`; nav link added; `StatusBadge` extended with the new
    status vocabulary (collision-checked against existing WhatsApp-confidence `HIGH`/`LOW` --
    reused the existing `PriorityBadge` component instead of overloading `StatusBadge` for
    priority). Typecheck/lint/build all clean.
18. **Real end-to-end verification** -- Docker/Postgres/Redis brought up, real `alembic upgrade`
    against the dev DB, real `uvicorn` + real worker process + real `next dev`, a real user/org
    created through the live API, demo data seeded for real, then exercised live: dashboard render
    (HTML contains the real seeded issues), issue resolve/reopen, task create/claim, the full
    REQUIRES_APPROVAL -> 409-before-approval -> approve -> execute pipeline, and the AI query
    endpoint -- all through real HTTP against real Postgres. Logs scanned for secrets/PII (clean).
    Demo org/user deleted afterward; dev DB confirmed empty again.
    **Limitation**: the Claude-in-Chrome browser extension was not connected in this environment,
    so this was HTTP/API-level end-to-end verification (including fetching the actual
    server-rendered Next.js HTML), not a mouse-driven browser session.

## Remaining before final sign-off (resolved -- see docs/phase-10-final-report.md)

- Full `pytest -m "not integration"` -- run to completion: 3664 passed, 16 skipped, 1 unrelated
  pre-existing failure in `tests/shopify/test_log_audit.py` (confirmed via `git diff --stat` to be
  outside Phase 10's diff, reproduces in isolation, not caused by this phase).
- Full `pytest -m integration` -- run to completion twice: first run surfaced 3 real Phase-10-
  caused failures (a missing FK index on `operational_tasks.created_by_user_id`, and two stale
  "0010 is head" assertions that needed bumping to "0011"); fixed, re-ran clean: 141 passed,
  1 skipped, 0 failed.
- `ruff check`/`ruff format`/`mypy` -- all run against the new Phase 10 files; fixed every finding
  (line-length via `ruff format`, two unused-variable/dead-code lints, 5 genuine Optional-narrowing
  mypy errors in `detectors.py`); all three clean on the final run.
- Mutation catalog drift -- `ruff format` rewrapped lines in `issues.py`/`repository.py`/`ai.py`
  that 4 of the 12 Phase 10 mutant snippets matched verbatim; updated all 4 to the reformatted
  source; `mutation.run check` reports all 250 snippets (every phase) unique; re-ran the full
  Phase 10 mutant set: 12/12 killed.
- Frontend typecheck/lint/build -- re-confirmed clean after all backend changes.
- Playwright `e2e/operations.spec.js` -- **NOT written** (deliberate scope decision after an
  advisor consult, given how much real verification was already banked); existing Phase 0-9
  suites were not re-run either. This is the one gate that keeps the final verdict at INCOMPLETE
  rather than COMPLETE/PASS -- see the final report's verdict table.
- `docs/phase-10-final-report.md` -- written, with the honest INCOMPLETE verdict above.

## Round 2: Platform Admin + Brand Management + Demo Environments

A second brief extended Phase 10 with a platform-admin layer on top of the Operations work above:
founder-level login, brand (organization) management, view-as-brand, 4 real demo brands, and --
this time -- an explicit requirement that Playwright E2E actually be green for a PASS verdict
(closing exactly the gate that kept round 1 at INCOMPLETE).

**Design decisions** (see `docs/platform-admin.md` for the full writeup):
- No second auth system: a platform admin is a `User` row with `is_platform_admin=True`, never an
  overloaded `OrganizationRole.OWNER`. Same login endpoint, same JWT, same session cookie.
- View-as-brand gets its own org-context resolver (`resolve_platform_admin_org_context`), never a
  bolt-on to the existing membership-based one -- so every existing org-scoped route works
  unchanged under admin view, with zero changes to those routers.
- `platform_audit_events` is a new table, not a repurposing of `OperationalAuditEvent` (different
  concern: brand access/identity, not operational resources).

**Implementation**: migration `0012` (users.is_platform_admin, organizations.environment/
is_active, platform_audit_events), `services/platform/service.py` (brand CRUD, dashboard KPIs,
audit), `api/platform/router.py` (9 routes), `tools/seed_platform_admin.py` +
`tools/seed_demo_platform.py` (reuses `seed_operations_demo.seed()` verbatim per brand), frontend
`/admin/*` (login, dashboard, brands, audit) + the admin-view banner wired into the root layout.

**Bugs found and fixed during this round's own verification** (not pre-existing):
1. **Concurrent approval race**: `approvals.approve`/`reject` were read-then-write, not atomic --
   two admins approving the same action at once could both succeed, the second silently
   overwriting the first. Fixed with the same atomic `UPDATE...WHERE status=PENDING` pattern as
   `tasks.claim_task`. Also fixed a latent gap this exposed: `ApprovalNotPending` had no catch
   anywhere, so even "approve something already decided" would have been an unhandled 500, not a
   clean 409.
2. **`platform_audit_events.actor_user_id` was NOT NULL with `ondelete=SET NULL`** -- a
   contradiction that only breaks when a referenced user is actually deleted (Postgres raises
   `NotNullViolation` instead of nulling the column). Caught live while cleaning up a throwaway
   test platform-admin account after verification. Fixed by making the column nullable, matching
   `organization_id`'s existing pattern (the audit trail must outlive what it references).
3. **Enum length drift**: migration's `organization_environment` enum defaulted to length 24
   while the model declared 20 explicitly -- `alembic check` caught it; fixed to match.
4. Stale `IssueCategory`/`IssuePriority`/`IssueSeverity`/`IssueStatus` exports missing from
   `app/db/models/__init__.py`'s `__all__`, and two unsorted import blocks -- pre-existing from
   the original Phase 10 pass (never caught because that pass only ever ran `ruff` against
   targeted path lists, never a bare `ruff check .` across the whole backend); fixed in passing.

**Process note, disclosed rather than hidden**: a background mutation run was killed mid-mutant
while a different fix was in progress, leaving `app/services/operations/ai.py` sitting mutated on
disk for a short window. Caught via 3 unrelated-looking test failures, root-caused, fixed, and
confirmed via a full programmatic sweep of every mutant's patched text against the whole `app/`
tree that nothing else was left mutated. Lesson applied for the rest of the session: never edit a
file while a mutation run that touches it is active in the background.

**Verification, final numbers** (all re-run after every fix above, not just once):
- Backend unit tier: 3739 passed, 17 skipped, 1 pre-existing unrelated failure (unchanged from
  round 1, reconfirmed unrelated).
- Integration tier: 141 passed, 1 skipped, 0 failed.
- Mutation: 18/18 killed (12 op- + 6 new plat-), zero survivors, `app/` tree verified identical.
- `e2e/platform-admin.spec.js` (new, 20 checks) and `e2e/operations.spec.js` (new, 20 checks) --
  both written for real and passing in full.
- Full Phase 0-9 Playwright regression (`node run.mjs`, all 9 offline suites) -- all green, run
  twice (before and after the concurrency/nullable-column fixes), identical all-pass both times.
- Frontend typecheck/lint/build clean; 39 routes including the 4 new `/admin/*` ones.
- Live HTTP verification: platform admin login/dashboard/brand CRUD/enter-exit-switch/
  cross-brand-isolation/audit trail, plus a freshly-created brand owner logging in through the
  normal flow and seeing only their own org.
- 4 demo brands live-seeded, idempotency verified twice in a row, `--reset-demo`'s
  PRODUCTION-refusal safety verified live.

**Known, deliberately scoped gap**: no dedicated brand-side Team/Settings frontend page was
built this round (the underlying `/api/organizations/{id}/members` API already existed from an
earlier phase and is already tested). See the final report's KNOWN LIMITATIONS section.

Final verdict: **PHASE 10: COMPLETE/PASS** -- see `docs/phase-10-final-report.md`.
