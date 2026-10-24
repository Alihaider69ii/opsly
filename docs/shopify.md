# Shopify integration (Phase 4, hardened in Phase 4.1)

A merchant connects a Shopify store; BizFlow imports its products, variants, customers and orders
into tenant-scoped tables and keeps them current. This phase **only reads and stores**. Nothing in
the AI agent calls Shopify or reads these tables yet: Phase 5 will, through `services/commerce`.

```
Dashboard "Connect"                                           Shopify
  POST /connect  --------------------------------------->  authorize (consent screen)
  <-- authorization_url (state = 256 random bits)                    |
                                                                      v
  /integrations/shopify/callback  <--- redirect with signed query -----+
  (Next.js route handler adds the user's session)
  GET /api/integrations/shopify/callback?<query, untouched>
     verify HMAC -> consume state -> exchange code -> store ENCRYPTED tokens -> queue first sync
                                                |
Worker (python -m app.workers.main)  <-- Redis job
   sync: products -> customers -> orders (time-sliced, resumable, rate-limit aware)
   webhook: re-read the changed object from Shopify, upsert
```

## What is implemented, verified, live-tested and blocked

| Area | Status |
| --- | --- |
| OAuth, expiring tokens and rotation, GraphQL client, throttling, sync, webhooks, disconnect, privacy webhooks | **IMPLEMENTED** and **VERIFIED**: aligned to Shopify's official documentation (below); 533 automated Shopify tests, real worker processes killed mid-operation (`docs/production.md` section 7), real PostgreSQL and Redis. |
| The same against a **real Shopify store** | **EXTERNALLY BLOCKED**: no app credentials or development store exist in this environment. The "Live verification runbook" below and `python -m tools.shopify_live_check` let the owner do it. Nothing in this document claims it was done. |
| Real webhook deliveries from Shopify to a public HTTPS URL | **EXTERNALLY BLOCKED** (needs a connected store and a public URL). Same runbook. |
| Approval for protected customer data | **EXTERNALLY BLOCKED**: granted by Shopify per app. Development stores are exempt. Without it, personal fields arrive null and the sync reports a warning, not a failure. |
| Encryption of the database at rest | **Operator's obligation**, required by Shopify for protected customer data (see "Privacy"). |
| Incremental sync, orders older than 60 days, several stores per organization | **NOT YET IMPLEMENTED** (see Limitations). |

### Verified against Shopify's official documentation (October 2026)

- The current stable Admin API version is **2026-10**. A new stable version ships every quarter;
  each is supported for at least **12 months**; a request for a version that is no longer served is
  **fallen forward** to the oldest supported one, silently, and reported in the
  `X-Shopify-API-Version` response header. **GraphQL** is the Admin API for new apps; REST is legacy
  and is not used.
- OAuth is the **authorization code grant**; the callback carries an `hmac` (hex HMAC-SHA256 of the
  sorted query without `hmac`, keyed with the client secret). The token request sets `expiring=1`.
- Offline access tokens are **expiring**: an access token of about an hour plus a **90-day refresh
  token that rotates**. Each refresh returns a new pair, but the refresh token you presented **stays
  usable until the earliest of**: the newer token is used, a new token is acquired by an
  authorization-code exchange, 30 days after it was first used to refresh, or its 90-day expiry. (An
  earlier version of this document said the old token is retired immediately; that was wrong and the
  code was written to the correct rule.)
- Webhooks are signed with `X-Shopify-Hmac-Sha256` (base64 HMAC-SHA256 of the raw body), identified
  by `X-Shopify-Webhook-Id`, must be acknowledged within **5 seconds**, arrive in no guaranteed
  order, and are **retried 8 times over about 4 hours, after which an API-created subscription is
  deleted by Shopify**. BizFlow therefore audits its subscriptions (below).
- GraphQL is rate limited by query cost (a leaky bucket in `extensions.cost.throttleStatus`); a
  throttled request is HTTP 200 with a `THROTTLED` error (or `MAX_COST_EXCEEDED`); the maximum cost of
  one query is 1000. `403` means a missing scope or permission; `402`, `404` and `423` mean the shop
  itself is unavailable (frozen, deleted, locked). BizFlow classifies them accordingly.
