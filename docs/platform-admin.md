# Platform Admin

The founder/operator layer on top of the existing organization-scoped BizFlow AI product. A
platform admin sees and manages every brand; a brand user only ever sees their own organization.

## Model

There is **no second authentication system** (the brief's own hard constraint). A platform admin
is simply a `User` row with `is_platform_admin = True` (`backend/app/db/models/user.py`) --
orthogonal to organization membership, not an organization role. A platform admin typically has
**zero** `OrganizationMember` rows of their own; `OrganizationRole.OWNER` is never overloaded to
mean "platform admin" (brief section 4).

They authenticate through the exact same `POST /api/auth/login` as everyone else, get the exact
same JWT (which still only encodes the user id -- `app/core/security.py` is unchanged), and the
same `bizflow_session` HttpOnly cookie on the frontend. The only new thing is a 403-gated set of
`/api/platform/*` routes and a frontend `/admin/*` section, both of which check
`user.is_platform_admin` fresh from the database on every request
(`app/api/deps.py:platform_admin_user`).

## Authorization: the view-as-brand resolver

`services/organizations/service.py` has two org-context resolvers now:

- `resolve_org_context` (existing): proves a **membership row** exists, and now also hides a
  deactivated brand (`Organization.is_active = False`) from its own members.
- `resolve_platform_admin_org_context` (new): a platform admin has no membership row, so this is
  a **deliberately separate** function, never a bolt-on boolean check inside the first one. Its
  only question is "does this organization exist" -- authorization (is the caller actually an
  admin?) is the caller's job, enforced once in `app/api/deps.py` before either resolver is ever
  reached.

`api/deps.py`'s `current_org` and `org_from_path` dependencies choose between the two based on
`user.is_platform_admin`:

- `current_org` (header-resolved routes): a normal user's `X-Platform-View-Organization-Id` header
  is silently ignored -- `resolve_org_context` is still what runs for them, so the header can never
  widen a non-admin's access. Only when the authenticated user's own `is_platform_admin` column is
  true does the header activate the admin resolver.
- `org_from_path` (path-resolved routes, e.g. `/api/organizations/{id}/...`): the id is already
  explicit in the URL, so a platform admin reaches it the same way, with no extra header needed.

The returned `OrgContext` carries the admin's **own real identity** as `user` (never a synthetic
brand-user actor -- brief section 13) with an `OWNER`-equivalent role, so every existing
`ManagerOrg`/`CurrentOrg`/`PathOrg`-gated route (operations, support, WhatsApp, email, ...) works
for view-as-brand with **zero changes to those routers**. This is the main reuse win: the Safe
Action Framework, approvals, and every org-scoped page work unmodified under admin view.

Mutation-tested (`backend/mutation/catalog_platform.py`, 5/5 killed): the admin gate being
removed, the header activating for a non-admin, `org_from_path` skipping the `is_platform_admin`
check, the admin resolver not actually requiring the org to exist, and a deactivated brand staying
reachable by its own member are all mutants that a real test catches.

## Frontend: view-as-brand without touching brand pages

`frontend/src/lib/session.ts` adds a second HttpOnly cookie, `bizflow_admin_view_org`, separate
from the session cookie. `lib/dal.ts`'s two low-level fetch helpers (`authed`/`authedOrMissing`)
read it and attach it as the `X-Platform-View-Organization-Id` header automatically -- every
existing DAL function and every existing brand page (`/dashboard`, `/operations`, `/support`, ...)
honors view-as-brand with no changes of their own. The only new frontend-visible piece is
`components/admin-view-banner.tsx`, rendered once in the root layout, which shows the
"⚠ PLATFORM ADMIN VIEW" banner and an "Exit Brand View" button whenever the cookie is set.

**Known dev-mode quirk** (not a security issue): in `next dev`, immediately after
"Open Workspace"/"Exit Brand View" (hard navigations driven by plain `<form action={serverAction}>`
submissions), `document.body.textContent` can transiently include leftover markup from the
previous page alongside the new page's correct content, before any user-visible repaint settles.
The *new* page's own content (confirmed independently via the backend API, the audit trail, and a
scoped DOM check on the banner element itself) is always correct; this was caught and worked
around in `e2e/platform-admin.spec.js` by scoping its assertion to the banner element rather than
the whole document body. Directly re-checked against a real `next build && next start` production
server: Open Workspace on UrbanThread, Exit Brand View, Open Workspace on GlowCart, screenshotting
immediately after each navigation -- banner and dashboard content were correct on first paint every
time, confirming this is specific to `next dev`'s streaming behavior and not investigated further
as it does not affect correctness, scope, or isolation under the build that actually ships.

## Audit

