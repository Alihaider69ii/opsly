# Phase 6 Progress

## Current Status

Phase 6 (WhatsApp Business messaging engine) is IMPLEMENTED and VERIFIED end-to-end: domain
model, provider abstraction, policy engine (consent/suppression/window/templates), webhook
intake+processing, outbound send orchestration with a documented idempotency boundary, automation
engine integration, dashboard API, minimal frontend dashboard, a dedicated `tests/whatsapp/`
pytest suite (233 tests), and a 20-mutant mutation catalog (all killed). The frontend was also
manually exercised in a real browser against the real backend and a real PostgreSQL database
(see "Live Verification"). Nothing has been committed to git (no commits unless asked, per
standing instructions). Do not start Phase 7.

## Completed

Everything listed in the previous checkpoint (database, provider abstraction, phone
normalization, policy engine, send orchestration, state machine, webhooks, automation
integration, AI auto-reply foundation, settings/startup gate) remains in place and unchanged in
substance. Added this checkpoint:

- **Worker wiring** (`app/workers/main.py`, `app/workers/queue.py`): `WhatsAppWebhookJob` with its
  own priority queue (`:whatsapp-webhook`, same priority tier as the Shopify webhook queue, ahead
  of automation and crawl jobs); `run_job`/`job_label` dispatch it; the existing Shopify
  `SecretManager` instance (`runtime.secrets`) is reused for WhatsApp token decryption rather than
  building a second one; `maintain()` now also calls `webhooks.requeue_pending_events`,
  `webhooks.prune_old_events` and `send.cancel_orphaned_sending`.
- **Dashboard API** (`app/schemas/whatsapp.py`, `app/api/integrations/whatsapp.py`,
  `app/api/whatsapp/router.py`, `app/api/webhooks/whatsapp.py`, registered in `app/main.py`):
  connect (manual credential entry, live-verified via `fetch_phone_info` unless the fake provider
  is active) / status / disconnect / auto-reply toggle; read-only conversations and message
  history; template list + sync (refuses while the fake provider is active, never claims a sync
  happened); contact consent get/set/opt-out; a read-only automation-usage summary; the public
  GET/POST webhook route. No access token is ever serialized by any schema.
- **Minimal frontend dashboard** (`frontend/src/app/whatsapp/`): connect form, connection status
  card with sync/auto-reply/disconnect actions, conversation list, conversation detail (message
  history + live consent/suppression panel), template list, automation-usage list. Reused
  `StatusBadge`/`Field`/`FormError`/`SubmitButton` rather than new components; extended
  `StatusBadge`'s shared style map with the new WhatsApp statuses instead of forking it.
- **`backend/tests/whatsapp/`**: 233 tests across phone normalization, consent/suppression,
  window, templates, the send policy gate and idempotency boundary (including a simulated
  retryable-vs-non-retryable provider failure and an exactly-once claim test), the status state
  machine, webhook intake/processing (signature, dedupe, replay, out-of-order status), automation
  integration (`validate_definition`, the `{{generated_text}}` chain, and a crash-recovery test
  mirroring the pre-existing `ai_generate` one), the API layer, and a dedicated adversarial
  security file (token corruption/rotation/missing-key, cross-tenant access, role enforcement,
  opt-out/template/window bypass resistance).
- **`backend/mutation/catalog_6.py`**: 20 mutants covering the state machine, window boundary, the
  policy gate (consent/suppression/connection-status checks, the idempotency claim, the
  retryable-error branch, the idempotency lookup), template approval/variable validation, the
  phone-normalization fail-closed guard, webhook signature verification and duplicate-delivery
  dedupe, the GET handshake, and the automation engine's consent-category validation and
  crash-protection extension. All 20 killed; wired into `mutation/run.py`.
- Extended the pre-existing generic security/architecture test suites rather than writing
  WhatsApp-specific duplicates: `tests/test_tenant_api_matrix.py` (every new route classified in
  the POLICY table -- 473 parametrized cases covering auth/role/tenant-isolation all pass),
  `tests/test_tenant_schema_audit.py` (the `whatsapp_webhook_events` NO_ORG_FK/NULLABLE_ORG
  exception, mirroring Shopify's), `tests/shopify/test_architecture.py` (allowlisted the new
  pure-data `messaging.catalog` import in `core/config.py`).
