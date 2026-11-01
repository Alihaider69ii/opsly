# WhatsApp integration (Phase 6, hardened in Phase 7)

A merchant connects one WhatsApp Business phone number (manual credential entry, not OAuth) so
automations can message customers and customers can message the business back. Every outbound send
goes through a deterministic policy gate (consent, suppression, the customer-service window,
template approval) before any provider call; every inbound delivery is deduplicated and processed
by the worker, never inline in the webhook request.

```
Dashboard "Connect"                                          Meta (Graph API)
  POST /connect  -------------------------------------->  (fake provider: no call at all)
  <-- phone_number_id, display name, quality rating                |
                                                                     v
Automation engine                                          Webhook: GET verify / POST deliver
  send_whatsapp_text / send_whatsapp_template                      |
     -> policy gate (rate limit, connection, suppression,          v
        consent, window/template, variables)              app/api/webhooks/whatsapp.py
     -> MessagingProvider.send_* (whatsapp_cloud | fake)      verify signature -> parse -> dedupe
                                                               -> ledger row -> enqueue -> 200
Worker (python -m app.workers.main)  <-- Redis job
   process_event: message -> contact/conversation/message, automation event, optional AI auto-reply
                  status  -> rank-checked state transition (never regresses)
```

## What is implemented, verified, live-tested and blocked

| Area | Status |
| --- | --- |
| Domain model, provider abstraction, policy engine (consent/suppression/window/templates), webhooks, send orchestration, automation integration, dashboard API/UI | **IMPLEMENTED** and **VERIFIED**: 157 unit/provider tests (`backend/tests/whatsapp/`), 28 mutation mutants (20 Phase 6 + 8 Phase 7, 28/28 killed), the full generic tenant/auth matrix (473 cases), a dedicated adversarial security suite. |
| Automated browser E2E (connect, status, template-sync refusal, signed webhook + duplicate delivery, AI auto-reply toggle, consent/suppression, a minimal automation workflow success/suppressed/invalid-template/valid-template path, disconnect data retention, cross-tenant isolation) | **IMPLEMENTED** and **VERIFIED**: `e2e/whatsapp.spec.js`, 29 checks, offline (`WHATSAPP_MESSAGING_PROVIDER=fake`), part of `cd e2e && npm test`. |
| Real-process failure injection: worker hard-killed before a send reaches the provider, a real severed-and-restored PostgreSQL connection during the same send, a duplicate webhook processed twice by a real worker, a real (unreachable) Redis client during webhook intake | **IMPLEMENTED** and **VERIFIED**: `backend/tests/integration/test_worker_crash_recovery.py` (4 new Phase 7 tests, real `python -m app.workers.main` processes, real PostgreSQL/Redis). |
| Error classification against Meta's documented error codes (not just HTTP status) | **IMPLEMENTED** and **VERIFIED** (Phase 7): see "Verified against Meta's official documentation" below. |
| Per-organization send rate limit (`WHATSAPP_SEND_RATE_LIMIT`) | **IMPLEMENTED** and **VERIFIED** (Phase 7) -- see "Known limitations" for the Phase 6 gap this closed. |
| The same against a **real WhatsApp Business Account and phone number** | **EXTERNALLY BLOCKED**: no Meta App, WABA or phone number exists in this environment. Nothing in this document, or in `docs/phase-7-progress.md`, claims this was done. |
| Real inbound webhook deliveries from Meta to a public HTTPS URL | **EXTERNALLY BLOCKED** (needs a connected number and a public URL). |
| A real approved template sent through Meta | **EXTERNALLY BLOCKED**. Template *validation* (positional variable count/content, APPROVED-only) is fully implemented and tested against directly-seeded template rows; only the live Meta round trip (create a template, get it approved, send it) is blocked. |
| Named-parameter templates (`{{first_name}}`-style) | **NOT YET IMPLEMENTED** -- a deliberate scope decision, not a bug. See "Known limitations". |
| RAG-grounded, multi-turn WhatsApp AI conversation, confidence/fallback, human handoff | **NOT IN SCOPE** for Phase 6/7 -- reserved for the future Phase 8 AI Conversation Layer. The current AI auto-reply is intentionally single-turn, ungrounded, and gated behind a per-organization off-by-default switch. |

