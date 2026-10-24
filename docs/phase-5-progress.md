# Phase 5 Progress

## Current Status

Implementation is substantially present in the working tree, but Phase 5 is **not complete**.
The fast backend suite and local frontend/widget checks are in progress or passing. The Docker
engine is unavailable, so database-backed migration, Redis/worker integration, browser E2E,
failure-injection, and mutation gates remain unverified. Do not begin Phase 6.

The pre-Phase-5 tree was entirely untracked on `main` with no commits. It matched the documented
Phase 4.1 baseline (migrations `0001`-`0006`); the complete existing tree was committed first as
`Phase 4.1 production-ready baseline`. Phase 5 edits are separate and not yet committed.

## Completed

- Inspected README and architecture/progress/production/Shopify/AI-provider documentation, API,
  models/migrations, queue/worker, AI service, Shopify integration, chat, frontend, and tests.
- Added tenant-scoped workflows, internal events, execution/step history, website leads, and internal
  follow-up tasks with tenant-aware foreign keys, deduplication constraints, statuses, and indexes.
- Added typed workflow/trigger/condition/action validation, event conversion from existing Shopify
  webhook processing, event dedupe, execution snapshots, durable wait scheduling, retries, recovery,
  bounded depth/steps, tag/task/audit/AI actions, and a messaging provider boundary that fails
  honestly when unconfigured.
- Added tenant-scoped APIs and owner/admin write controls; no public event injection API.
- Added voluntary website lead capture tied to widget key and conversation token, with consent/source
  metadata and an internal lead event. Anonymous users are not treated as known customers.
- Added the structured `/automations` dashboard with workflow controls and execution inspection.
- Added focused unit/API/security tests and PostgreSQL integration tests; updated migration and worker
  tests. Added [automation.md](automation.md).
- Local checks already passed: widget tests (48), frontend tests (15), frontend lint, typecheck and
  production build; mypy; targeted Ruff; and a 232-test focused backend run before the final tag/payload
  edits. Reruns/results are being recorded below.

## In Progress

- Final verification and checkpoint commit after full backend non-integration suite passed.
- Update README, architecture, production and progress docs with the Phase 5 architecture and honest
  verification status.
- Review diff, secret hygiene, and final Git state; create logical Phase 5 checkpoint commits.

## Remaining

- Run migration `0007` upgrade/downgrade/upgrade and `alembic check` against PostgreSQL.
- Run PostgreSQL + Redis integration tests, duplicate delivery, worker crash/recovery, Redis outage,
  PostgreSQL transient failure and delayed execution recovery with real local services.
- Run browser E2E including workflow lifecycle, execution inspection/failure and tenant isolation.
- Extend/run mutation coverage and classify all survivors.
- Complete security and tenant-isolation audit against actual DB constraints and race scenarios.
- Review and commit final docs/tests/implementation checkpoints.

## Database Migrations

- Added `backend/app/db/migrations/versions/0007_automation_engine.py`.
- Creates workflows, events, executions, execution steps, website leads, and automation tasks; adds
  customer tags. Includes tenant composite foreign keys, unique idempotency constraints, indexes,
  status constraints, and downgrade logic.
- Migration cycle and `alembic check` were attempted but not verified: the approved `docker compose up
  -d db redis` command could not connect to the Docker Desktop Linux engine (named pipe unavailable).
  `alembic check` consequently waited on the unavailable database and was interrupted.

## API Changes

- Added `backend/app/api/automations.py`, registered in `backend/app/main.py`.
- Tenant-scoped list/create/detail/patch/activate/pause/archive and execution list/detail routes.
- Website lead capture added to public chat router; public internal automation event writes are not
  exposed.

## Frontend Changes

- Added `frontend/src/app/automations/` pages, actions and forms; added navigation and DAL/types.
- Structured editor supports the available event/condition/action definitions and workflow lifecycle;
  detail pages show recent executions and step details.
- Frontend tests: 15 passed; lint, typecheck and build passed after implementation edits.

## Worker Changes

- Added automation event/execution job types to the existing Redis queue and dispatch to the existing
  worker. Maintenance requeues durable due/queued/retryable work and recovers stale claims.
- Existing Shopify webhook worker now converts normalized outcomes into internal events before
  marking source webhook processed; deduplication makes replay safe.
- No separate worker architecture and no in-memory delay sleeps.

## Tests

- `backend/tests/test_automation.py`: validation, conditions, dedupe, snapshots, scheduling/actions,
  tenant access, RBAC, lead capture, AI injection handling and recovery.
- `backend/tests/integration/test_automation_postgres.py`: Postgres/Redis event/execution and
  composite tenant FK integration coverage (not run; services unavailable).
- Updated migration-cycle expectations and existing worker process test.
- Focused backend command: 232 passed, 1 warning before final customer tag/payload changes.
- Full backend command (`-m 'not integration'`): **2,147 passed, 1 skipped, 125 deselected**, 2
  warnings. The 125 integration tests were intentionally excluded because their Postgres/Redis
  services were unavailable. The first run exposed the required application-route policy table
  missing new routes; the matrix was updated and its focused rerun passed: 330 passed, 2 warnings.
- Static checks: Ruff check/format and mypy clean. Secret hygiene: 30 passed. Frontend tests 15,
  widget tests 48, lint/typecheck/build passed (see prior command results in this work session).
- Widget: 48 passed. Frontend: 15 passed.
- Baseline secret hygiene: 30 passed.
- Do not state full Phase 5 test counts until reruns complete.

## Security Tests

- Added cross-tenant workflow/execution API assertions, role-based write checks, malformed definition
  rejection, fail-closed condition/operator handling, duplicate event behavior, explicit lead capture
  and consent behavior, and AI prompt-injection data separation tests.
- Database-level composite FK integration assertion exists but awaits PostgreSQL.
- Full route/security matrix, concurrency races, browser tenant checks, and mutation audit remain.

## Known Failures

- Docker Compose database/Redis startup failed because Docker Desktop Linux engine pipe was unavailable.
- `alembic check` could not complete without the database.
- Phase 5 integration, browser E2E, failure-injection and mutation gates are not verified.
- Python test run uses `backend/.venv/Scripts/python.exe`; system Python lacks pytest.

## Known Limitations

- Shopify cart scopes/events are not part of the existing integration; `cart_abandoned` is only a
  typed internal trigger foundation. No product-view-based abandonment is inferred.
- Existing event sources do not establish delivered-order state.
- No WhatsApp/messaging provider is configured; actions fail explicitly, never report delivery.
- AI stale-call recovery fails terminally rather than automatically repeating a potentially completed
  provider request. Provider-side idempotency is not assured for timeouts.
- No live Shopify or AI credentials are available. Existing Phase 4.1 operational limitations remain.

## Next Exact Action

Review the full diff and final Git state, then create the Phase 5 implementation checkpoint commit.
Keep database, browser E2E, failure-injection and mutation gates explicitly open until run in an
available integration environment.

## How To Resume

1. Read this file, then `git status --short --branch` and `git log --oneline -3`.
2. Resume with the next exact action above; use `backend/.venv/Scripts/python.exe` for Python tests.
3. Backend non-integration suite has passed: 2,147 passed, 1 skipped, 125 deselected.
4. For unavailable services, record the failure and leave the gate open; do not claim Phase 5 complete.
5. Keep this file current at each meaningful checkpoint. Do not start Phase 6.