- Fixed a mutation-catalog collision the new code caused: `sg-10-missing-encryption-key-allowed`
  (Phase 4.1, `catalog_41.py`) matched both the pre-existing Shopify snippet and my new,
  structurally identical WhatsApp one; widened its `old`/`new` strings with Shopify-specific
  context so it targets only its original line. Re-verified: still kills correctly.

## In Progress

None (between checkpoints).

## Remaining

- A proper automated browser E2E spec (Playwright, integrated into `e2e/run.mjs` the way the
  existing Shopify/automation suites are) was NOT built. The dashboard was instead manually driven
  in a real Chrome browser against the real backend/frontend/Postgres this checkpoint (see "Live
  Verification") -- that is real evidence the UI works, but it is not a repeatable, CI-shaped
  artifact the way `e2e/*.spec.js` is for Shopify. Building that is the next concrete piece of
  work if browser E2E coverage needs to be permanent and automated.
- Live Meta verification (a real WABA, a real phone number, a real inbound message/status
  delivery) remains externally blocked -- no credentials exist in this environment.

## Architecture Decisions

Decisions 1-9 from the previous checkpoint are unchanged. Added:

10. **Disconnect does not purge WhatsApp data**, unlike Shopify's disconnect (which purges
    re-importable commerce data). Consent/suppression records and conversation history have no
    external source of truth to re-import from, and a suppressed contact must stay suppressed
    across a disconnect/reconnect cycle -- purging would silently re-expose a contact who opted
    out. Disconnect only clears the stored token and marks the connection released (so the
    `phone_number_id` can be claimed by another organization later, same mechanism as Shopify).
11. **The dashboard has no contacts list page.** Every contact is reachable from its conversation
    (conversation -> `contact_id`), which is sufficient for the "Preferences: opt-in status,
    opt-out, suppression" requirement without adding a `GET /api/whatsapp/contacts` list endpoint
    nobody asked for.
12. **The worker reuses the Shopify runtime's `SecretManager` instance** for WhatsApp token
    decryption rather than constructing a second one. Both integrations share one
    `SECRET_ENCRYPTION_KEY`; building a second manager would be identical state for no benefit.

## Database Migrations

Unchanged from the previous checkpoint: `0008_whatsapp.py`, verified against real PostgreSQL
(upgrade/downgrade/upgrade, `alembic check` clean, the FK-leading-index audit clean after the fix
described there). Re-confirmed this checkpoint via the full `tests/integration/test_migrations.py`
run (below).

## API Changes

New routers, all tenant-scoped via the existing `CurrentOrg`/`require_current_org_role` dependency
chain (owner/admin for every mutating route, any member for reads) -- see "Completed" above for
the full endpoint list. Every one is classified in `tests/test_tenant_api_matrix.py`'s POLICY
table and passes the full generic auth/role/tenant-isolation matrix (473 parametrized cases).

## Provider Changes

Unchanged from the previous checkpoint.

## Webhook Changes

The service logic (previous checkpoint) is now reachable over real HTTP:
`app/api/webhooks/whatsapp.py` implements the GET verification handshake (plain-text echo of
`hub.challenge`, rate-limited on repeated failures) and the POST intake (body-size-capped read,
signature verification, `WebhookRejected` mapped to the right HTTP status), registered in
`app/main.py`. Verified via `TestClient` in `tests/whatsapp/test_api.py`: GET handshake
accept/reject, POST with a real HMAC-SHA256 signature accepted and processed, invalid signature
rejected with 401, oversized body rejected with 413.

## Worker Changes

See "Completed": `WhatsAppWebhookJob` dispatch, its own priority queue tier, and three new
maintenance steps. `app/workers/main.py` and `app/workers/queue.py` changes are additive;
`tests/test_worker_process.py` and the full Shopify test directory were re-run after the change
with zero regressions.

## Frontend Changes

`frontend/src/app/whatsapp/` (page, connect form, connection actions, conversation detail,
consent panel, server actions) plus additive extensions to `lib/types.ts`, `lib/dal.ts`,
`components/status-badge.tsx` and `components/app-header.tsx`. `npm run typecheck`, `npm run
lint`, `npm run build` and `npm test` (15 pre-existing tests, no WhatsApp-specific ones needed --
no new pure-logic helper module was created that would warrant one, unlike `lib/shopify.ts`) all
pass. Manually verified in a real Chrome browser (see "Live Verification"), which caught and fixed
one real UX bug (see "Known Failures").

## Tests

