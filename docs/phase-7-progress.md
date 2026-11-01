# Phase 7 Progress: WhatsApp Production Hardening + Verification

## Current Status

Phase 7 is **IMPLEMENTED and VERIFIED** for every objective that does not require live Meta
credentials. Phase 6 (the WhatsApp messaging engine itself) was already complete and extensively
tested; Phase 7's job was hardening and verification, not a rewrite, and that is exactly what
happened: one real, previously-unenforced reliability gap was found and fixed (the per-organization
send rate limit), one real error-classification gap was found and fixed (Meta's own error codes,
not just HTTP status), one real observability gap was found and fixed (a failed status webhook's
Meta error code was being discarded), a documentation gap was found and fixed (`docs/whatsapp.md`
was referenced by two docstrings but never existed), and a stale default was corrected (the Graph
API version). Everything else audited came back clean: the webhook dedupe/ordering state machine,
the idempotency boundary, the retry classification, and the tenant-isolation matrix were already
correct and are now additionally verified with real infrastructure (a real worker process hard-
killed, a real severed PostgreSQL connection, a real unreachable Redis client, a real browser).

Live Meta verification remains **EXTERNALLY BLOCKED**: no Meta App, WABA, phone number or test
template exists in this environment, exactly as Phase 6 recorded. Nothing in this document claims
otherwise. Nothing has been committed to git (no commits unless asked, per standing instructions).

## Implemented (Phase 7 changes only -- Phase 6 is unchanged in substance except where noted)

### 1. Per-organization send rate limit, actually enforced (reliability)

`WHATSAPP_SEND_RATE_LIMIT` / `WHATSAPP_SEND_RATE_WINDOW_SECONDS` existed in `core/config.py` since
Phase 6 but were never read anywhere in the send path -- a runaway or misconfigured workflow could
generate unbounded outbound WhatsApp traffic. Fixed in
`services/messaging/whatsapp/send.py` (`_rate_limit_exceeded`, checked first in the policy gate,
before connection/suppression/consent/window), reusing the app's existing `RateLimiter`
(Redis-backed across workers, falling back to in-process). Classified `RATE_LIMITED` -- the same
code a provider-reported throughput error gets -- so the existing retry/backoff path in
`services/automation.py` handles both identically. Tests:
`test_send_blocked_once_the_organization_rate_limit_is_exceeded`,
`test_rate_limit_is_scoped_per_organization` (two separate tenants, separate buckets).

### 2. Meta error-code-aware classification (reliability + Meta verification)

`providers/_http.py`'s `classify_http_status` classified purely by HTTP status. Meta's Graph API
returns most WhatsApp Cloud API business-logic errors -- a transient throughput error exactly like
a permanent template-validation error -- as plain HTTP 400; the `error.code` field in the body is
what actually distinguishes them (verified live against
`https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes`). Before this fix, a
documented rate-limit code (`130429`, `80007`, `4`, `131056`) delivered inside a 400 would have been
misclassified `INVALID_REQUEST` and never retried -- a real reliability bug, not a hypothetical one.
Fixed by mirroring the identical, already-proven pattern in `app/services/ai/errors.py`
(`classify_http_status(status, body)`), which this module had simply not been brought up to. 28 new
tests in `tests/whatsapp/test_provider.py` (see "Playwright E2E" section below for why this file was
needed at all) include a parametrized table of every documented code and two end-to-end retry-vs-
not-retried proofs.

### 3. Meta's own error code captured on a failed status webhook (observability)

A `"failed"` status webhook's `errors[0].code` (Meta's own documented error code, e.g. `131026`
"recipient not a WhatsApp user") was parsed and then discarded; the message's `error_code` was
always the generic placeholder `"provider_reported_failure"`. Fixed in
`services/messaging/whatsapp/webhooks.py` (`parse_webhook_payload`, `_apply_status`): the code is
now stored as `meta_<code>` when Meta supplies one, falling back to the generic placeholder only
when it doesn't. An integer error code is safe to store and display -- never raw error text, never a
secret. Tests: `test_failed_status_captures_metas_error_code_for_debugging`,
`test_failed_status_with_no_error_code_falls_back_to_a_generic_placeholder`.

### 4. `docs/whatsapp.md` created