## Verified against Meta's official documentation (live-fetched, October 2026)

Re-checked in Phase 7 against `developers.facebook.com` (every point below cites its source page;
see the research notes referenced from `docs/phase-7-progress.md` for the full citation list).

- **Endpoint**: `POST https://graph.facebook.com/v{VERSION}/{PHONE_NUMBER_ID}/messages`, bearer
  token. The WABA id is used only for account-level operations (e.g. listing phone numbers,
  fetching templates), never in the send path.
- **Current API version**: Meta's own live example used `v26.0`; the deployment default was bumped
  from the Phase 6 value `v21.0` accordingly (`whatsapp_api_version` in `core/config.py`). This is
  only the default for a *new* connection -- an existing one keeps the version it was created with.
- **Text/template payload shapes** (`messaging_product`, `recipient_type`, `to`, `type`,
  `text.body`/`preview_url`; `template.name`, `template.language.code`,
  `template.components[].parameters[]`) match what `providers/whatsapp_cloud.py` already sent.
- **Template parameter formats**: Meta currently documents **two** valid formats, chosen per
  template at creation time (`parameter_format`): **positional** (`{{1}}`, `{{2}}`, array-order, no
  name field -- what this integration supports) and **named** (`{{first_name}}`-style, a
  `parameter_name` field, order-independent). Both are current, real Meta features. This
  integration deliberately supports only positional templates; a named-format template is rejected
  rather than guessed at (see `services/messaging/whatsapp/templates.py`). The Phase 6 docstring
  wording implying named placeholders are themselves invalid was corrected in Phase 7 -- they are
  valid on Meta's side, just not yet implemented here.
- **Error codes are not reliably distinguishable by HTTP status alone.** Meta documents dozens of
  WhatsApp-specific `error.code` values and returns most of them -- a permanent validation failure
  exactly like a transient throughput error -- as plain HTTP 400. `providers/_http.py`'s
  `classify_http_status` now reads `error.code` from the response body (mirroring the pattern
  already used by `app/services/ai/errors.py` for the AI provider family) before falling back to
  status-based classification:
  - Auth (non-retryable): `0`, `3`, `10`, `190`, `200`.
  - Rate limit / throughput (retryable): `4` (app rate limit), `80007` (WABA rate limit), `130429`
    (Cloud API message throughput reached), `131056` (too many messages to one recipient).
  - Transient (retryable): `1`, `2` (Meta's own "invalid request / possible server error" and
    "temporary service issue" codes).
  - Everything else documented (`100`, `131008`, `131009`, `132000`, `132001`, `132012` --
    parameter/template validation; `131047` the 24-hour window; `131049`, `131050`, `131026`,
    `131021` -- permanent delivery blocks) falls through to the existing status-based
    classification, which is already correct for all of them (a plain 400 -> `INVALID_REQUEST`,
    never retried).
  Source: `https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes`.
- **Webhook signature**: `X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body, keyed with
  the Meta App secret>` -- confirmed unchanged, matches the existing implementation exactly.
- **Webhook delivery is at-least-once and unordered, by Meta's own documentation**: "Meta retries
  delivery with decreasing frequency... for up to 7 days" on anything but a 200, and "these retries
  can result in duplicate webhook notifications." No ordering guarantee is documented. This
  directly supports (rather than merely motivates) the existing content-derived dedupe ledger and
  the rank-checked status state machine that never regresses a message on an out-of-order or
  duplicate delivery.
- **A `"played"` status value exists** (voice messages) alongside sent/delivered/read/failed. The
  state machine already treats any unrecognized status as informational-only (ignored, never an
  error) -- confirmed still correct, and now documented as a deliberate forward-compatibility
  property rather than an oversight (see `services/messaging/whatsapp/state.py`).