- **New `tests/whatsapp/` suite: 233 passed.**
- **Full non-integration regression** (`pytest -m "not integration"`): **2,456 passed, 2
  skipped, 129 deselected.** (One pre-existing architecture-allowlist test needed a legitimate,
  documented extension -- see "Completed"; zero unexplained failures.)
- **Full integration tier** (`pytest -m integration`, real PostgreSQL + Redis): **128 passed, 1
  skipped.** One real-process failure-injection test
  (`test_postgres_going_away_mid_import_the_worker_survives_and_the_import_is_resumed`) failed
  once under the load of a long combined run and passed cleanly re-run in isolation immediately
  after; it exercises only Shopify sync/crash-recovery code this phase never touched, so this is
  recorded as a pre-existing flaky test under load, not a regression. No other integration test
  failed across two full runs this checkpoint (the second after fixing the FK-index gap found by
  the first).
- **`tests/test_tenant_api_matrix.py` in isolation: 473 passed** (every WhatsApp route added to
  the POLICY table; auth, role and tenant-isolation checked generically for all of them).
- Full-repo static checks: `ruff check`, `ruff format --check`, `mypy app tools` (160 source
  files) all clean. Frontend `tsc --noEmit`, `eslint`, `next build`, `node --test` all clean.
- Migration up/down/up + `alembic check` clean (re-confirmed via the integration run above).

## Security Tests

`tests/whatsapp/test_security.py` (14 tests): corrupted ciphertext fails closed (not a crash);
two connections' tokens cannot decrypt under each other's bound context (cross-tenant, since the
schema allows only one connection per organization); a missing encryption key fails closed;
token rotation round-trips under the new key and the pre-rotation envelope becomes unreadable
once the old key is actually retired (mirrors the existing Shopify key-rotation test pattern); the
access token never appears in a connect/status API response; cross-tenant message/conversation id
guessing returns 404 (never 403, never confirms existence); a cross-tenant consent-set attempt
404s and provably does not mutate the other org's row; disconnect is tenant-scoped; every mutating
route requires OWNER/ADMIN; opt-out cannot be bypassed by an automation retry reusing the same
idempotency key; a free-form text send outside the window cannot be made to bypass the template
requirement under any consent state. Combined with the generic 473-case tenant/role/auth matrix
and the 20/20 killed mutation catalog, this is the adversarial coverage the brief's "Security"
checklist asks for.

## Mutation Tests

`backend/mutation/catalog_6.py`: **20 of 20 killed, 0 survived.** Plus the one pre-existing
collision found and fixed (`sg-10`, re-verified killed). The other 150 pre-existing mutants
(Phase 4/4.1/5) were NOT re-executed this session (their target snippets are untouched and
`mutation.run check` confirms every snippet, old and new, still matches the current codebase
exactly once) -- `mutation.run report` shows 171 of 171 killed, 0 survived, combining this
session's 21 fresh runs with the trusted prior-session cache for the other 150. This is stated
precisely rather than claimed as "re-ran the full suite," per this project's verification
standard.

## Live Verification

No real Meta credentials exist in this environment; live Meta API behavior remains EXTERNALLY
BLOCKED, exactly as Phase 4.1/5 recorded for Shopify/AI. What WAS live-tested this checkpoint,
against real local infrastructure:

- Backend (`uvicorn`) and frontend (`next dev`) started against the real dev PostgreSQL (migrated
  to head `0008`) and the `fake` messaging provider (`WHATSAPP_MESSAGING_PROVIDER=fake`,
  configured via `backend/.env`, gitignored).
