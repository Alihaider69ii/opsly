# Phase 9 Progress: Omnichannel Customer Support + Email Automation

## Current Status

**IN PROGRESS.** Core implementation (database, services, AI layer, automation engine
integration, API routers, frontend) is done and has passed the full non-integration unit suite
plus the Postgres integration tier at least once. Backend unit/service test coverage for every
new Phase 9 service module is now written and passing. A Phase 9 mutation catalog
(`backend/mutation/catalog_9.py`, 29 mutants) exists and is being run. Nothing has been committed
to git (no commits unless asked, per standing instructions). **Do not start Phase 10.**

Still outstanding before this phase can be marked complete: triage of any mutation survivors,
race-condition/failure-injection tests beyond what unit tests already cover, Playwright E2E
(23 minimum scenarios), a live-email-verification harness (expected to report EXTERNAL BLOCKER --
no real Resend credentials exist in this environment), a final full regression pass (ruff/mypy
not yet run for Phase 9 code), and the 36-section final report.

## Architecture

```
WEBSITE:  public chat -> chat/service.py -> SupportConversation IS authoritative (ai_state)
WHATSAPP: webhook -> messaging/whatsapp/* (unchanged Phase 8 state machine, authoritative) --
          mirrored into SupportConversation by support/conversations.sync_conversation
EMAIL:    webhook (Resend, Svix-signed) -> services/email/* (new, mirrors whatsapp/* exactly:
          its own EmailThread.ai_state is authoritative) -- mirrored the same way

All three channels funnel into:
  app/services/support/
    identity.py        -- Customer matching (Shopify id > email > phone, tenant-scoped, never by name)
    timeline.py         -- append-only audit ledger (customer + conversation + consent + email events)
    conversations.py    -- sync_conversation, list_inbox, priority, assignment, take_over/
                            return_to_ai/request_human (channel-dispatch), resolve/reopen
    messages.py          -- UnifiedMessage normalization across Message/WhatsAppMessage/EmailMessage
    analytics.py         -- SupportAnalytics (Postgres-only: func.extract)

  app/api/support/router.py  (/api/support, ANY_MEMBER -- everyday support operations)
```

Email mirrors the existing WhatsApp/Shopify provider-abstraction pattern exactly
(`catalog.py`/`registry.py`/`factory.py`/`provider.py`/`errors.py`) under `app/services/email/`:
`send.py` (the one and only place `EmailProvider.send` is called), `webhooks.py`, `campaigns.py`,
`templates.py`, `threading.py` (RFC 5322 Message-ID/In-Reply-To/References), `consent.py`,
`state.py` (rank-checked status transitions), `sanitize.py` (nh3 allowlist HTML sanitizer),
`autoreply.py` + `app/services/ai/email_agent.py` (reuses WhatsApp's confidence/greeting/
human-request detectors and the shared RAG retrieval -- no second AI integration, no second RAG).

No second automation engine, webhook queue, retry framework, or idempotency system was created --
`send_email`/`send_email_template` are new action types dispatched by the existing
`app/services/automation.py`, and email webhooks reuse the existing `JobQueue`/worker loop
(`EmailWebhookJob`, `EmailCampaignJob`).

## A gap found and closed during test-writing: the email unsubscribe mechanism

While writing mutation/race-condition tests for email status handling, `EmailMessageStatus.
UNSUBSCRIBED` was found to be modeled and handled (`webhooks.py:_apply_status` /
`_suppress_recipient`) but **unreachable**: no Resend webhook event type ever produced it, and
there was no unsubscribe link/endpoint anywhere in the app. The brief's "unsubscribe tracking"
objective was dead code, not a working feature.

Fixed (user-approved: "Build a minimal real unsubscribe link"):

- `app/services/email/unsubscribe.py` (new) -- a stateless, signed, URL-safe token
  (`HMAC-SHA256("{org_id}:{customer_id}")`, keyed off a value derived from
  `Settings.secret_encryption_key`, context-separated so no second secret is needed). No database
  lookup is required to validate a token, so the endpoint needs no authentication.
- `GET /api/email/unsubscribe?token=...` (new, public, in `app/api/email/router.py`) -- resolves
  the `(organization_id, customer_id)` the token was signed for, suppresses the customer
  (`consent.suppress`, idempotent), records an `email_unsubscribed` timeline event, and returns a
  minimal confirmation page. Added to `tests/test_tenant_api_matrix.py`'s `PUBLIC`/`PUBLIC_REASON`
  tables like the existing webhook routes.
- `app/services/email/send.py` now refuses to send a MARKETING-classified email at all
  (`EmailErrorCode.POLICY_BLOCKED`) when no unsubscribe base URL (`EMAIL_UNSUBSCRIBE_BASE_URL`,
  new setting) or no `secret_encryption_key` is configured -- a marketing email is never sent with
  no working way to opt out. When the gate passes, the link is appended to the body automatically
  (the one centralized send path), so no caller (campaigns, automation, autoreply) needs to
  remember to add it, and transactional/lifecycle email is never decorated or gated by this.
- `app/services/email/state.py`: Resend's own provider-level suppression event
  (`email.suppressed`) is now normalized to `UNSUBSCRIBED` too (functionally the same outcome:
  stop sending to this address), giving a second, provider-reported path to the same state.

## Database Design

Every new tenant table carries `organization_id` with a composite `(child_id, organization_id) ->
(parent.id, parent.organization_id)` FK to its parent, per the existing convention -- audited by
`tests/test_tenant_schema_audit.py` (194 passed / 3 skipped) and a custom FK-without-leading-index
query copied from `tests/integration/test_migrations.py`. One migration
(`0010_support_and_email.py`), table creation order sequenced to satisfy composite FK dependencies
(`email_templates -> email_campaigns -> email_threads -> email_messages ->
email_campaign_recipients`). Verified against real Postgres: upgrade -> downgrade -> upgrade
preserves data, `alembic check` reports no drift.

