# Automation engine (Phase 5)

## Scope and architecture

The automation service extends the existing Shopify webhook processor, Redis job queue,
PostgreSQL models, and AI provider registry. PostgreSQL is authoritative for events, workflow
definitions, executions, steps, tasks, leads, and scheduled work. Redis only wakes the existing
worker. A queue loss is recovered by its maintenance pass from database state.

An internal event is deduplicated by organization and deduplication key. Dispatch matches only
active workflows, validates their typed definitions, and records an execution snapshot containing
the workflow definition and event payload. Editing a workflow therefore affects future executions;
an in-flight run retains the snapshot it started with.

The supported event names and fields, trigger validation, condition operators, and action schemas
are explicit allowlists in `backend/app/services/automation.py`. Unknown or malformed definitions
fail closed. Conditions are evaluated by typed comparisons; no tenant-provided Python, JavaScript,
or expression is executed.

## Execution, idempotency, and recovery

Executions and steps have explicit states. Database row locks claim work; unique constraints enforce
one execution per workflow/event and one step record per execution/step. Side effects use a stable
execution/step identity: customer and lead tags are set-like updates, tasks have a unique
execution/step key, and audit steps are uniquely recorded. Event ingestion also uses a unique
organization/dedupe key. Shopify webhook processing converts normalized database rows into internal
events before marking the webhook processed, so a crash replays safely through event deduplication.

Waits store their due time in PostgreSQL and return the worker to its queue; no in-process sleep is
used. The maintenance pass requeues due, queued, and retryable work and recovers stale claims with
bounded exponential backoff and attempt limits. Execution depth and step count are bounded to
reduce event loops and runaway workflows. Redis is not the source of truth.

There is no configured messaging provider. A messaging action returns a clear failed result and
cannot claim delivery. The provider interface requires an idempotency key for a future real adapter.
An uncertain in-flight AI call is failed terminally during stale-work recovery rather than
automatically repeated, avoiding a duplicate generation at the cost of requiring operator review.
The current provider abstraction does not guarantee provider-side idempotency for an API timeout.

## AI and untrusted data

AI generation uses the existing provider registry. Event/customer/product text is passed as data,
with system instructions defining it as untrusted; it cannot change the workflow or invoke tools.
The output is length-bounded and treated as content only. Generated text is not logged. No arbitrary
tool execution is available to a workflow.

## Website leads and Shopify events

The website widget offers an optional explicit contact form after a conversation exists. A visitor
must submit a valid name and at least one contact method. Marketing consent defaults to false and
source/consent metadata is stored. Lead events carry an internal lead identifier and safe flags,
not the contact values. Anonymous visitors are not promoted to customers and are not messaged.

Existing Shopify webhook handling remains the ingestion source. Current normalized event mapping
covers order create/cancel, customer create/update, product create/update, and order update states
that establish paid/fulfilled. The existing app does not request cart scopes or receive an
abandoned-cart webhook, and it has no delivered event source. Consequently `cart_abandoned` is a
validated internal trigger foundation only; no abandoned-cart event is fabricated from product
views. A real cart source and provider-backed delivery remain deployment work.

## API and access control

Authenticated endpoints are under `/api/automations`: list/create, detail/update, activate, pause,
archive, execution list, and execution detail. Reads are constrained to the current organization;
mutations require the existing owner/admin role dependency. There is no public internal-event
injection endpoint. Website lead capture is a public chat operation bound to the public widget key
and conversation token and uses existing public-chat rate limiting.

## Operations and limitations

Apply migration `0007_automation_engine` before deploying API or worker code. Existing queue and
worker health/recovery mechanisms are reused. Review failed execution/step classifications and
attempt counts from the authenticated dashboard API. Keep database backups and Redis configured as
documented in `production.md`.

The Phase 5 local code checks pass as recorded in `phase-5-progress.md`, but this environment did
not provide a running PostgreSQL/Redis Docker engine. Migration up/down/up, database/worker crash
injection, browser E2E and mutation coverage have not been verified for Phase 5. Do not treat this
feature as production-gated until those checks are run in the project integration environment.
