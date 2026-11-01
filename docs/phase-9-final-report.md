# Phase 9 Final Report: Omnichannel Customer Support + Email Automation

## 1. Executive summary

Phase 9 adds a unified cross-channel support inbox (WhatsApp/Website/Email), AI/human handoff and
assignment/priority workflow, a customer identity + timeline layer, and a full email subsystem
(provider abstraction, inbound/outbound/threading, templates, automation actions, campaigns,
consent/suppression, one-click unsubscribe) reusing every existing Phase 0-8 system (RAG, AI
provider abstraction, automation engine, job queue, idempotency pattern) with no duplication.
Backend is **IMPLEMENTED and VERIFIED**: full unit suite, Postgres integration tier, mutation
testing (29/29 new mutants killed), lint/type checks, and a new 36-check Playwright E2E suite are
all green against real infrastructure. Live email-provider verification is **EXTERNALLY BLOCKED**
(no real Resend credentials exist in this environment) and is reported as such, not faked. One
real product gap was found during test-writing (the unsubscribe mechanism was modeled but
unreachable) and fixed with explicit user approval mid-session. Nothing has been committed.

## 2. Phase 9 architecture

```
WEBSITE:  public chat -> chat/service.py -> SupportConversation IS authoritative (ai_state)
WHATSAPP: webhook -> messaging/whatsapp/* (Phase 8 state machine, authoritative) -> mirrored
          into SupportConversation via support/conversations.sync_conversation
EMAIL:    webhook (Resend, Svix-signed) -> services/email/* (mirrors whatsapp/* exactly: its
          own EmailThread.ai_state is authoritative) -> mirrored the same way

All three funnel into app/services/support/: identity.py (tenant-scoped Customer matching),
timeline.py (append-only audit ledger), conversations.py (sync_conversation, list_inbox,
priority/assignment/take_over/return_to_ai/request_human channel-dispatch, resolve/reopen),
messages.py (UnifiedMessage normalization), analytics.py (SupportAnalytics).
app/api/support/router.py exposes it (ANY_MEMBER -- everyday support ops, not a managed action).
```

Email mirrors the existing WhatsApp/Shopify provider-abstraction pattern exactly
(catalog/registry/factory/provider/errors) under `app/services/email/`. `send.py` is the one and
only place `EmailProvider.send` is called. No second automation engine, webhook queue, retry
framework, or idempotency system was created -- `send_email`/`send_email_template` are new action
types dispatched by the existing `app/services/automation.py`, and email webhooks reuse the
existing `JobQueue`/worker loop.

## 3. Files changed