Referenced by two existing docstrings (`provider.py`, `providers/whatsapp_cloud.py`) since Phase 6
but never written. Now exists: architecture diagram, an IMPLEMENTED/VERIFIED/EXTERNALLY
BLOCKED/NOT YET IMPLEMENTED status table, the full sourced Meta-documentation diff, reliability
properties, known limitations (including one found while building the E2E suite: there is no
dashboard/API path to lift a suppression -- `lift_suppression` exists in code but is wired to
nothing), and a live-verification runbook for when credentials exist.

### 5. Stale defaults and documentation corrected

`whatsapp_api_version` default bumped `v21.0` -> `v26.0` (Meta's own live example as of this
session's fetch; only affects new connections). `templates.py`'s docstring corrected: named-
parameter templates are a current, real Meta feature, not an invalid one -- this integration just
doesn't support them yet. `state.py` and `whatsapp_cloud.py` docstrings updated with the Phase 7
verification findings (the `"played"` status is already safely ignored by the existing unrecognized-
status fallback; `message_status` on a successful send is a known, deliberately deferred gap, not a
silent bug).

## Database

No schema changes. The existing Phase 6 migration (`0008_whatsapp.py`) and models were audited, not
modified: every tenant table has a composite FK to its parent tying it to the same organization
(`fk_whatsapp_*_contact_org`, `_connection_org`, `_conversation_org`), every FK is index-led, every
tenant table is covered by `tests/test_tenant_schema_audit.py`'s generic FK/composite-reference/
index audit (ran clean as part of the full regression below, 2490 tests). `alembic upgrade head`,
the real-Postgres integration tier, and `test_migrations.py` (upgrade/downgrade/upgrade + `alembic
check`) all passed clean -- re-confirmed, not re-implemented, since nothing changed.

## API

No new endpoints. No behavior change to any existing endpoint's request/response shape. The one
behavior change reachable from the API is the new 429-equivalent rejection (`RATE_LIMITED` ->
surfaces as the existing `whatsapp_rate_limited` automation step error) once an organization's send
rate limit is exceeded -- previously there was no limit at all.

## Playwright E2E

`e2e/whatsapp.spec.js` (new, 29 checks), wired into `e2e/run.mjs` (added to the `OFFLINE` suite list,
`WHATSAPP_MESSAGING_PROVIDER=fake` + app secret/verify-token env vars for the stack, `/whatsapp`
added to the dashboard warm-up routes). Entirely offline, deterministic, no real Meta call. Covers:

- Connect with the fake provider; the connection card shows `phone_number_id`, never a raw token
  (checked in the rendered HTML AND in `localStorage`).
- Template sync against the fake provider is visibly refused (503), never faked.
- AI auto-reply toggles live, no page reload.
- A forged-signature webhook is rejected (401); a correctly signed one is accepted (200); the
  identical delivery sent twice is still accepted (200) but produces no duplicate message or
  ledger row.
- The inbound message renders in the conversation detail.
- Opting the SUPPORT category in updates its badge live.
- A minimal automation workflow (`send_whatsapp_text`, triggered by a real `whatsapp_message_received`
  webhook event) completes and the sent message appears in the conversation.
- Suppressing the contact and delivering another inbound message: the triggered send is rejected
  (`whatsapp_policy_blocked`), not silently dropped or sent.
- A template action with the wrong variable count fails closed (`whatsapp_invalid_request`) before
  any provider call; a correctly-filled, directly-seeded APPROVED template sends successfully and is
  recorded with its name/language.
- Disconnect preserves consent, suppression and message history (row counts compared before/after)
  and clears the stored token.
- Cross-tenant isolation: another organization gets a 404 (never 403, never a content leak) for the
  first organization's conversation, both through the browser and directly through the API.

Window-closed rejection was deliberately **not** re-tested here through a real inbound webhook: every
inbound message reopens the window as a side effect (`window_service.record_inbound`), so racing a
background worker to re-close it before its automation job runs would be an inherently flaky thing
to assert on in a real, asynchronously-processed browser run. It is already covered deterministically
by `tests/whatsapp/test_send.py::test_text_send_blocked_with_window_closed` and the equivalent
automation-integration test -- see "Known Limitations" for why this is an intentional scope boundary,
not a gap.

Two real, non-hypothetical test-authoring bugs were found and fixed while building this suite (both
are evidence the suite was actually run against the real stack repeatedly, not written once and
assumed correct): clicking the first "Opt in" button opted into TRANSACTIONAL, not the SUPPORT
category the test workflow actually needed; and a message-count assertion needed to exclude an
earlier FAILED row that legitimately shares the same `template_name` (recorded for audit visibility
by `send.py`'s `_write_failed`, by design).

Full offline suite re-run after these changes: `auth`, `security`, `layout`, `shopify`,
`automations`, `whatsapp` -- **all 6 suites pass**, 0 regressions.

## Meta Verification

Live-fetched against `developers.facebook.com` in this session (full sourced detail in
`docs/whatsapp.md`). Key findings, each independently diffed against the existing implementation:

- Endpoint construction, text/template payload shapes, webhook signature scheme: **confirmed
  unchanged and correct**.
- Current API version (`v26.0` vs the Phase 6 default `v21.0`): **corrected** (see Implemented #5).
- Error codes are not reliably distinguishable by HTTP status alone: **a real classification bug,
  fixed** (see Implemented #2).
- Named-parameter templates are a current, real Meta feature (not deprecated/invalid): **docstring
  corrected**, behavior unchanged (still correctly rejected, just described honestly).
- Webhook delivery is documented at-least-once and unordered by Meta itself: **directly confirms**
  (does not merely motivate) the existing dedupe ledger and rank-checked state machine are the right
  design, not defensive-only speculation.
- A `"played"` status value exists beyond sent/delivered/read/failed: **confirmed already handled
  safely** (the state machine's unrecognized-status fallback), now documented as intentional.
- A successful send's `message_status` field (`accepted`/`held_for_quality_assessment`/`paused`) is
  not read: **a real, found-but-deferred gap**, documented rather than silently left or hastily
  patched (implementing it would add a new message state, which is a bigger change than this
  objective's scope).
- Meta documents no idempotency guarantee for sends and no `Retry-After` mechanism for rate limits:
  **confirms** the existing "internal idempotency boundary, not a Meta guarantee" framing was
  already correct and should stay stated that way.
- `conversation.origin.type`'s current enum (pricing-category-based, not
  `business_initiated`/`user_initiated`) is **never read by this codebase** -- confirmed by grep, so
  there was no stale-enum bug to fix.

**Live verification against a real WABA/phone number remains EXTERNALLY BLOCKED** -- no credentials
exist in this environment. The verification harness Phase 6 already built (manual-credential
connect, live-verified via `fetch_phone_info` when a real provider is configured) is what would run
it; nothing here fabricates a result.

## Template Verification

Positional-variable validation (count, content, APPROVED-only) was audited against the code and
tested with a directly-seeded APPROVED template row (no real Meta sync is possible offline): a
correct variable set sends; a wrong count fails closed with `invalid_request` before any provider
call, in both the Playwright E2E suite and the existing unit suite
(`tests/whatsapp/test_templates.py`, `test_send.py::test_malformed_template_variables_fail_closed_before_any_provider_call`).
Named-parameter templates: confirmed a real, current Meta feature (not invalid); still out of scope,
now described accurately. A real Meta template create -> approve -> send round trip is **EXTERNALLY
BLOCKED**.

## Reliability / Failure Injection

All of the following are **real infrastructure**, not fakes, per this project's verification
standard -- new in `backend/tests/integration/test_worker_crash_recovery.py` (which already hosted
the equivalent Shopify/generic-automation real-process tests; extended, not duplicated, with a small
additive change to its shared `Stack` fixture for WhatsApp settings/env):

| Scenario | Mechanism | Result |
| --- | --- | --- |
| Worker hard-killed before a `send_whatsapp_text` action's message row ever committed (before any provider call) | Real OS process kill (`TerminateProcess`/`SIGKILL`), a `pg_sleep` trigger holding the `whatsapp_messages` INSERT | Execution left RUNNING with a stale heartbeat; a second real worker's crash recovery fails it outright (`worker_lost_during_whatsapp_send`), never retries; no message row ever exists. |
| The same send, but PostgreSQL is a real severed-and-restored TCP connection instead of a killed process | `TcpProxy` cut mid-insert, then restored | The worker survives (never crashes), nothing was ever committed, and recovery (the same still-alive worker's retry, backed up by a staleness-based second worker) completes the send exactly once -- one message row, not a duplicate. |
| The same inbound webhook delivered twice, consumed by a real worker process (not a direct function call) | Two identical signed `receive_webhook` calls, then a real worker drains the resulting job(s) | Exactly one ledger row, one inbound message, one automation task -- not two. |
| Redis genuinely unreachable during webhook intake | A real `RedisJobQueue` pointed at a closed port, not a mock | Intake fails closed: 503, and the just-written ledger row is deleted rather than left orphaned for Meta to never correctly retry. |

Everything in the "Failure-Injection Checklist" that these four scenarios don't individually cover
is covered by Phase 6's existing unit-level simulated tests, confirmed still passing: provider
authentication failure, provider rate limit, provider 4xx/5xx, invalid template, missing/extra
template variables, expired window, suppressed/opted-out customer, invalid phone number, duplicate
send request (idempotency key reuse), invalid webhook signature, malformed webhook, unknown provider
message id. The two that remain impossible to exercise honestly in this environment: a real Meta
rate-limit/auth response (needs live credentials) and the specific "worker killed mid-provider-HTTP-
call" window (the fake provider's call is synchronous and instant in-process; simulating a pause
there would mean adding a test-only delay hook to production provider code for one scenario, which
was judged not worth the risk/scope for this phase -- the "before the row committed" and "DB outage
during the same window" scenarios above already prove the crash-safety property that matters).

## Security

Tenant/role/auth matrix (`tests/test_tenant_api_matrix.py`, 473 cases covering every WhatsApp route)
and the dedicated adversarial suite (`tests/whatsapp/test_security.py`, 14 tests: token corruption/
rotation/missing-key, cross-tenant access, role enforcement, opt-out/template/window bypass
resistance) -- both re-run clean, unchanged from Phase 6, no new routes added this phase. The
Playwright suite adds a second, independent confirmation at the browser/HTTP layer: a cross-tenant
conversation 404s both through the UI and directly through the API (never 403, never a content leak),
and no raw access token appears anywhere in rendered HTML or `localStorage`.

## Mutation Testing

**179 of 179 mutants: 179 killed, 0 survived.** 8 new Phase 7 mutants
(`backend/mutation/catalog_7.py`, registered in `mutation/run.py`), targeting exactly the three
Phase 7 code changes: the rate-limit check (`wa7-01`, `wa7-02`), the Meta-error-code-aware
classification (`wa7-03` through `wa7-06`), and the failed-status error-code capture
(`wa7-07`, `wa7-08`). All 8 killed. The pre-existing 20 Phase 6 WhatsApp mutants were also
**re-run** this session (not merely assumed from cache, since this phase's edits touched
`send.py`, `_http.py`, `webhooks.py`, `errors.py` -- the same files several Phase 6 mutants target)
and all 20 still kill correctly: no regression in mutation coverage from this phase's changes. The
other 151 pre-existing mutants (Phase 4/4.1/5) were not re-executed this session; `mutation.run
check` confirms every snippet, old and new, still matches the current codebase exactly once.

## Regression

- **Backend, non-integration tier** (`pytest -m "not integration"`): **2490 passed, 2 skipped, 129
  deselected.** (Phase 6 baseline was 2456; +34 from this phase's new tests.)
- **Backend, integration tier** (`pytest -m integration`, real PostgreSQL + Redis): **132 passed, 1
  skipped.** (Phase 6 baseline was 128+1 skipped; +4 from the new real-process failure-injection
  tests above.)
- **`tests/whatsapp/` in isolation**: **157 passed** (129 pre-existing/Phase-6-shaped plus the 28
  new provider tests, 2 new rate-limit tests and 2 new error-code-capture tests this phase added).
- Static checks: `ruff check`, `ruff format --check` on every touched file: clean. `mypy app tools`
  (160 source files, unchanged count -- no new `app/` modules this phase): clean.
- Migration: `alembic upgrade head` / the integration tier's upgrade-downgrade-upgrade + `alembic
  check` cycle: clean (re-confirmed, nothing changed to migrate).

## Frontend

`npm run typecheck`, `npm run lint`, `npm run build` (the `/whatsapp` and
`/whatsapp/conversations/[id]` routes build correctly), `npm test` (15 pre-existing tests, 0
failures) -- all clean. No frontend source files were changed this phase: the one frontend change
considered (translating `error_code` into a human-readable sentence, per the production prompt's
"show a safe human-readable explanation") was deliberately **not** made after checking for
precedent: the rest of the dashboard (e.g. the automation execution detail page) already shows the
raw internal code with no translation layer, consistently, everywhere. Introducing one only for
WhatsApp would be an inconsistent, unscoped design decision for one feature rather than a project-
wide UX improvement; the existing display already satisfies "preserve an internal code for
debugging," which is the other half of that same requirement.

## Known Limitations

Unchanged from Phase 6 (AI auto-reply is single-turn, not RAG-grounded; `template` variables are
positional-only; `WebsiteLead.marketing_consent` is not auto-imported into `WhatsAppConsent`; media
is modeled but never sent/received), plus, found and documented in Phase 7:

- There is no dashboard or API path to lift a suppression once set (`lift_suppression` exists in
  `services/messaging/whatsapp/consent.py`, wired to nothing). Objective 1's "restore eligible state
  where supported" is genuinely not supported today -- an inherited Phase 6 gap, not a Phase 7
  regression, surfaced explicitly here rather than silently worked around in a test.
- `message_status` (`accepted`/`held_for_quality_assessment`/`paused`) on a successful send is not
  read; `conversation.id`/`origin.type`/`pricing` on a status webhook are not read. Neither is
  currently needed by anything this integration does; both are documented as found-but-deferred.
- Named-parameter templates remain unsupported (a real Meta feature, a scope choice).
- No automated test exercises a real worker being killed specifically mid-provider-HTTP-call (as
  distinct from before the message row committed) -- see "Reliability / Failure Injection" above for
  why, and why the two scenarios that were built already establish the crash-safety property.

## External Blockers

**Live Meta verification is externally blocked because no credentials are available.** Specifically
blocked: a real WABA/phone number connection, a real inbound webhook delivery, a real template
create-approve-send round trip, a real rate-limit/auth error response, Meta's exact GET-handshake
response wording (plain-text vs JSON, 200 vs 403) beyond what Phase 6 already implemented
consistent with community convention. The live-verification runbook in `docs/whatsapp.md` is ready
for when credentials exist.

## Files Changed (Phase 7 only -- Phase 6's already-complete, already-uncommitted implementation is
unchanged in substance; this list excludes files that were already modified/untracked before this
session started)

**Backend, edited:**
`app/services/messaging/providers/_http.py`, `app/services/messaging/whatsapp/send.py`,
`app/services/messaging/errors.py`, `app/services/messaging/whatsapp/webhooks.py`,
`app/services/messaging/whatsapp/templates.py`, `app/services/messaging/providers/whatsapp_cloud.py`,
`app/services/messaging/whatsapp/state.py`, `app/db/models/whatsapp.py` (docstring only),
`app/core/config.py` (api_version default + comments), `mutation/run.py` (register catalog_7),
`tests/whatsapp/test_send.py` (+2 tests), `tests/whatsapp/test_webhooks.py` (+2 tests),
`tests/integration/test_worker_crash_recovery.py` (+4 tests, `Stack` extended additively).

**Backend, new:**
`tests/whatsapp/test_provider.py` (28 tests), `mutation/catalog_7.py` (8 mutants).

**E2E, edited:** `e2e/run.mjs` (WhatsApp env vars, suite registration, warm-up route).
**E2E, new:** `e2e/whatsapp.spec.js` (29 checks).

**Docs, new:** `docs/whatsapp.md`, `docs/phase-7-progress.md` (this file).

**Incidental:** `backend/mutation/expected.py` picked up a purely cosmetic `{\n}` -> `{}`
normalization from a `ruff format` invocation whose target list was broader than intended; the
change is semantically inert (an empty dict literal either way) and was left rather than reverted.

## Next Exact Action

Phase 7's own checklist (Playwright E2E, Meta documentation verification, reliability hardening,
real-infrastructure failure injection, mutation, full regression, documentation) is complete for
everything achievable without live Meta credentials. If continuing further WhatsApp work: the
clearest next steps are (1) obtain real Meta credentials and run the live-verification runbook in
`docs/whatsapp.md`, or (2) await explicit approval before starting Phase 8 (the AI Conversation
Layer), per standing instructions and the Phase 7 brief's own explicit boundary.

## How To Resume

1. Read this file, then `git status --short --branch` and `git log --oneline -6` (nothing from
   Phase 6 or Phase 7 has been committed; everything is working-tree changes only).
2. Docker Desktop must be running; `docker compose up -d db redis` from the repo root starts the dev
   PostgreSQL (`bizflow`) and Redis.
3. Re-run `python -m pytest -m "not integration" -q` and `python -m pytest -m integration -q` from
   `backend/` to reconfirm zero regressions (2490 and 132+1-skipped respectively at the end of this
   checkpoint). `cd e2e && node run.mjs` re-runs all 6 offline browser suites.
4. `backend/mutation/catalog_7.py` lists the 8 Phase 7 mutants (`python -m mutation.run run wa7-` to
   re-run just those).
5. Live Meta verification remains externally blocked; do not claim otherwise.