- **A failed status's `errors[]` array has a confirmed shape** (`code`, `title`, `message`,
  `error_data.details`, `href`). Phase 7 now captures `errors[0].code` from a `"failed"` status
  webhook and stores it as the message's `error_code` (`meta_<code>`) instead of a generic
  placeholder, when Meta supplies one -- see "Message status visibility" below.
- **Idempotency: Meta documents none, for sends or for webhooks.** The send-messages guide
  contains no mention of an idempotency key or deduplication guarantee for
  `POST /{phone-number-id}/messages`. This integration's "idempotency boundary" (a `WhatsAppMessage`
  row created and committed before any provider call, claimed atomically immediately before it) is
  therefore an internal safety property -- it prevents *this system's own* retries from double
  sending -- never a claim that Meta itself deduplicates identical requests. If a crash happens
  between a successful Meta call and persisting that result, a true double-send to Meta remains
  possible; this is stated here exactly as plainly as it was in Phase 6, now with Meta's own silence
  on the topic as direct confirming evidence rather than an assumption.
- **Rate limits**: default **80 messages/second per phone number** (upgradeable to 1,000 mps);
  separate business-initiated-conversation daily tiers (1,000 -> 10,000 -> 100,000 -> unlimited,
  auto-upgraded on quality/volume). **No `Retry-After` mechanism is documented** for these --
  `services/messaging/providers/_http.py` still honors a `Retry-After` header when a response
  happens to carry one (a reasonable, generic HTTP convention to respect), but this is this
  system's own backoff policy, not something Meta's docs promise.
- **The 24-hour customer-service window** is expressed through pricing/conversation categories
  (`authentication`, `authentication_international`, `marketing`, `marketing_lite`,
  `referral_conversion`, `service`, `utility`), not the older `business_initiated`/
  `user_initiated` framing. This integration never parses or validates against that enum (it tracks
  the window itself from `last_inbound_at` + a configurable duration), so there was nothing to
  correct in the code -- confirmed by grep, not merely assumed.
- **Not independently re-confirmed this session** (documented as such, not guessed): the exact
  plain-text/status-code wording of the GET verification handshake response, and the generic Graph
  API top-level error envelope fields (`error.type`, `error.subcode`, `error.fbtrace_id`) beyond
  `error.code`, which this integration does not currently read.

## Message status visibility

Dashboard-visible statuses: `QUEUED`, `SENDING`, `SENT`, `DELIVERED`, `READ`, `FAILED`,
`CANCELLED`, `RECEIVED` (inbound only). A `FAILED` message's `error_code` is a fixed internal code
(`opted_out`, `window_closed`, `policy_blocked`, `invalid_request`, `rate_limited`,
`authentication_error`, `provider_unavailable`, `unknown_provider_error`, `worker_lost`) or, when
the failure was reported by a status webhook with a documented Meta error code, `meta_<code>`
(e.g. `meta_131026`). **The dashboard shows this raw code, not a translated sentence** -- this
matches the existing, repo-wide convention (the automations execution detail page does the same for
its own `error_code`) rather than introducing a new, WhatsApp-only translation layer; adding one
project-wide is a legitimate future UX improvement but out of scope for Phase 7 to invent
unilaterally for one feature. The code is never a raw provider payload or secret.

## Reliability

- **Retry classification**: `RATE_LIMITED` and `PROVIDER_UNAVAILABLE` are retryable (exponential
  backoff in the automation engine, capped); everything else -- every policy block and every
  permanent provider validation error -- is not, and is never blindly retried.
- **Per-organization send rate limit** (Phase 7): `WHATSAPP_SEND_RATE_LIMIT` per
  `WHATSAPP_SEND_RATE_WINDOW_SECONDS` (defaults 60/60s), enforced in
  `services/messaging/whatsapp/send.py`'s policy gate before any other check, using the same
  Redis-backed (falling back to in-process) limiter as the rest of the app. **Phase 6 gap closed in
  Phase 7**: the setting existed in `core/config.py` but was never actually read or enforced
  anywhere in the send path; a runaway workflow retry loop could previously generate unbounded
  WhatsApp traffic. Fixed, tested (`tests/whatsapp/test_send.py`), and mutation-covered
  (`wa7-01`, `wa7-02`).