- Customer and order personal data is **protected customer data**: an app that is not approved gets
  nulls and `ACCESS_DENIED` for those fields (development stores are exempt). Level 1 requires
  encryption at rest and in transit. `read_orders` covers the last 60 days only.
- The three privacy webhooks (`customers/data_request`, `customers/redact`, `shop/redact`) are
  mandatory for public apps and are configured in the app's settings, not through the API.
  `customers/data_request` must be **acted on within 30 days**.

### API version policy

`services/integrations/shopify/versioning.py` is the only place the version is written
(`CURRENT_API_VERSION`); `SHOPIFY_API_VERSION` overrides it per deployment so an upgrade can be
staged. Only quarterly versions (`YYYY-01|04|07|10`) are accepted.

* The API and the worker log a warning at start when the configured version is past nine months old
  (three months before the end of the guaranteed window).
* Every client compares the version Shopify says it served with the one configured and warns **once**
  per pair when they differ: that is the fall-forward signal.
* **Upgrade procedure:** (1) read the changelog for the new version; (2) move `CURRENT_API_VERSION`,
  run the whole test suite; (3) run `python -m tools.shopify_live_check` against a development store
  with `SHOPIFY_API_VERSION` set to the new version; (4) deploy the worker first, then the API;
  (5) do this at most once a quarter and always before the configured version is nine months old.

## Scopes

Centralized in `services/integrations/shopify/scopes.py`, the only place scope strings are written.
BizFlow asks for the minimum, **read-only**:

| Scope | Required | Why |
| --- | :-: | --- |
| `read_products` | yes | Catalog and variants (titles, prices, inventory, availability); `products/*` webhooks. Connecting fails if it is declined. |
| `read_customers` | no | Customer records (name, email, phone, marketing consent); `customers/*` webhooks. Without it the customer import is skipped. |
| `read_orders` | no | Orders and line items for the last 60 days; `orders/*` webhooks. Without it the order import is skipped. |

Deliberately **not** requested: any `write_*` scope (BizFlow never changes a store), `read_all_orders`
(history beyond 60 days needs separate approval and is not needed yet), anything administrative.
`SHOPIFY_SCOPES` can narrow or change the list; a stage whose scope was not granted is `SKIPPED`
(a decision the merchant made), never a failure. The status API returns the granted scopes.

## Connecting: OAuth

Code: `oauth.py`, `validation.py`, `service.py`, `api/integrations/shopify.py`, frontend
`app/integrations/shopify/`.

| Threat | Control |
| --- | --- |
| Arbitrary or hostile shop (SSRF, open redirect) | `normalize_shop_domain` accepts only `<handle>.myshopify.com` (also the pasted admin URL `admin.shopify.com/store/<handle>`). It rejects IPs, `localhost`, ports, credentials, paths, queries, whitespace, non-ASCII (including look-alikes such as the Kelvin sign), `evil.com`, `a.myshopify.com.evil.com` and many more (the validation tests cover dozens of hostile inputs). Applied when the flow starts, in the callback, on webhook headers, and before every outgoing request. The frontend independently refuses to redirect anywhere but `https://*.myshopify.com`. |
| Forged callback | The query's HMAC is verified in constant time before anything else; duplicate query parameters are rejected (an attacker cannot make verification and use see different values). |
| State forgery / CSRF / login CSRF | `state` is 256 random bits; only its **hash** is stored; it expires in 10 minutes; it is single use (one atomic `UPDATE ... WHERE consumed_at IS NULL`); it is bound to the organization, **user** and shop that started it. The callback is authenticated, so another user cannot complete someone else's flow and an attacker cannot hand a victim an authorization the attacker started. |
| Replay | A used state fails; the callback's own `timestamp` must be within 10 minutes. |
| Organization injection | The organization is never read from the callback, a URL or a body; it comes from the authenticated session and the stored state. |
| Shop swap | The shop in the callback must equal the shop the state was issued for, and `shop { myshopifyDomain }` read back from Shopify must match it. |
| Two accounts, one store | A partial unique index (`shop_domain` where not `DISCONNECTED`) makes it impossible; it is also checked up front for a friendly error. One store per organization for now. |
| Wrong grant | Missing required scopes fail the connection with a clear message. |
| Abuse | `connect` and `callback` are rate limited (20 per minute per client); only OWNER/ADMIN may connect. |

