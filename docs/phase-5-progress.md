# Phase 5 Progress

## Current Status

Phase 5 implementation and repository verification gates are complete on the current tree. The final
verification pass used real PostgreSQL, Redis, existing worker processes and browser E2E. The current
verification changes are not yet committed; see Git status before deployment. This does not certify
live Shopify, AI-provider or messaging delivery.

## Completed

- Tenant-scoped workflow, event, execution/step, lead and follow-up task models; typed workflow
  validation; snapshot execution; scheduling, retry, recovery, idempotency and bounded event depth.
- Shopify webhook conversion reuses the existing webhook path; website lead capture is voluntary and
  rate limited. Dashboard/API provides workflow lifecycle and execution inspection.
- Fixed an omitted event foreign-key index in migration 0007/model after the real PostgreSQL FK audit.
- Fixed UTC comparison handling for SQLite-backed retries, removed duplicate tag assignment, and made
  the Shopify OAuth rate limit configurable (default 20; E2E isolated process uses 100 to avoid the
  suite's unrelated request burst hitting the product limit).
- Added process-level worker/Redis/PostgreSQL failure tests, delayed-wait recovery and duplicate-side
  effect assertions. Added Phase 5 mutation catalog and browser coverage.

## In Progress

- None.

## Remaining

- No repository Phase 5 production-gate checks remain. Live Shopify/AI credentials and a real
  messaging provider remain external deployment verification, as described under Known Limitations.

## Database Migrations

- Migration `backend/app/db/migrations/versions/0007_automation_engine.py` creates the automation
  schema and adds `ix_automation_executions_event_id` for the event foreign key.
- Migration up/down/up passed against PostgreSQL. `alembic check` reports no new upgrade operations.
- The PostgreSQL FK index audit passed after adding the missing index.

## API Changes

- Tenant-scoped `/api/automations` workflow and execution APIs; owner/admin mutation checks and no
  public internal event injection endpoint.
- Public lead capture remains bound to the existing widget key and conversation token.

## Frontend Changes

- Minimal `/automations` workflow management and execution inspection.
- Browser E2E covered create/activate/pause, task execution, explicit messaging-action failure,
  execution inspection and cross-tenant isolation.

## Worker Changes

- Reuses the existing Redis queue and worker. PostgreSQL remains the durable event and schedule
  store; maintenance requeues persisted work after Redis or process recovery.
- Failure recovery tests killed a worker during an action, restarted a worker for a due wait, made
  Redis unavailable during enqueue and injected a PostgreSQL transient failure. Work recovered and
  side effects remained idempotent.

## Tests

- PostgreSQL/Redis integration: **128 passed, 1 skipped**, including Phase 5 duplicate delivery,
  process crash, outage, delayed recovery and tenant-FK coverage.
- Backend non-integration regression: **2,151 passed, 1 skipped, 129 deselected**. The 129
  deselected tests are integration-marked and were run in the integration tier above.
- Browser E2E offline suite: **156 checks passed** across auth (34), security (16), layout (8),
  Shopify (87) and automation (11). No live public-site crawl suite was requested by this offline
  production gate.
- Frontend: **15 tests passed**, lint, typecheck and production build passed.
- Widget: **48 tests passed**.
- `ruff check`, `ruff format --check`, `mypy app tools`, and `alembic check` passed.
- Migration up/down/up passed.

## Security Tests

- Tenant and role security was covered by backend/API regression and browser tenant-isolation checks.
- Malformed trigger/condition/action definitions, unknown operators, injection-like customer/product
  text, replay/deduplication and cross-organization execution access are covered by tests.
- Mutation framework: **151/151 killed, zero survivors**. This includes the seven Phase 5 mutants.
  The six previously expected survivors were killed by the expanded tests; `backend/mutation/expected.py`
  no longer classifies them as survivors. The worker-startup mutation was rerun after making its test
  fail immediately if execution passes the startup gate. Mutation runner confirmed `app/` was byte
  identical before and after both complete runs.
- No unresolved CRITICAL or HIGH finding was identified by this verification pass.

## Known Failures

- No required Phase 5 verification gate failed on the final reruns. An earlier E2E attempt hit the
  existing Shopify OAuth rate limit; the test runner now raises that limit only in its isolated
  process, while the product default remains unchanged.
- The initial PostgreSQL FK audit found a missing event index; model and migration were fixed and the
  audit plus integration suite passed.

## Known Limitations

- No real Shopify merchant credentials, AI vendor credentials, or messaging provider are available.
- `cart_abandoned` remains an internal trigger foundation because the current Shopify integration
  has no cart scope/webhook source. No abandonment is inferred from product views.
- No delivered-order event source exists. Uncertain AI calls fail closed during stale recovery rather
  than risk a duplicate provider call; provider-side idempotency for timeouts is not assured.
- Existing Phase 4.1 limitations remain: one Shopify store per organization, 60-day order import,
  no incremental sync, no session revocation, no Python lock file, and operator-managed database
  volume encryption/Redis durability.

## Next Exact Action

Review the final diff and commit the verification fixes and documentation. Then use the documented
live-integration runbooks when Shopify and AI credentials/provider configuration are available. Do
not start Phase 6 as part of this checkpoint.

## How To Resume

1. Read this file, then `git status --short --branch` and `git log --oneline -4`.
2. For repository verification reruns, use `backend/.venv/Scripts/python.exe`; the existing local
   Docker PostgreSQL and Redis services are the integration dependencies.
3. Use `backend/mutation/README.md` for the 151-mutant framework. Verify the working tree is clean
   after the runner restores its mutations.
4. Live Shopify, AI, and messaging checks require external credentials/configuration; follow
   `docs/shopify.md` and `docs/ai-providers.md`. Do not claim those integrations were live-tested.