- **Idempotency boundary**: see "Verified against Meta's official documentation" above -- internal
  only, protects against this system's own retries, not a Meta guarantee.
- **Webhook ordering/duplication**: the status state machine's rank check
  (`services/messaging/whatsapp/state.py`) never regresses a message on an out-of-order or
  duplicate delivery; the content-derived dedupe ledger (`whatsapp_webhook_events`) never creates a
  duplicate message or re-runs an automation side effect for the same delivery. Verified at the
  unit level (Phase 6) and, in Phase 7, by a real worker process consuming a real duplicate
  delivery end to end (see `docs/phase-7-progress.md`).
- **Worker crash safety**: a `send_whatsapp_text`/`send_whatsapp_template` action is an
  `EXTERNAL_CALL_ACTION_TYPE`; if the worker dies (or loses its PostgreSQL connection) while that
  step is RUNNING, crash recovery fails the execution outright (`worker_lost_during_whatsapp_send`)
  rather than silently retrying a call that may have already reached a real recipient. If the crash
  happens before the message row itself ever committed (the common case -- see the Phase 7
  real-process tests), nothing was ever sent and a surviving worker's own retry completes it exactly
  once, using the same per-step idempotency key.

## Known limitations

- Named-parameter templates (`{{first_name}}`-style) are not supported -- a real, current Meta
  feature deliberately not implemented yet (see above), not a bug.
- `message_status` (`accepted` | `held_for_quality_assessment` | `paused`) on a successful send
  response is not read; this integration records SENT for any acceptance, which is not incorrect
  (acceptance did occur) but is less informative than it could be for a held/paused message. Noted
  as a found-but-deferred gap rather than silently ignored; implementing it would add a new message
  state and is a bigger change than Phase 7's reliability-hardening scope calls for.
  `conversation.id`/`origin.type` and `pricing` on a status webhook are likewise not read (not
  needed for anything this integration currently does).
  `message_templates` list pagination's cursor shape, the exact `quality_rating` field shape, and
  the mark-as-read request shape remain not independently re-verified (unchanged from Phase 6).
- Dashboard-driven, ad hoc WhatsApp sends do not exist (every outbound message is created by the
  automation engine) -- an unchanged Phase 6 scope decision, not a Phase 7 gap.
- There is no dashboard or API path to lift a suppression once set (`lift_suppression` exists in
  `services/messaging/whatsapp/consent.py` but is not wired to any route) -- confirmed by grep
  while building the Phase 7 E2E suite. Objective 1's "restore eligible state where supported" is
  therefore genuinely not supported today; this is an inherited Phase 6 gap, surfaced explicitly
  here rather than quietly worked around.
- AI auto-reply remains single-turn, ungrounded, off by default -- unchanged, and deliberately not
  expanded in Phase 7 (see `docs/phase-7-progress.md` "Phase 8 Handoff").

## Live verification runbook (when real credentials exist)

No real Meta App, WABA, phone number or test template exists in this environment. When they do:

1. Set `WHATSAPP_MESSAGING_PROVIDER=whatsapp_cloud`, `WHATSAPP_APP_ID`, `WHATSAPP_APP_SECRET`,
   `WHATSAPP_WEBHOOK_VERIFY_TOKEN` from the Meta App dashboard.
2. Connect a real phone number through the dashboard (`phone_number_id`, WABA id, a permanent
   System User access token) -- `connect` live-verifies it via `fetch_phone_info`.
3. Configure the webhook URL in the Meta App dashboard; send a real inbound message and confirm it
   appears in the conversation; confirm a real delivery/read status updates the sent message.
4. Create and get a real template approved in Meta Business Manager; `POST
   /api/whatsapp/templates/sync`; send it through a workflow using `send_whatsapp_template` with
   the correct positional variables.
5. Deliberately trigger each documented permanent error (an unapproved template, a non-WhatsApp
   number, a window-closed free-form send) and confirm the mapped `MessagingErrorCode` and the
   dashboard's shown `error_code` match expectations.