The callback lands on the **Next.js** server (`/integrations/shopify/callback`), which holds the
user's session cookie, and forwards the raw query string to the API with the session token. The
browser never sees a token or the client secret, and the page displays only text from a fixed set of
error codes: a crafted `?error=` link cannot put words on the page.

On success: shop metadata (name, currency, time zone) is read, tokens are stored encrypted, the
first sync is queued, and the page shows "Syncing".

## Credentials at rest: `SecretManager`

`app/core/secrets.py`, reusable by any future integration.

- **AES-256-GCM** from the vetted `cryptography` library; a fresh random 96-bit nonce per message.
- Envelope `bf1:<key id>:<base64url(nonce || ciphertext+tag)>`. The key id makes **rotation**
  possible: deploy the new key as current and list the old one in `SECRET_ENCRYPTION_PREVIOUS_KEYS`;
  old ciphertext still decrypts and is re-encrypted under the new key the next time it is written
  (tokens refresh every hour; idle stores are rotated by the worker).
- **Context binding.** The connection id and kind (`shopify:access_token:<id>`) are authenticated
  data, so a ciphertext copied to another row (or from access token to refresh token) fails to
  decrypt. A database write alone cannot repoint one tenant's token at another.
- Fails closed: no key, malformed key, wrong key, tampered or truncated ciphertext, or an unknown key
  id each raise a `SecretError` with a code and no secret in the message. With no key configured the
  Shopify page says secure storage is missing and refuses to connect.
- Plaintext tokens exist only in memory during a request. They are never in an API response, a log
  line, an exception message or the frontend; `repr()` of the objects that hold them is redacted.

**Losing the key** makes stored tokens unreadable: those stores show "Needs attention" and the
merchant reconnects. **Leaking it together with a database copy** exposes the tokens; keep it in a
secret manager.

### Token lifecycle

Access tokens last about an hour. `ConnectionTokens` (`auth.py`) refreshes five minutes before expiry
and after a `401`. Because the refresh token **rotates**, two workers refreshing at once would waste
a rotation each, so refreshing happens under a row lock (`SELECT ... FOR UPDATE`) and is re-checked
after the lock: the loser of the race uses the winner's token. This is exercised with real threads on
Postgres. A refresh rejected by Shopify marks the connection `ERROR` ("reconnect"); a transient
failure changes nothing. A maintenance step rotates refresh tokens within 30 days of their 90-day
expiry so an idle store does not silently lose its connection.