`platform_audit_events` (migration `0012`) is a **separate table** from
`OperationalAuditEvent` -- same append-only pattern (`services/platform/service.py:record_audit`
mirrors `services/operations/audit.py:record`), different concern: who accessed or changed *which
brand's existence/membership*, not what happened inside one brand's operations.
`organization_id` is nullable and `ON DELETE SET NULL` (not `CASCADE`, the usual tenant-table
convention) because some events (`PLATFORM_ADMIN_LOGIN`) aren't about any one brand, and an event
that *is* about a brand must still survive that brand being deleted -- the audit trail outlives
the organization it references. Event vocabulary: `PLATFORM_ADMIN_LOGIN`, `BRAND_CREATED`,
`BRAND_OWNER_CREATED`, `BRAND_ACTIVATED`, `BRAND_DEACTIVATED`, `PLATFORM_ADMIN_ENTERED_BRAND`,
`PLATFORM_ADMIN_EXITED_BRAND`. `actor_user_id` is always the real platform admin -- never
overwritten, never a synthetic brand-user id (brief section 13), verified end to end in
`tests/platform/test_api.py::test_enter_and_exit_brand_are_audited_with_the_admins_own_identity`
and live over HTTP (see `docs/phase-10-progress.md`).

## Brand lifecycle

`Organization.environment` (`DEMO`/`PRODUCTION`, default `PRODUCTION`) and `Organization.is_active`
(default `True`) are explicit columns, never inferred from the name. `POST /api/platform/brands`
(`services/platform/service.py:create_brand`) reuses `services/auth/service.py:register_user` and
`services/organizations/service.py:create_organization` verbatim -- it does not duplicate
org/owner creation logic -- then tags the environment and writes the two audit events in the same
transaction. The response (`BrandCreated`) never includes the password or its hash; the frontend
form field is `type="password"` and nothing echoes it back into page HTML.

## Seeding a platform admin

`tools/seed_platform_admin.py` reads `PLATFORM_ADMIN_EMAIL`/`PLATFORM_ADMIN_PASSWORD` and upserts
one `User` with `is_platform_admin=True` (`--i-know-what-im-doing` required when `ENVIRONMENT` is
production). No env vars are set in this dev environment, so nothing is seeded automatically on
startup; `founder@bizflow.demo` is a dev-only admin created by hand with this tool for this round's
live verification and intentionally left in the database afterward as a working example login,
rather than deleted. It is not a production credential.

## Concurrent approval (failure injection)

`services/operations/approvals.py:approve`/`reject` were, in the original Phase 10 pass, a plain
read-then-write (`if approval.status != PENDING: raise; approval.status = APPROVED`) -- the exact
same shape `tasks.claim_task` deliberately avoids. Two admins approving the same action at once
could both pass the check before either committed, with the second simply overwriting the first's
`approved_by`/`approved_at` -- a silent double-approval, not a 409. Found while working through
this brief's explicit "concurrent approval: exactly one logical approval" failure-injection point
(section 33) and fixed with the same atomic `UPDATE ... WHERE status = PENDING` pattern
`tasks.claim_task` already uses; `actions.approve_action`/`reject_action` now catch the resulting
`ApprovalNotPending` and re-raise it as `ActionError`, which the router already maps to 409 (this
mapping was itself missing before -- `ApprovalNotPending` had no catch anywhere, so even the
pre-existing "approve something already decided" case would have surfaced as an unhandled 500).
Covered by `tests/operations/test_actions.py::test_only_one_of_two_concurrent_approvals_succeeds`
and mutation `plat-06` (removing the rowcount guard is killed).

**Known adjacent risk, not fixed in this pass**: `execute_action` has the same read-then-write
shape for its `status is APPROVED` check. Two concurrent executes of the same approved action
could both pass that check and both run the executor -- for `_record_only` actions this only
means a duplicate ledger entry, but for `inventory_adjustment` (a real local mutation) it could
mean a real double-decrement. Out of scope for this pass (the brief named *approval*, not
*execute*, concurrency); noted here rather than silently left unexamined.

## Security summary

- Server-side only: `platform_admin_user` (403) gates every `/api/platform/*` route; the frontend
  hiding `/admin` from a non-admin's nav is cosmetic, not the control.
- A non-admin's `X-Platform-View-Organization-Id` header has no effect (verified by
  `test_a_non_admin_cannot_use_the_view_as_brand_header_to_escalate` and mutation `plat-02`).
- `org_from_path` gates the admin resolver on `is_platform_admin` too, not just `current_org`
  (mutation `plat-03` -- this one actually survived its first version of the catalog and was
  caught only because the existing tenant-API-matrix cross-tenant test was added to its test list;
  see `docs/phase-10-progress.md`).
- Cross-brand isolation under view-as-brand verified at three levels: unit
  (`tests/platform/test_service.py`), HTTP (`tests/platform/test_api.py`), and browser
  (`e2e/platform-admin.spec.js` -- brand A's name never appears while viewing brand B).
- `PLATFORM_ADMIN_PASSWORD`, if set, is checked by `app/core/startup_checks.py` the same way
  `JWT_SECRET_KEY` is: production refuses to start with a weak or placeholder value.