- A real account was created through the real signup/onboarding UI in a real Chrome browser (via
  `claude-in-chrome`), then the full `/whatsapp` flow was driven by hand:
  - **Connect**: filled the real form, submitted, landed on `?connected=1` with the connection
    card showing the phone number id, CONNECTED status, timestamps and API version.
  - **Template sync**: clicked "Sync templates" and saw the exact expected refusal ("Template
    sync requires a real WhatsApp connection; the offline/test provider is active") -- confirms
    the 503 path is wired correctly end to end and never fakes a sync.
  - **AI auto-reply toggle**: clicked "Enable AI auto-reply", watched the connection card update
    live to "enabled" and the button flip to "Disable AI auto-reply" without a page reload.
  - **Conversation list and detail**: seeded one realistic conversation (an inbound message, SUPPORT
    consent, one outbound reply via the real `send_text` orchestration against the fake provider)
    directly through the backend's own service functions, then confirmed the dashboard rendered
    it correctly: message bubbles (inbound left, outbound right), correct per-message status
    badges, the customer-service window shown as Open.
  - **Consent/suppression panel**: clicked "Suppress" and watched the suppression badge flip to
    "Yes" live, while the SUPPORT category's "Opted in" status stayed untouched -- confirms the
    suppression-is-independent-of-consent property visibly, not just in a unit test.
  - Confirmed the suppression write actually persisted by querying Postgres directly (not just
    trusting the optimistic client state).
- The dev database and the test account/connection/conversation/messages/consent/suppression rows
  created for this check were deleted afterward (confirmed by direct query), both dev server
  processes were stopped, and the browser tab was closed -- the dev `bizflow` database is empty
  again, matching the "empty between sessions" convention.

## Known Failures

One found and fixed during this checkpoint's live browser verification: the consent panel's
"Suppressed" indicator reused the `OPTED_IN`/`OPTED_OUT` badge styling and labels for a different
boolean (is this contact suppressed at all), which rendered as the confusing "Suppressed: Opted
in" when a contact was NOT suppressed. Fixed to a dedicated Yes/No indicator with its own color
logic; re-verified live in the browser. This is exactly the category of bug code review alone
would not have caught -- a labeling confusion only visible once rendered.

No other failures outstanding. The FK-index gap and the missing ledger-payload column (previous
checkpoint) and the `sg-10` mutation collision and the consent-panel label (this checkpoint) were
all found and fixed before being reported here, consistent with "fix the cause, don't just
report."

## Known Limitations

Unchanged from the previous checkpoint (AI auto-reply is single-turn, not RAG-grounded;
`whatsapp_message_replied` is not implemented; template variables are positional-only;
`WebsiteLead.marketing_consent` is not auto-imported into `WhatsAppConsent`; media is modeled but
never sent/received; no real Shopify/AI/WhatsApp credentials exist in this environment). Added:

- No automated, CI-shaped browser E2E spec exists yet for WhatsApp (see "Remaining"); the
  dashboard's correctness is evidenced by the manual live-browser session recorded above plus
  `TestClient`-level API tests, not a Playwright spec a future run can replay unattended.

## Next Exact Action

Phase 6's own checklist (domain/DB, provider abstraction, connection/token security, webhooks,
outbound messaging, templates/window/consent, automation integration, dashboard, security/mutation
tests, production gate) is complete. If continuing further WhatsApp hardening rather than moving
on: build the automated Playwright E2E spec (extend `e2e/run.mjs` to configure
`WHATSAPP_MESSAGING_PROVIDER=fake` and the dev secret/verify-token env vars, add
`e2e/whatsapp.spec.js` covering connect/status/disconnect, a simulated signed webhook delivery,
and tenant isolation across two browser sessions, mirroring the existing Shopify spec's
structure). Otherwise: await explicit approval before starting Phase 7, per standing instructions.

## How To Resume

1. Read this file, then `git status --short --branch` and `git log --oneline -6` (nothing has
   been committed this phase; everything is working-tree changes only).
2. Docker Desktop must be running; `docker compose up -d db redis` from the repo root starts the
   dev PostgreSQL (`bizflow`, at migration head `0008`, empty) and Redis. Use
   `backend/.venv/Scripts/python.exe`.
3. Re-run `python -m pytest -m "not integration" -q` and `python -m pytest -m integration -q`
   from `backend/` to reconfirm zero regressions before changing anything (both passed clean at
   the end of this checkpoint: 2,456 and 128+1-skipped respectively). `tests/whatsapp/` alone
   (233 tests) runs in under 5 seconds and is the fastest first check.
4. `backend/.env` already has `WHATSAPP_APP_ID` / `WHATSAPP_APP_SECRET` /
   `WHATSAPP_WEBHOOK_VERIFY_TOKEN` / `WHATSAPP_MESSAGING_PROVIDER=fake` dev placeholders (file is
   gitignored) for manual exploration; `backend/mutation/catalog_6.py` lists the 20 Phase 6
   mutants (`python -m mutation.run run wa-` to re-run just those).
5. Live Meta/Shopify/AI checks remain externally blocked; do not claim otherwise. No automated
   browser E2E spec exists for WhatsApp yet -- see "Next Exact Action" if building one.