~40 new backend modules (`app/services/support/*`, `app/services/email/*`, `app/db/models/
support.py`, `app/db/models/email.py`, `app/api/support/router.py`, `app/api/email/router.py`,
`app/api/integrations/email.py`, `app/api/webhooks/email.py`), one migration
(`0010_support_and_email.py`), ~25 modified backend files (automation.py, chat/service.py,
whatsapp/* for sender attribution + take_over, config.py, startup_checks.py, main.py), ~15 new
frontend routes/components under `frontend/src/app/support/` and `frontend/src/app/email/`, and
~30 new backend test files (`tests/support/*`, `tests/email/*`, `tests/ai/test_email_agent.py`,
`tests/integration/test_support_analytics.py`) plus `e2e/support.spec.js`. Full diff is in the
working tree, uncommitted, for review.

## 4. Database migrations

One migration, `0010_support_and_email.py`: `customers` gained `normalized_email/_phone`,
`status`, `email_marketing_consent`, `email_suppressed(_reason/_at)`; `conversations`/`messages`/
`whatsapp_messages` gained `sender_type`/`sender_user_id`; new tables `support_conversations`,
`support_timeline_events`, `email_connections`, `email_threads`, `email_messages`,
`email_templates`, `email_campaigns`, `email_campaign_recipients`, `email_webhook_events`. Table
creation order sequenced for composite FK dependencies. **VERIFIED** against real Postgres:
upgrade/downgrade/upgrade cycle preserves data, `alembic check` reports no drift,
`tests/test_tenant_schema_audit.py` (194 passed/3 skipped) and `tests/integration/
test_migrations.py` (19 passed) both green. Every tenant table carries `organization_id` with a
composite FK to its parent per the existing convention; audited, no leading-index gaps.

## 5. Customer/contact model

`app/services/support/identity.py`: tenant-scoped matching, fixed priority (Shopify id > email >
phone, never by name), race-safe `get_or_create_customer` (IntegrityError retry), backfill-
without-overwrite semantics. **VERIFIED**: `tests/support/test_identity.py` (8 tests), including a
dedicated tenant-isolation test and a mutation (`p9-06`) proving the email-match branch is actually
org-scoped.

## 6. Unified conversation model

`SupportConversation`: a thin index row kept in sync by each channel's own intake code, never a
rewrite of the channel-specific tables (WhatsAppConversation/EmailThread/Conversation stay the
systems of record for their own channel). `sync_conversation` upserts on
`(organization_id, channel, channel_conversation_id)`, race-safe via IntegrityError retry.
**VERIFIED**: `tests/support/test_conversations.py` (14 tests) + `tests/support/
test_conversations_channel_dispatch.py` (7 tests, WhatsApp/Email channel-dispatch branches
specifically, not just Website) + live in `e2e/support.spec.js`.

## 7. Unified inbox

`list_inbox` (filters: channel/priority/status/assignment), `/support` dashboard shows every
WhatsApp/website/email conversation with channel/priority/status badges and a live analytics
strip. **LIVE-TESTED**: real browser, real backend, all three channels, in `e2e/support.spec.js`
("the website/WhatsApp/email conversation is visible in the unified inbox" -- all passing).

## 8. WhatsApp integration

Reused verbatim: Phase 8's state machine, consent, window, send/webhook pipeline. New for Phase 9:
`sender_type`/`sender_user_id` attribution threaded through `send_text`/`send_template`/
`autoreply.py`, and a genuinely new `handoff.take_over()` (atomic transition from AI_ACTIVE **or**
HUMAN_REQUIRED, unlike Phase 8's `claim()` which only promotes from HUMAN_REQUIRED). **VERIFIED**:
full existing WhatsApp unit/integration suite unchanged and still green (316+ tests), new
`take_over` mutants `p9-28`/`p9-29` killed by new dedicated tests, live in E2E.

## 9. Website integration

`chat/service.py`'s `send_message` now checks `ai_state` before generating (no fake AI reply while
a human is active), syncs into `SupportConversation` on both inbound and AI-reply outbound.
**VERIFIED**: unit tests + live in E2E (website message -> AI auto-reply -> marked AI -> human
takeover -> human reply -> marked HUMAN -> return to AI -> resolve -> reopen, all passing against
the real stack).

## 10. AI/human switching

Fixed state machine per channel (`AI_ACTIVE -> HUMAN_REQUIRED -> HUMAN_ACTIVE -> AI_ACTIVE`),
race-safe atomic transitions (`UPDATE ... WHERE ai_state = ...`), dispatched per channel from
`support/conversations.py` (WhatsApp/Email delegate to their own channel's handoff module; Website
is directly authoritative on `SupportConversation`). **VERIFIED**: unit tests for all three
channels' dispatch branches, mutation-covered (`p9-28`/`p9-29`), **LIVE-TESTED** end to end in the
browser for Website; WhatsApp/Email channel-dispatch verified at the unit level (real
WhatsAppConversation/EmailThread rows, real service calls) since driving those two specific
channels' take-over through the browser would duplicate what Phase 8's own WhatsApp E2E suite and
this session's unit tests already prove.

## 11. Assignment

`claim`/`release`/`assign`, atomic `UPDATE ... WHERE assigned_agent_user_id IS NULL` (two agents
claiming the same conversation: only one wins -- explicit brief scenario). **VERIFIED**: unit test
`test_two_agents_claiming_the_same_conversation_only_one_wins`, mutation `p9-04`/`p9-05`,
**LIVE-TESTED** claim/release round-trip in the browser.

## 12. Priority system

URGENT/HIGH/NORMAL/LOW with `priority_source` (SYSTEM/AI/HUMAN): a human-set priority is never
silently overwritten by AI/system, but a second human can override a human's own choice.
**VERIFIED**: dedicated unit tests, mutation `p9-01`/`p9-02`, **LIVE-TESTED** in the browser
(clicking a priority control updates the badge live, no reload).

## 13. Customer timeline

Append-only `SupportTimelineEvent` ledger (fixed ~23-value event-type vocabulary), backs the
customer timeline, the support audit trail, and the consent/suppression change audit as one
mechanism. **VERIFIED**: `tests/support/test_timeline.py` (7 tests: rejection of unknown event
types, never-self-commits semantics, oversized-payload truncation, ordering/cursor pagination,
tenant isolation), **LIVE-TESTED** (`ul[data-testid="timeline"]` populated in the browser).

## 14. Email provider abstraction

Mirrors WhatsApp/Shopify's catalog/registry/factory/provider/errors pattern exactly. Providers:
`resend` (production-allowed) and `fake` (offline, refused in production). `ResendProvider`'s
request/response shapes were verified against Resend's real published API docs. **IMPLEMENTED,
VERIFIED** at the unit level; real-network verification is externally blocked (section 32).

## 15. Email inbound support

Resend's real webhook carries metadata only; `EmailProvider.fetch_inbound()` is a required
follow-up fetch (mirrors the Shopify "re-fetch by id" precedent). The fake provider's "registered
inbound content" store is **Redis-backed, not an in-process dict** -- found and fixed during E2E
work: the API and worker run as separate OS processes (exactly like production), so an in-process
dict would never be visible across that boundary; this was a latent correctness gap in the fake
provider itself, not just a test convenience. **VERIFIED**: `tests/email/test_webhooks.py` (14
tests: signature, dedup, idempotent reprocessing, unknown-connection handling), **LIVE-TESTED** in
`e2e/support.spec.js` (a real signed inbound webhook, fetched via the Redis-backed fake registry,
creates a real conversation visible in the browser; duplicate delivery proven not to duplicate the
message).

## 16. Email threading

RFC 5322 Message-ID/In-Reply-To/References plus the provider's own thread id where available. A
provider thread id, once seen, always wins; otherwise a reply is matched against stored
`message_id_header`s; a shared sender address alone never merges two threads. **VERIFIED**:
`tests/email/test_threading.py` (9 tests, including explicit "never merges by sender alone" and
tenant-isolation cases).

## 17. Email outbound

`send.py` is the one and only place a provider is called. Fixed policy gate order: rate limit ->
connection connected+enabled -> suppression (universal) -> marketing consent (MARKETING only) ->
**unsubscribe link configured (MARKETING only, new this session)**. Idempotency mirrors
WhatsApp's QUEUED->SENDING claim pattern exactly. **VERIFIED**: `tests/email/test_send.py` (12
tests), mutation `p9-07`/`p9-08`/`p9-09`/`p9-10`, **LIVE-TESTED** (a real automation-triggered send
and a real campaign send both completed against the fake provider in the browser/E2E run).

## 18. Email templates

Bounded `{{dotted.path}}` placeholder syntax only (no loops/conditionals/code execution); an
undeclared variable is rejected at save time; an unknown variable at render time fails safely to
empty, never left literal; HTML-escaped in HTML context, not in plain text. **VERIFIED**:
`tests/email/test_templates.py` (9 tests), **LIVE-TESTED**: create, edit, activate a template
through the real dashboard form in the browser.

## 19. Email automations

`send_email`/`send_email_template` are new automation action types, validated
(subject/content/classification bounds, template-id UUID validity, variable-count bounds) and
dispatched by the existing engine -- no second engine. **VERIFIED**: `tests/email/
test_automation_integration.py` (13 tests: validation + real end-to-end dispatch, including
unresolvable-recipient and no-connection fail-closed cases), mutation `p9-24`-`p9-27`,
**LIVE-TESTED**: a real `send_email` automation workflow, triggered by a real `website_lead_
captured` event from the browser's lead-capture flow, completed and produced a real EmailMessage.

## 20. Email campaigns

Audience resolution is a bounded, deterministic filter (ACTIVE + has email + not suppressed +
marketing consent unless overridden) -- never raw SQL. Idempotency is three-layered: a unique
`(org, campaign, customer)` recipient row, a per-recipient send idempotency key, and the send
policy gate re-checking suppression/consent at actual send time (catches the "unsubscribe while
queued" race, not just audience-resolution-time staleness). Time-sliced batch processing
re-queues itself. **VERIFIED**: `tests/email/test_campaigns.py` (12 tests: duplicate `queue_
campaign` never duplicates a recipient, concurrent batch claim via `skip_locked` never double-
claims, unsubscribe-while-queued caught at send time), mutation `p9-20`/`p9-21`, **LIVE-TESTED**:
a real campaign created, started, and completed against the fake provider in the browser, with a
suppressed contact correctly excluded and a real `email.delivered` status webhook updating a real
message's status, all in `e2e/support.spec.js`.

## 21. Consent/suppression

`email_marketing_consent` (nullable: unknown is never opted-in) and `email_suppressed` (universal
kill switch, independent of consent, gates transactional/lifecycle email too) live directly on
`Customer`. **A real gap was found and fixed this session**: `EmailMessageStatus.UNSUBSCRIBED` was
modeled and handled but unreachable -- no webhook event ever produced it and there was no
unsubscribe link anywhere in the app. With explicit user approval, this was fixed:
- `app/services/email/unsubscribe.py` (new): a stateless HMAC-signed token
  (`"{org_id}:{customer_id}"`, keyed off a value derived from `secret_encryption_key`), no DB
  lookup needed to validate.
- `GET /api/email/unsubscribe?token=...` (new, public): resolves the token, suppresses the
  customer (idempotent), records a timeline event.
- `send.py` now refuses to send ANY marketing email at all when no unsubscribe link is configured
  (fail-closed, never sends with no way to opt out), and auto-appends the link to every marketing
  send's body.
- Resend's own provider-level suppression event (`email.suppressed`) now also normalizes to
  UNSUBSCRIBED, giving a second, provider-reported path to the same state.
**VERIFIED**: `tests/email/test_consent.py` (8 tests), `tests/email/test_unsubscribe.py` (7 tests),
`tests/email/test_unsubscribe_api.py` (6 tests), mutation `p9-11`/`p9-12`/`p9-22`, **LIVE-TESTED**
suppression excluding a contact from a real campaign send in the browser.

## 22. Cross-channel automation

`send_email`/`send_email_template`/`send_whatsapp_text`/`send_whatsapp_template` all dispatch
through the one automation engine from any trigger event. **VERIFIED**: unit + live (section 19).

## 23. Analytics

`SupportAnalytics`: total/open/urgent/high-priority counts, AI-handled vs human-handled (by last
responder), AI handoff count, avg first-response/resolution time (`func.extract("epoch", ...)`,
**Postgres-only** -- deliberately not exercised by the SQLite unit tier), email sent/delivered/
bounced/unsubscribed counts. **VERIFIED**: new `tests/integration/test_support_analytics.py` (6
tests) against real Postgres, including a dedicated real-timing test proving the epoch-extraction
math (not just that it returns *some* number), and a tenant-isolation test. **LIVE-TESTED**: the
`/support` analytics strip renders live counts in the browser.

## 24. Security

Tenant isolation scoped on every query (see 25); authorization via `_require_member` for
assignment/claim (mutation `p9-03`); Svix webhook signature (HMAC-SHA256, 300s replay-skew
tolerance, mutation `p9-16`/`p9-17`); email credentials are environment-based, never stored per-
row; the unsubscribe token's signing key is derived from the existing `secret_encryption_key`, not
a new stored secret; all inbound email HTML is sanitized once at ingestion (`nh3` allowlist) and
the raw payload is never persisted; `sanitize_header` strips `\r`/`\n` before any header-bound
field. `ruff check`, `ruff format --check`, and `mypy app tools` all report clean (34 real type
errors found and fixed this session, mostly variable-reuse-across-branches type confusion in
`automation.py`/`support/conversations.py`/`support/messages.py` -- harmless at runtime since the
branches are mutually exclusive, but real static-analysis gaps, now named distinctly).

## 25. Tenant isolation

Every new tenant table carries `organization_id` with a composite FK to its parent; every new
service function scopes its queries by `organization_id`. **VERIFIED**: `tests/test_tenant_schema_
audit.py` (194 passed), `tests/test_tenant_api_matrix.py` (891 passed, every new Phase 9 route
classified and role/auth-checked, including the new public unsubscribe endpoint), dedicated unit
tests (`test_identity_matching_never_crosses_tenants`, `test_customer_timeline_never_crosses_
tenants`, `test_resolve_inbound_thread_never_crosses_tenants`, `test_messages_never_cross_
tenants`, `test_postgres_analytics_never_crosses_tenants`), **LIVE-TESTED**: a second organization
gets a 404 (never 403, never confirms existence) for another org's conversation/template/campaign
id, both via the browser flow and direct API calls, in `e2e/support.spec.js`.

## 26. Failure injection

**Covered**: worker-killed-mid-processing is structurally the same crash-recovery pattern Phase 8
already has a dedicated integration test for (`tests/integration/test_worker_crash_recovery.py`,
unchanged, still green against Phase 9's changes); webhook dedup under replay (unit-tested, section
15); out-of-order/duplicate status webhooks (rank-checked, section 17, mutation `p9-13`-`p9-15`);
duplicate campaign execution and concurrent batch claim (section 20); unsubscribe/suppression
racing a queued send (section 20/21); automation retry/backoff (reuses Phase 4-8's unchanged
machinery, re-verified via `tests/integration/test_automation_postgres.py`, still green).
**NOT separately re-implemented for Phase 9**: a dedicated integration-tier "worker killed mid-
email-send" test distinct from the existing WhatsApp one -- `email/send.py` and `email/webhooks.py`
mirror that exact pattern (same QUEUED/SENDING claim, same crash-leaves-SENDING-not-retried
contract) byte-for-byte in design, and this is the one item in this report accepted as "covered by
structural equivalence, not an independent test," rather than re-run.

## 27. Mutation results

`backend/mutation/catalog_9.py`: 29 new mutants covering human-priority override, assignment
race-safety, authorization, tenant-scoped identity matching, email suppression/consent/
unsubscribe-gate, the unsubscribe token's own signature check, email status rank-checking, webhook
signature verification and dedup, campaign idempotency, AI/human/system sender attribution, and
automation action validation. **Result: 29/29 killed, 0 survivors.** Two pre-existing catalog/
source mismatches from earlier Phase 9 work (unrelated to this session's new code) were found and
fixed in the same pass: `catalog_6.py`'s `EXTERNAL_CALL_ACTION_TYPES` snippet (stale after
`send_email`/`send_email_template` were added) and `catalog_8.py`'s handoff rowcount-check
snippet (became non-unique after WhatsApp's new `take_over()` was added) -- both re-verified
killed after the fix. Full catalog (238 mutants, Phase 4 through 9): `check` reports all unique.

## 28. Playwright results

New `e2e/support.spec.js`: **36/36 checks passing** against the real stack (real Postgres/Redis in
Docker, real uvicorn API, real worker, real Next.js dev server, fake WhatsApp/email providers).
Covers: dashboard load, all three channels visible in the unified inbox, customer timeline,
priority change, claim/release, take-over, AI-attributed reply, human-attributed reply, return-to-
AI, resolve, reopen, template create/edit/activate, campaign create/start/complete, marketing
suppression, inbound email webhook duplicate-delivery dedup, a real delivery-status webhook
update, a real cross-channel automation send, and cross-tenant isolation (conversation/template/
campaign). All 6 pre-existing offline suites (auth, security, layout, shopify, automations,
whatsapp) re-run alongside it and remain green -- **zero regressions**. Infrastructure fixes made
to get here (documented since they're reusable beyond this one suite): `EMAIL_PROVIDER=fake`/
`EMAIL_WEBHOOK_SECRET`/`EMAIL_UNSUBSCRIBE_BASE_URL` wired into `e2e/run.mjs`'s backend env; a
`cfg.redisSet` helper added to `e2e/lib/config.js` so the E2E process can seed the (now Redis-
backed) fake email provider's shared inbound registry.

## 29. Backend test results

Full non-integration (SQLite) unit tier: **3207 passed, 13 skipped**, 0 failed. New Phase 9
coverage within that: `tests/support/` 27 tests, `tests/email/` 125 tests, `tests/ai/
test_email_agent.py` 11 tests -- all passing, zero regressions against Phase 0-8.1 (the pre-
existing 3164-ish baseline this session started from).

## 30. Integration results

Full Postgres+Redis integration tier: **141 passed, 1 skipped**, 0 failed. Includes the existing
Phase 0-8 integration suite unchanged (`test_migrations.py`, `test_worker_crash_recovery.py`,
`test_automation_postgres.py`) plus the new `tests/integration/test_support_analytics.py`.

## 31. Frontend results

`npm run typecheck`: clean. `npm run lint`: clean. `npm run build`: succeeds, all 26 routes
compile (Partial Prerender/static/dynamic as expected), zero errors. All re-run fresh this session
after every backend change (frontend source itself was not modified this session; these confirm no
drift).

## 32. Live provider verification

**EXTERNALLY BLOCKED.** `tests/email/test_live_email_verification.py` (new, mirrors `tests/ai/
test_live_ai_verification.py`'s gating exactly): gated behind `LIVE_EMAIL_TESTS=1`; when enabled in
this environment, all 3 tests report "LIVE EMAIL VERIFICATION = EXTERNAL BLOCKER" via `pytest.
skip` (no real `EMAIL_API_KEY`/non-fake `EMAIL_PROVIDER` exists here) -- confirmed by actually
running it with the flag set. This is an honest report of an environment limitation, not a fake
success and not a test failure.

## 33. Phase 8 regression results

The full existing WhatsApp/automation/chat unit suite (316+ tests covering RAG grounding,
confidence, handoff, suppression, opt-out, customer-service window, idempotency, multilingual
human-request detection, prompt injection, tenant isolation) ran unchanged as part of the 3207-
passed full suite in section 29 -- zero weakening from Phase 9's changes. `tests/integration/
test_worker_crash_recovery.py` and `tests/integration/test_automation_postgres.py` (real killed
worker processes, real Postgres) both green in section 30. The `whatsapp` E2E suite re-ran
alongside the new `support` suite in section 28 and is still green.

## 34. Known limitations

- No live/real-time polling of an in-progress campaign's send counts in the UI -- a deliberate
  scope decision (the brief explicitly says "do not overbuild"); a page reload shows current state.
- `SupportAnalytics`'s first-response/resolution timing fields are Postgres-only by construction
  (`func.extract`); this is intentional, not a gap, but means they are untestable on SQLite.
- Email's "worker killed mid-send" path is covered by structural equivalence to WhatsApp's
  dedicated crash-recovery test (section 26), not an independent integration test of its own.
- The one-click unsubscribe link's confirmation page is a minimal inline HTML response from the
  backend, not a page in the Next.js frontend -- intentional (it's meant to be opened directly from
  an email client, not routed through the dashboard SPA).

## 35. Unresolved blockers

None that block calling Phase 9 complete for everything reachable without real Resend
credentials. The only blocker is external (section 32): no real email provider credentials exist
in this environment, so the actual Resend API has never been called. Everything else -- including
every code path a real Resend account would exercise (send, inbound fetch, webhook signature,
status transitions) -- is implemented and verified against the fake provider plus unit-level
verification of the Resend adapter's real request/response shapes (checked against Resend's
published API docs).

## 36. Exact final verdict

**PHASE 9: COMPLETE / PASS**

(Backend, database, AI layer, automation engine integration, API, frontend, and E2E are all
implemented and verified against real infrastructure. The sole gap -- live Resend verification --
is an external credential blocker, explicitly reported as such per section 32, not a defect in
this phase's work.)

Do not start Phase 10. Nothing in this phase has been committed to git.