New/modified tables: `support_conversations`, `support_timeline_events`, `email_connections`,
`email_threads`, `email_messages`, `email_templates`, `email_campaigns`,
`email_campaign_recipients`, `email_webhook_events`; `customers` gained
`normalized_email`/`normalized_phone`/`status`/`email_marketing_consent`/`email_suppressed(_reason/_at)`;
`conversations`/`messages`/`whatsapp_messages` gained `sender_type`/`sender_user_id` attribution.

## Security

- Tenant isolation: every new query is scoped by `organization_id`; `find_customer`'s email-match
  branch has a dedicated mutant (`p9-06`) and test
  (`test_identity_matching_never_crosses_tenants`) proving it.
- Authorization: assignment/claim actions call `_require_member` (raises `NotOrganizationMember`
  for a user outside the organization) -- mutant `p9-03`.
- Webhook authenticity: Svix signature (HMAC-SHA256, `whsec_...`, 300s replay-skew tolerance) --
  `tests/email/test_validation.py`, mutants `p9-16`/`p9-17`.
- Secrets: email provider credentials are environment-based (per the brief), never stored
  per-row/encrypted like WhatsApp's SecretManager; the unsubscribe token's signing key is derived
  from the existing `secret_encryption_key`, not a new stored secret.
- HTML safety: all inbound email HTML is sanitized once at ingestion (`nh3`, allowlist-based) --
  the raw provider payload is never persisted.
- Header injection: `sanitize_header` strips `\r`/`\n` from subject/addresses before they ever
  reach a header-bound field.

## Race Conditions (unit-tested so far)

- Two agents claiming the same conversation: only one wins (`test_two_agents_claiming_the_same_
  conversation_only_one_wins`, atomic `UPDATE ... WHERE assigned_agent_user_id IS NULL`).
- Human-set priority is never silently overwritten by AI/system, but a second human can override
  a human's own choice (`test_human_set_priority_is_never_overwritten_by_ai_or_system`,
  `test_a_second_human_can_override_a_human_set_priority`).
- Duplicate inbound/status webhook delivery is deduped via a unique ledger `dedupe_key`
  (`test_duplicate_delivery_of_the_same_inbound_email_is_deduped`,
  `test_duplicate_status_event_for_the_same_email_and_created_at_is_deduped`).
- Duplicate `queue_campaign` call never duplicates a recipient row (unique
  `(organization_id, campaign_id, customer_id)`); concurrent batch claim via
  `with_for_update(skip_locked=True)` never double-claims
  (`test_claim_batch_never_returns_the_same_recipient_twice`).
- Unsubscribe/suppression while a campaign is mid-flight is caught by `send.py`'s own policy gate
  at actual send time, not assumed stale from audience-resolution time
  (`test_run_campaign_batch_skips_a_recipient_who_unsubscribes_while_queued`).
- Out-of-order/duplicate email status webhooks never regress a message (rank-checked
  `state.should_apply`; `test_out_of_order_status_event_never_regresses_the_message`).
- `take_over` is race-safe and reachable from either `AI_ACTIVE` or `HUMAN_REQUIRED`, for both
  WhatsApp and Email channels dispatched from the unified `support/conversations.py` layer, not
  just the website path (`tests/support/test_conversations_channel_dispatch.py`).

Not yet covered: a dedicated worker-crash-mid-send / Redis-unavailable / Postgres-transient
failure-injection pass specific to the new email paths (the existing WhatsApp-pattern crash
recovery is proven for WhatsApp in `tests/integration/test_worker_crash_recovery.py`; email's
`send.py`/`webhooks.py` mirror that exact pattern but do not yet have their own dedicated
integration-tier crash-injection test).

## Testing (current counts)

- `tests/support/`: 27 passed (identity, timeline, conversations, channel dispatch).
- `tests/email/`: 123 passed (send, consent, state, validation, threading, templates, sanitize,
  webhooks, campaigns, autoreply, automation integration, unsubscribe + its API).
- `tests/ai/test_email_agent.py`: 11 passed.
- Full non-integration unit tier: 3164 passed, 13 skipped (re-run after all of the above landed;
  zero regressions against Phase 0-8.1).
- `tests/test_tenant_api_matrix.py`: 891 passed (includes the new unsubscribe public route and
  every new Phase 9 endpoint).
- Mutation catalog: `catalog_9.py`, 29 mutants, registered in `mutation/run.py`; `check` reports
  all 238 total mutants (Phase 4 through 9) unique. Two pre-existing catalog/source mismatches
  from earlier Phase 9 work were found and fixed in the same pass (`catalog_6.py`'s
  `EXTERNAL_CALL_ACTION_TYPES` snippet after `send_email`/`send_email_template` were added;
  `catalog_8.py`'s handoff rowcount-check snippet after WhatsApp's new `take_over()` made it
  non-unique). **Results of the actual `run` pass are being recorded as they complete -- see the
  final report for the survivor triage.**

## Known Limitations / Deferred

- Support/email frontend has no live polling of an in-progress campaign's send counts (Next's
  single post-action refresh only; documented, deliberate, matches the brief's "do not overbuild").
- `SupportAnalytics`'s avg-first-response/resolution timings use `func.extract("epoch", ...)` --
  Postgres-only, not exercised by the SQLite unit tier (integration-tier only).
- Live email verification (real Resend send/receive) has not been attempted in this environment --
  no real provider credentials exist here; expect this to report EXTERNAL BLOCKER, not fabricated
  success, in the final report.