**A crash during a refresh is recoverable.** If the worker dies after Shopify issued the new tokens
but before BizFlow stored them, the stored refresh token is still valid (Shopify's grace rule above),
so the next worker simply refreshes again. This was tested by hard-killing a real worker process at
exactly that moment. The limit: if the new tokens are lost **and** 30 days pass before any refresh
succeeds, the old token is dead and the merchant must reconnect; the connection says so instead of
looping.

## Data model (migrations `0005`, `0006`)

All UUID primary keys, all tenant-owned tables carry `organization_id`. `0006` only adds indexes
on the four foreign keys that lacked one (a test fails for any foreign key without one).

| Table | Notes |
| --- | --- |
| `shopify_connections` | One per organization (unique). Domain, `status` (CONNECTED, SYNCING, ERROR, DISCONNECTED), scopes, **only ciphertext** tokens (`*_encrypted`) and their expiries, shop name/currency/time zone, API version, `installed_at`, `last_sync_at`, webhook registration state, a safe user-facing error code and message. |
| `shopify_sync_states` | One row per (connection, resource): status, **cursor**, counts, attempts, heartbeat, safe error. This is what makes a sync resumable and observable. |
| `shopify_oauth_states` | Pending OAuth flows: **hash** of the state, organization, user, shop, expiry, consumed-at. |
| `shopify_webhook_events` | The idempotency ledger and the worker's work queue: shop, delivery id, topic, resource id, status. **Identifiers only: no payload, no personal data.** Pruned after 30 days. |
| `products`, `product_variants` | Catalog. Prices are exact decimals. Nullable where Shopify varies. |
| `customers` | Name, email, phone, marketing consent. No addresses. `shopify_customer_id` is nullable because Shopify does not always provide one. |
| `orders`, `order_items` | Statuses, currency, subtotal/total, timestamps, `cancelled_at`; lines link to products/variants where those are known. |

- **Tenant uniqueness:** `(organization_id, shopify_*_id)` is unique for every imported object, so
  two organizations can each hold the same Shopify id and one can never hold it twice.
- **Tenant integrity in the database:** children reference parents by composite foreign keys that
  include `organization_id` (`(product_id, organization_id) -> products`, and the same for orders,
  customers, sync states and webhook events). A row *cannot* point across tenants even if the
  application had a bug; integration tests try each case against real Postgres.
- **Deletion:** products, variants and customers carry `deleted_at` (a delete seen in Shopify marks
  the row inactive rather than destroying history); orders are never deleted. Every read service
  filters `deleted_at`. `customers/redact` erases the personal fields of a customer but keeps the row.
- **Money and time:** `NUMERIC` money, timezone-aware timestamps. Shopify timestamps are stored as
  given (`shopify_updated_at`).
- Raw Shopify JSON is never stored. The mappers (`mapper.py`) are pure and deterministic, tolerate
  missing and null fields, and return `None` for an object with no usable id, which is counted and
  skipped instead of failing a sync.

## Importing: the sync

Code: `sync.py`, `fetch.py`, `store.py`, `client.py`, `queries.py`, `workers/`.

- **Queued, never inline.** `callback` and `POST /sync` only mark the stages `PENDING` and enqueue a
  job on the existing Redis queue (no Celery). The worker runs it. If Redis is down the stages are
  failed visibly (`queue_unavailable`) instead of hanging.
- **Order:** products, then customers, then orders, so order lines link to products and customers.
  Stages whose scope was not granted are `SKIPPED` up front.
- **Idempotent:** every write is an upsert on `(organization, Shopify id)`. Running a sync twice, or
  two workers overlapping, never duplicates a row. An object whose Shopify `updatedAt` is older than
  the stored copy is "stale" and does not overwrite newer data.
- **Resumable:** each page and the cursor that points past it commit in **one transaction**, so an
  interrupted import continues from the last committed page.
- **Time-sliced:** a job runs for at most `SHOPIFY_SYNC_SLICE_SECONDS` (120), saves its place and
  re-queues itself, so a large import cannot starve crawls and webhooks on the same worker. Webhook
  jobs are served before crawls, crawls before imports.
- **Bounded memory:** one page at a time (20 products with up to 25 variants each, 50 customers, 20
  orders with up to 25 lines each); nested lists that exceed a page are followed with a bounded number
  of extra pages.
- **Safe to claim:** a stage is claimed with one atomic `UPDATE` (`PENDING -> RUNNING`); a worker
  that dies stops heart-beating and the maintenance loop returns its stage to `PENDING` with the
  cursor intact (after repeated attempts it is `FAILED`, "interrupted").
- **Reconciliation:** after a *complete* products or customers pass, rows not seen in that run are
  marked deleted. This repairs a missed delete webhook. It never runs after a partial pass.
- **Per-stage status** (`IDLE`, `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `SKIPPED`), counts,
  skipped-object count and a safe message are shown on the dashboard.
- **Manual sync:** `POST /sync` (OWNER/ADMIN). One atomic status flip means two clicks start one
  sync (409 for the second); a minimum interval between syncs (`SHOPIFY_SYNC_MIN_INTERVAL_SECONDS`,
  60) answers 429 with `Retry-After`. A connection that needs reauthorization cannot sync.

### Rate limits and retries

`client.py` reads `extensions.cost.throttleStatus` from every response and waits for the bucket to
refill *before* a request that would exceed it; if Shopify still throttles (`THROTTLED` or HTTP 429)
it retries with exponential backoff and jitter, honouring `Retry-After` (capped at 60 s), at most
`SHOPIFY_MAX_RETRIES` (5) times, then the stage retries later (up to 3 job-level attempts) via the
maintenance loop. **Permanent failures are not retried:** authentication (401, handled once by a
token refresh), permission (`ACCESS_DENIED`), invalid requests, `3xx`. Timeouts and `5xx` retry.
Redirects are never followed, the response size is capped (20 MB), environment proxy settings are
ignored, and logs carry the shop, operation, status and attempt but never tokens, headers, queries
or bodies.

When Shopify withholds protected customer fields, the sync keeps what it got and marks the stage with
a warning ("customer and order details may need protected customer data approval") rather than
failing.

## Webhooks

Code: `webhooks.py`, `api/webhooks/shopify.py`. One public endpoint, `POST /api/webhooks/shopify`.

**Intake** (inside the HTTP request, which has Shopify's 5-second budget) does no real work:

1. Read the raw body (streamed, capped at `SHOPIFY_WEBHOOK_MAX_BODY_BYTES`, 413 beyond).
2. **Verify `X-Shopify-Hmac-Sha256`** over the raw bytes, constant time, before parsing anything.
   Missing, malformed, wrong or an unconfigured secret all give `401` and store nothing. Failed
   signatures are rate limited per IP (30 per minute) so the endpoint cannot be probed freely.
3. Validate the shop header (`*.myshopify.com`), the topic (a topic we never subscribed to is
   acknowledged and ignored) and the delivery id.
4. Find the connection by shop. An unknown or disconnected store is acknowledged and ignored.
5. **Deduplicate.** `(shop_domain, webhook_id)` is unique, so a repeat is detected by the database
   itself (a pre-check for the common case, and an `IntegrityError` handler for two deliveries
   racing, proven with 8 simultaneous threads on Postgres): `200 duplicate`, no second job.
6. Store an identifiers-only ledger row, enqueue a job, answer `200`. If the queue is down the row is
   removed and `503` is returned, so Shopify's retry is not mistaken for a duplicate.

**Processing** (worker): a webhook is a *notification*, not data. The worker claims the event
(atomic `PENDING -> PROCESSING`), **re-reads the current object from Shopify** by id and upserts it.
That removes three problems: a replayed or out-of-order delivery cannot roll data back (the read
returns the truth), a validly signed but stale payload has nothing to inject, and no body containing
customer data ever needs storing. Transient failures retry (up to 5 attempts, re-queued by the
maintenance loop); permanent ones are recorded with a code.

| Topic | Effect |
| --- | --- |
| `products/create`, `products/update` | Re-read and upsert (variants included) |
| `products/delete` | Product and its variants marked deleted (`deleted_at`) |
| `customers/create`, `customers/update` | Re-read and upsert |
| `customers/delete` | Customer marked deleted |
| `orders/create`, `orders/updated`, `orders/cancelled` | Re-read and upsert (cancellation arrives as `cancelledAt`) |
| `app/uninstalled` | Same as Disconnect (no webhook cleanup: the app is already gone) |
| `customers/redact` | Erase that customer's personal fields |
| `customers/data_request` | Acknowledged and recorded (identifiers only). BizFlow **does** store a customer's name, email, phone and orders, so this must be fulfilled within 30 days: `python -m tools.customer_data_export --shop <shop> --customer-id <id> --out request.json` (owner-only file; the console prints counts, never values) |
| `shop/redact` | Purge everything for the store, unless the merchant reinstalled meanwhile (then ignored: it is current data) |

Subscriptions are created after the first sync starts (`SHOPIFY_WEBHOOK_BASE_URL` must be set;
otherwise `webhook_status` is `NOT_CONFIGURED`, the page says live updates are off and the manual sync
still works), limited to the scopes granted, idempotent, and recorded as `REGISTERED`, `PARTIAL` or
`FAILED`. A refusal never fails the connection. The privacy topics cannot be created through the API
and must be configured in the app's settings in Shopify.

**Self-healing subscriptions.** Shopify deletes an API-created subscription after 8 failed
deliveries (about 4 hours of the endpoint being down), silently. Every
`SHOPIFY_WEBHOOK_AUDIT_INTERVAL_SECONDS` (default 3600) the worker lists each connected store's
subscriptions, re-creates any that are missing and queues a **catch-up sync** for the time the store
was blind. It makes no Shopify call when `SHOPIFY_WEBHOOK_BASE_URL` is unset (or
`SHOPIFY_WEBHOOKS_DISABLED=true`), and a store whose token Shopify rejects is flagged "Needs
attention" by the audit itself, which is usually the first place a revoked token is noticed.

## Disconnecting

`POST /disconnect` (also the `app/uninstalled` and `shop/redact` webhooks) does, in order:

1. best effort: delete BizFlow's webhook subscriptions at Shopify (a Shopify outage cannot block it);
2. in one transaction: wipe both tokens and expiries, mark `DISCONNECTED`, delete the sync progress.
   From this instant no worker can use the store and any running sync notices at its next heartbeat;
3. purge the imported products, variants, customers, orders and lines for the organization, in
   batches. If interrupted, the worker's maintenance loop finishes it for **every** disconnected
   store that still has imported rows, however long ago it was disconnected (an earlier two-day
   limit was removed: personal data must not outlive a disconnect because a worker was down).

A sync that is writing a page when the merchant disconnects cannot leave data behind: the page is
written in a transaction that holds a row lock on the stage, which the disconnect needs, so the
disconnect waits for the page and then purges it (proved on PostgreSQL with two threads, and by
removing the lock and watching the test fail). What remains after a disconnect is audit metadata
only: the connection row (no tokens) and the webhook ledger (shop, delivery id, topic, resource id;
pruned after 30 days).

**Decision for review:** disconnecting **deletes the imported data**. Everything is re-importable by
reconnecting; keeping stale customer personal data and a catalogue Phase 5 might treat as current was
judged worse. If you prefer keep-until-asked, change `disconnect_store(purge=...)`; the guard against
the opposite mistake (`shop/redact` after a reinstall) already exists. Disconnecting does not
uninstall the app at Shopify (the merchant can do that in their store admin); BizFlow discards its
credentials and, where it can, its subscriptions, so the grant it held is unusable to it.

## Dashboard `/integrations/shopify`

Connect form (validated server-side; a bad address never leaves the app), status, shop name and
domain, connected date, last sync, per-stage progress with counts and messages, imported counts
(products, variants, customers, orders), granted scopes, webhook state, **Sync now**, and a two-step
**Disconnect** with an explicit warning that imported data is removed. The page refreshes itself
while a sync runs. It shows what the API returns, and the API returns no token, ciphertext, client
secret or customer personal data (response models are built from explicit field lists; a test asserts
no model has a field that could carry one). Members see everything but have no buttons; the API
refuses their writes with `403`. Server not configured for Shopify, or without secure storage, each
get their own clear message. A connection Shopify no longer accepts shows "Needs attention", the
reason and a Reconnect form.

## API

All under `/api`. "Manager" is OWNER or ADMIN. The organization always comes from the session, never
from the request (an `X-Organization-ID` header can only select among the caller's own organizations).

| Method | Path | Access | Notes |
| --- | --- | --- | --- |
| GET | `/integrations/shopify` | member | `available`, `secure_storage`, `webhooks_configured`, connection, scopes, per-stage sync, counts |
| POST | `/integrations/shopify/connect` | manager | `{"shop": "..."}` -> `{"authorization_url"}`; `422` invalid shop, `409` store owned elsewhere, `503` not configured |
| GET | `/integrations/shopify/callback` | manager | OAuth completion; errors are `{"detail": {"code", "message"}}` with a fixed catalogue |
| POST | `/integrations/shopify/sync` | manager | `202` queued; `404` not connected; `409` running or needs reauthorization; `429` too soon |
| POST | `/integrations/shopify/disconnect` | manager | Discard credentials, remove webhooks, purge |
| POST | `/webhooks/shopify` | HMAC | `200 accepted / duplicate / ignored`, `401` bad signature, `400` malformed, `413`, `503` queue down |

Request bodies forbid unknown fields (no mass assignment). The API returns no database ids at all and
has no endpoint that takes a Shopify or BizFlow object id from the client, so there is nothing to
enumerate.

## Reading the data (for Phase 5)

`services/commerce/queries.py`: `list_products`, `get_product`, `list_variants`, `get_customer`,
`get_order`, `list_order_items`, `orders_for_customer`. Each takes an `organization_id`, starts from
`scoped_select` and hides deleted rows; searches escape `%` and `_`. There is no Shopify call in the
agent or in these functions: the agent will read the local tables.

## Security review

| Risk | Result |
| --- | --- |
| OAuth state forgery, replay, cross-user completion | Hashed, single-use, 10-minute, bound to user + organization + shop; callback HMAC and timestamp. Tested incl. concurrent completion on Postgres. |
| Token leakage | AES-GCM at rest with row-bound context; never in API, frontend, logs (a test scans captured logs and the whole browser traffic), exceptions or `repr`. `python -m tools.reencrypt_secrets` proves when an old encryption key can be retired. |
| Tenant isolation | `scoped_select` everywhere, composite foreign keys, tenant-unique ids; cross-tenant attempts tried at the API and at the database. |
| Webhook HMAC bypass | Verified first, constant time, over raw bytes; empty secret, missing header and malformed base64 all refused. |
| Webhook replay / duplicates | Unique `(shop, delivery id)` enforced by the database; notification-only design makes replays harmless anyway. |
| Arbitrary shop domain / SSRF | Single validator, strict `*.myshopify.com`, applied at every boundary; no redirects followed; proxies ignored. |
| Unauthorized sync/disconnect/connect | `403` for members, `404` for outsiders, `401` anonymous. |
| PII leakage | Ledger stores identifiers only; API and logs carry none; `customers/redact` erases; disconnect purges. Personal data **is** stored in `customers` (name, email, phone) because Phase 5 needs it; it is exposed by no endpoint yet. |
| Mass assignment | `extra="forbid"` on request bodies; responses from explicit field lists. |
| ID enumeration | Random UUIDs; foreign and missing both `404`. |
| Insecure logging | Logs carry shop, org and connection ids, codes and counts only; `log_event` rejects sensitive field names. uvicorn's access log prints whole URLs, so a filter replaces the OAuth callback's `code`, `state` and `hmac` (and other credential-like query parameters) with `[redacted]`; the Next.js request log ignores the callback route. `httpx` request lines (which would print the same URLs) are switched off in the API and the worker, and one formatter redacts tokens, bearer credentials and values echoed by PostgreSQL error text after formatting. A test captures the raw log records of every flow (connect, failed connect, import, webhooks, refresh, revocation, throttling, restricted data, disconnect) and fails on any secret or customer detail; real container logs were scanned for six secrets. |
| Dev override abuse | `SHOPIFY_DEV_BASE_URL` (a local fake) is refused when `ENVIRONMENT=production`; so is an `http://` redirect URI. |

## Live verification runbook

Everything below needs credentials this repository does not have. It is the procedure that turns the
**EXTERNALLY BLOCKED** rows above into **LIVE-TESTED**. No step requires committing a secret; put the
values in `backend/.env` (git-ignored) or your secret manager. Use a **development store with test
data**; never point this at a merchant's real store while testing, and do not paste customer data into
tickets, screenshots or chat (the checker prints counts only).

1. **Create the app** in the Shopify Dev Dashboard (or Partner Dashboard). Set the redirect URL to
   `{NEXT_PUBLIC_APP_URL}/integrations/shopify/callback` (it must equal `SHOPIFY_REDIRECT_URI`
   exactly). Request the scopes in `docs/shopify.md` "Scopes". Copy the client id and secret.
2. **Create a development store** and add some products, customers and orders (the Dev Dashboard can
   generate test data). Development stores are exempt from protected-customer-data approval.
3. **Expose the API over HTTPS** for webhooks, for example `ngrok http 8000`, and set
   `SHOPIFY_WEBHOOK_BASE_URL=https://<your-subdomain>.ngrok-free.app`. Shopify's documentation only says the
   redirect URI "must exactly match" one configured for the app; it does **not** say whether an
   `http://localhost` redirect is accepted. Try `http://localhost:3000/integrations/shopify/callback`;
   if Shopify refuses it, tunnel the dashboard too and set `NEXT_PUBLIC_APP_URL` and
   `SHOPIFY_REDIRECT_URI` to the HTTPS address (BizFlow accepts `http://` redirects only outside production).
4. **Configure** `backend/.env`: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`,
   `SECRET_ENCRYPTION_KEY` (generate one, see `.env.example`), `SHOPIFY_REDIRECT_URI`,
   `SHOPIFY_WEBHOOK_BASE_URL`. Start Postgres and Redis, run `alembic upgrade head`, then the API, the
   worker and the dashboard.
5. **Connect** the store through the dashboard (the consent screen needs a human). Watch the three
   stages complete.
6. **Run the checker:** `cd backend && python -m tools.shopify_live_check --shop <store>.myshopify.com --pages 2 --webhooks --refresh`.
   It reads the shop, one or more pages of each resource, reports counts only (objects, variants,
   lines, malformed, protected-data withheld, how many customers have an email), reports the API
   version Shopify actually served, lists BizFlow's webhook subscriptions against the required topics,
   and forces a token refresh to prove rotation works. Exit status is non-zero on any failure;
   limitations of the store or app approval are reported as `WARN`.
7. **Real webhooks:** in the store admin edit a product, a customer and an order, and cancel an order.
   Within seconds the ledger (`select topic, status from shopify_webhook_events`) should show
   `PROCESSED` rows and the changes should appear. Edit the same product twice quickly (a duplicate or
   out-of-order delivery must change nothing).
8. **Failure cases worth running once:** stop the tunnel for 5 minutes, edit a product, restart it and
   confirm the next audit (or a manual sync) catches up; revoke the app's access in the store admin
   and confirm the connection flips to "Needs attention" within the audit interval; reconnect.
9. **Record the results** in `docs/production.md` section 2 (change the row to **LIVE-TESTED** and
   say what was run, on which date, against which API version). A failure is a finding, not a note.

For the AI vendor the equivalent is `python -m tools.ai_live_check [--embedding]` (see
`docs/ai-providers.md`).

## Tests

`pytest tests/shopify tests/core/test_secrets.py` (offline, in-memory SQLite with foreign keys on;
Phase 4.1 added API-version policy, webhook-subscription self-healing, key rotation, a log audit of
every flow, the disconnect/retention suite and the live-check tools):
validation (hostile shop inputs), mapper (nulls, bad ids, odd shapes), client (pagination cost,
throttling, `Retry-After`, retry limits, no retry on permanent errors, no leakage), OAuth (HMAC,
state, binding, replay, expiry, scopes), tokens (rotation, concurrency, failures), store (upserts,
staleness, reconciliation, purge, isolation), sync (resume, slices, crash recovery, scopes,
protected data), webhooks (HMAC, dedup, every topic, compliance), API (roles, isolation, rate
limits, no secrets). `pytest -m integration` repeats the parts that need a real database: schema and
constraints, composite foreign keys, concurrent upserts, the webhook unique constraint and race, the
sync state machine with competing workers, token-refresh locking, the API on Postgres + Redis, and
the embedding-model change on real pgvector, **hard-killing the real worker process** during product,
customer and order import, webhook processing and token refresh, and cutting Redis and PostgreSQL
under a running worker. `e2e/` (`cd e2e && npm test`) starts the whole stack in an isolated database
and drives the dashboard in a real browser against a fake Shopify served over HTTP.

## Limitations

- **Not run against a real store** (EXTERNALLY BLOCKED, see above and the runbook); protected
  customer data approval and public-app review are the operator's to obtain from Shopify.
- Protected customer data requires **encryption at rest** (Shopify, Level 1). The application encrypts
  tokens; it does not encrypt the customer rows. Use an encrypted PostgreSQL volume or managed database.
- **Orders cover the last 60 days** (`read_orders`). Older history needs `read_all_orders`.
- One store per organization. Multi-store needs the unique constraint relaxed and a store picker.
- Full re-import on every manual sync (idempotent, but not incremental). Incremental sync by
  `updated_at` is the next optimisation for very large stores.
- Only products and customers are reconciled after a complete pass (to repair a missed delete
  webhook); orders are never deleted or reconciled.
- Without a public `SHOPIFY_WEBHOOK_BASE_URL`, data is only as fresh as the last sync.
- Nested lists (variants, line items) are followed for up to 40 follow-up pages of 100 rows; a
  response beyond that would be truncated, which is above Shopify's own limits (2048 variants).
- Inventory is the variant's `inventoryQuantity` as Shopify reports it; per-location inventory,
  discounts, refunds, fulfilments, addresses and metafields are not imported.
- Currency is stored per row but not converted.
- Shopify API upgrades are deliberate: `SHOPIFY_API_VERSION` is pinned and validated as a quarterly
  version; a new version should be tested against the fake and a development store first.
