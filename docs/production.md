# Production readiness and operations (Phase 4.1)

Phase 4 built the Shopify integration and the provider-independent AI layer. Phase 4.1 took it from
"code-complete and heavily tested" to "deployable": a startup gate that refuses unsafe
configuration, real container images, real process-kill recovery tests, health endpoints an
orchestrator can use, a log pipeline that cannot leak, and a documented split between what was
proven and what could not be.

This document is the operator's guide and the honest ledger. Read section 2 first.

## 1. Status vocabulary

Every claim in these docs carries one of these labels, and they are never blurred.

| Label | Meaning |
| --- | --- |
| **IMPLEMENTED** | The code exists and automated tests cover it. |
| **VERIFIED** | Checked against Shopify's or a vendor's *official documentation*, or by a test/run described here that executed against real local infrastructure (PostgreSQL, Redis, sockets, containers, a browser). |
| **LIVE-TESTED** | Exercised against the *real external service* (a real Shopify store, a real AI vendor account). |
| **EXTERNALLY BLOCKED** | Cannot be done without credentials, accounts or approvals this environment does not have. A tool or runbook is provided so the owner can do it. |
| **NOT YET IMPLEMENTED** | Does not exist. |

## 2. What is and is not proven

No Shopify app credentials, Shopify store, or AI vendor key exists in this environment. Everything
that needs one is **EXTERNALLY BLOCKED**, not claimed.

| Area | Status | Evidence / what is missing |
| --- | --- | --- |
| Shopify OAuth, expiring tokens, refresh rotation, GraphQL client, throttling, sync, webhooks | IMPLEMENTED, VERIFIED | Aligned to Shopify's official documentation (October 2026); 533 Shopify tests; real worker processes killed mid-operation (section 7). |
| The same against a **real Shopify store** | **EXTERNALLY BLOCKED** | Needs a Dev Dashboard app + development store. Runbook: `docs/shopify.md` "Live verification runbook"; checker: `python -m tools.shopify_live_check`. |
| Real webhook deliveries from Shopify to a public HTTPS URL | **EXTERNALLY BLOCKED** | Needs a public URL (ngrok) and a connected store. Same runbook. |
| Protected customer data approval (customer and order personal fields) | **EXTERNALLY BLOCKED** | Granted by Shopify per app (development stores are exempt). Without it fields arrive null and the sync reports a warning. |
| OpenAI-compatible and Anthropic adapters | IMPLEMENTED, VERIFIED | Real local HTTP servers over real sockets (timeouts, oversized and slow bodies, redirects, malformed JSON); official API contracts checked October 2026. |
| Any adapter against a **real vendor account** | **EXTERNALLY BLOCKED** | Needs a key. Checker: `python -m tools.ai_live_check` (spends a few tokens). |
| Production startup gate | IMPLEMENTED, VERIFIED | Unit tests plus a real container refused an unsafe configuration and leaked none of its values. |
| Container images and the production compose topology | IMPLEMENTED, VERIFIED | Built and run: non-root, read-only filesystem, health checks, migration job, Redis/Postgres outage injection, no secret in any log. |
| Browser behaviour (auth, tenancy, Shopify page, layout, security headers, cookies) | IMPLEMENTED, VERIFIED | `e2e/` runs from one command in an isolated database. |
| Encryption of the database volume at rest | Operator responsibility | Shopify's protected-data requirement; the application encrypts tokens, not customer rows. See section 12. |
| Incremental (delta) sync, more than 60 days of orders, multiple stores per organization | NOT YET IMPLEMENTED | Documented limitations. |

## 3. Deployment topology

`docker-compose.production.yml` is a working single-host topology (it is what was run and tested):

```
reverse proxy / load balancer (TLS, appends the client address to X-Forwarded-For)   <- yours
        |                                   |
   frontend :3000 (127.0.0.1)         api :8000 (127.0.0.1)
        |  API_URL=http://api:8000          |            worker  (same image)
        +-----------------------------------+------------+
                         private network: db (pgvector) , redis (password)
                         migrate: one-shot `alembic upgrade head`
```

```bash
cp .env.production.example .env.production        # fill in every value; it is git-ignored
docker compose --env-file .env.production -f docker-compose.production.yml run --rm migrate
docker compose --env-file .env.production -f docker-compose.production.yml up -d
```

* **Nothing but the two web ports is published, and only to 127.0.0.1.** PostgreSQL and Redis are
  reachable from the private network only. (The *development* compose file binds both to loopback on
  IPv4 and IPv6; it is not a production file.)
* **Every secret is required.** Compose refuses to start with one unset, and the application refuses
  to start with a weak one (section 4).
* **API and worker are one image**, non-root (uid 10001), read-only root filesystem, no Linux
  capabilities, `no-new-privileges`. The worker's container health check is
  `python -m app.workers.healthcheck`.
* **The frontend image needs `frontend/public/widget.js`.** It is a git-ignored build artefact
  (`cd widget && npm ci && npm run build`); the Dockerfile fails the build if it is missing rather
  than ship a "Website chat" page that serves nothing.
* **Scaling.** Several API replicas are fine if `RATE_LIMIT_BACKEND=redis` (the gate warns otherwise).
  One worker is enough; several are safe (stage claims are atomic, duplicate jobs are harmless; both
  are tested with real processes).
* **The reverse proxy is required and must be trusted.** The frontend forwards the visitor's
  `X-Forwarded-For` to the API so rate limits see real clients. Your proxy must *append* the real peer
  address, and `FORWARDED_ALLOW_IPS` must name only the frontend's network (the compose default is the
  private subnet). If the frontend were reachable directly from the internet, a client could choose
  its own address and evade per-client rate limits (not authentication or tenancy).
* **Building on Windows under OneDrive:** Docker cannot read OneDrive "files on demand" placeholders
  (`invalid file request`). Build from a copy outside OneDrive.

## 4. The production startup gate

`app/core/startup_checks.py`. When `ENVIRONMENT=production`, the **API and the worker** list *every*
unsafe finding and exit instead of starting; `staging` logs the same findings as warnings;
`development` and `test` enforce nothing. `ENVIRONMENT` must be one of `development`, `test`,
`staging`, `production` (a typo such as `prod` is refused rather than silently skipping every guard).
Findings name the setting and the rule, never a value.

| Refused in production | Why |
| --- | --- |
| `JWT_SECRET_KEY` shorter than 32, fewer than 12 distinct characters, or containing a placeholder word | Forgeable login tokens |
| `DATABASE_URL` not PostgreSQL, no password, or a well-known development password | pgvector is required; guessable credentials |
| `CORS_ORIGINS` empty, a wildcard, not `https://`, `localhost`, or with a path | Cross-site access to the API |
| `REDIS_URL` left at the development default, or not `redis://`/`rediss://`/`unix://` | Queue and rate limits on the wrong server |
| `FORWARDED_ALLOW_IPS` not set (env var or setting) | Rate limiting would key on the proxy, not on clients |
| `LLM_PROVIDER` / `EMBEDDING_PROVIDER` unusable (including `fake`, which `Settings` also rejects) or without a key | The first customer question would fail |
| Shopify configured but `SECRET_ENCRYPTION_KEY` unusable | Tokens could not be stored safely |
| Shopify configured with neither an https `SHOPIFY_WEBHOOK_BASE_URL` nor `SHOPIFY_WEBHOOKS_DISABLED=true` | Live updates must be a decision, not an accident |
| `SHOPIFY_CLIENT_ID` without `SHOPIFY_CLIENT_SECRET` (or the reverse) | Half-configured |
| `LOG_LEVEL=DEBUG` | Verbose logs in production |

Warnings (start anyway): `RATE_LIMIT_BACKEND=memory`, `FORWARDED_ALLOW_IPS=*`, a remote database
without `sslmode`, a remote Redis without a password or without TLS, `API_DOCS_ENABLED`.
`SHOPIFY_DEV_BASE_URL` and an `http://` Shopify redirect URI are refused by `Settings` itself.

New settings (all optional, safe defaults; see `.env.example`): `FORWARDED_ALLOW_IPS`,
`API_DOCS_ENABLED` (docs are off in production), `MAX_REQUEST_BODY_BYTES` (1 MiB; webhooks and the
public chat have their own caps), `DB_CONNECT_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_SECONDS`,
`DB_LOCK_TIMEOUT_SECONDS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`, `REDIS_QUEUE_KEY`,
`SHOPIFY_WEBHOOKS_DISABLED`, `SHOPIFY_WEBHOOK_AUDIT_INTERVAL_SECONDS`, `WORKER_HEARTBEAT_FILE`.

## 5. Health, readiness and the worker

| Endpoint | Question | Touches | Use it for |
| --- | --- | --- | --- |
| `GET /health` , `/health/live` | Is the process running? | nothing | Liveness probe. A database outage must not make an orchestrator restart healthy API processes in a loop. |
| `GET /health/ready` | Can this instance serve? | PostgreSQL, Redis | Readiness probe. `503` takes it out of rotation. Bodies name a component and a state, never an address or an exception. |
| `GET /health/worker` | Is any worker beating? | Redis | **Alert on this.** With no worker, imports, webhooks and crawls silently stop. `503` with `none` or `unknown`. |
| `GET /health/db` | Database only | PostgreSQL | Kept for existing callers. |

The worker beats a file every 5 seconds (the container health check reads it) and a Redis key with a
20-second TTL (`/health/worker` counts them). A stopped worker removes its file, so it never looks
alive. Verified in real containers: Redis stopped gives readiness `503` while liveness stays `200` and
logins keep answering; PostgreSQL stopped gives `503` with `Retry-After: 5` and a request id, and the
API recovers when the database returns **without a restart** (`pool_pre_ping`); neither outage
restarted the API or the worker.

Every response carries `X-Request-ID` (a hostile inbound value is replaced, never echoed); log lines
written while handling a request, and worker log lines for a job (`sync:<id>`, `webhook:<id>`,
`crawl:<id>`), end with `request_id=...`. Unexpected errors return a generic 500 JSON with that id and
nothing else.

## 6. Retries, rate limits and recovery thresholds

| What | Policy |
| --- | --- |
| Shopify call | Waits for the GraphQL cost bucket before sending; `THROTTLED`/HTTP 429 retry with exponential backoff and jitter, honouring `Retry-After` (capped 60 s), at most `SHOPIFY_MAX_RETRIES` (5); `5xx` and timeouts retry. **Never retried:** `401` (one token refresh only), `403`, `402/404/423` (shop unavailable), invalid requests, redirects. |
| Sync stage | Claimed atomically; up to 3 job-level attempts; a stage whose heartbeat is older than `SHOPIFY_SYNC_STALE_AFTER_SECONDS` (600) returns to `PENDING` with its cursor, or is `FAILED` "interrupted" after 3 attempts. Pending stages older than 300 s are re-queued. |
| Webhook event | Claimed atomically; up to 5 attempts; events older than 120 s that are `PENDING` or stuck `PROCESSING` are re-queued (age is measured from receipt, so a long-running job is re-queued only if it also exceeds that). Processing is idempotent (it re-reads the object). |
| Webhook subscriptions | Shopify deletes an API-created subscription after 8 failed deliveries (about 4 hours). Every `SHOPIFY_WEBHOOK_AUDIT_INTERVAL_SECONDS` (3600) the worker checks each store's subscriptions, re-creates missing ones and queues a catch-up sync. |
| Token refresh | Under a row lock, re-checked after the lock; a crash after Shopify rotates but before BizFlow stores the new tokens is **recoverable**: Shopify keeps the old refresh token valid until the newer one is used, a new token is acquired, 30 days pass, or it expires (killed-worker test below). After 30 days of a lost rotation the merchant must reconnect; the connection says so. |
| Redis outage | The worker waits and retries; it does not crash. |
| Database outage | The job in flight is contained (`job crashed`), the worker pauses, and work resumes from the committed cursor. |
| Maintenance loop | Every 60 s, each step independent: stale crawls, orphaned jobs, stale sync stages, orphaned webhook events, ledger pruning (30 days), purge completion, token rotation (refresh tokens within 30 days of expiry), subscription audit. |

## 7. Failure-injection results (real processes)

`backend/tests/integration/test_worker_crash_recovery.py` starts the real worker
(`python -m app.workers.main`) against real PostgreSQL and Redis, with Shopify served over HTTP by
`tests/shopify/http_server.py`, holds one request open so the worker is known to be mid-operation,
and hard-kills it (no `finally`, no signal handler, as an out-of-memory kill would).

| Scenario | Result |
| --- | --- |
| Killed during the **product** import (page 2 held) | Page 1 is stored with its cursor; a second worker continues from that cursor; the first page is not fetched again; counts exact. |
| Killed during the **customer** import | The finished product stage is not repeated; customers complete. |
| Killed during the **order** import | Orders and their lines complete with no duplicates. |
| Killed twice in a row | The third worker finishes (attempt 3, the documented limit). |
| Killed three times | The stage fails *visibly* as "interrupted" with a safe message, never hangs; a manual **Sync now** completes everything. |
| Killed **while processing a webhook** | The event stays `PROCESSING`, is re-queued after the threshold, and is applied exactly once (2 attempts, 1 row). |
| Killed **after Shopify rotated the refresh token but before it was stored** | The stored refresh token still works; the second worker re-refreshes with it and finishes; the merchant never reconnects. |
| Duplicate webhook delivery and three duplicate queue messages | One event, one fetch, one row. |
| Two workers plus three duplicate sync jobs | Everything imported exactly once; each page fetched once. |
| Redis stopped under a running worker | Worker stays alive, logs `redis unavailable`, resumes when Redis returns. |
| PostgreSQL connections cut mid-import | Worker stays alive (`job crashed` contained), import completes after the database returns. |
| Disconnect while a page is being written | The disconnect waits for the page (row lock), then purges it; nothing is left behind. |

## 8. Database

* **Migrations** `0001`-`0006`, additive. `alembic check` reports no drift; the whole chain runs empty
  database -> head -> base -> head and `0006` is reversible with data kept (integration tests).
  Run `alembic upgrade head` as a one-shot job *before* starting new application containers
  (`migrate` in the compose file). Take a backup first; downgrades exist but are for development.
* **Server-side limits** on every PostgreSQL connection: statement timeout (120 s), lock timeout
  (30 s), idle-in-transaction timeout (300 s), connect timeout (5 s). A stuck query, a contended row or
  a vanished client cannot hold a connection or a lock indefinitely (each proven against the real
  server). `hide_parameters` keeps bound values (tokens, emails) out of SQLAlchemy errors.
* **Tenancy in the schema:** every tenant table has a non-null `organization_id` with a cascading
  foreign key (the webhook ledger is the one deliberate exception, see section 12), children reference
  parents by composite `(id, organization_id)` keys, and a test fails the build for any new table that
  breaks the pattern.
* **Indexes:** every foreign key has a supporting index that does not lead only with the tenant
  column (enforced by a test; `0006` added the four that were missing).
* **Backups, replication, point-in-time recovery, encryption of the volume:** the operator's, not the
  application's.
* **Key rotation:** `python -m tools.reencrypt_secrets` reports how many stored secrets use which key
  and `--apply` re-encrypts them; it exits `0` only when every secret is readable and on the current
  key, i.e. when it is provably safe to delete the old key from `SECRET_ENCRYPTION_PREVIOUS_KEYS`.

## 9. Secrets inventory

| Secret | Held in | Protection | Rotation |
| --- | --- | --- | --- |
| Shopify access and refresh tokens | `shopify_connections.*_encrypted` | AES-256-GCM, per-row context binding, key id in the envelope | Refresh hourly; key rotation via `tools.reencrypt_secrets` |
| `SECRET_ENCRYPTION_KEY` (+ previous keys) | Environment / your secret manager | `SecretStr`; never in `repr`, logs or errors | Section 8 |
| `SHOPIFY_CLIENT_SECRET` | Environment | `SecretStr`; signs/verifies OAuth callbacks and webhooks only | Regenerate in the Dev Dashboard, redeploy |
| `JWT_SECRET_KEY` | Environment | `SecretStr`; gate refuses weak values | Changing it logs everyone out |
| LLM / embedding API keys | Environment | `SecretStr`; sent only as the vendor's auth header; adapters never log request or response bodies | Per vendor |
| PostgreSQL / Redis passwords | Environment (URL userinfo) | Masked in `repr` of settings; not logged | Per infrastructure |
| OAuth `state` | `shopify_oauth_states` | Only its hash is stored | Single use, 10 minutes |
| Session token | HttpOnly cookie in the browser | Never readable by page scripts, never in HTML/RSC output, not in storage | 8 hours |
| Public chat conversation tokens | Browser; hashed at rest | Constant-time comparison | Per conversation |

Nothing secret is in the repository (a test scans every committable file), in the frontend bundle or
the widget (a test asserts server-only names never appear there), in an API response (response
models are explicit field lists), or in any container's log (verified for six secrets).

## 10. Logging

* Application logs (`app.*`) and worker logs use one formatter that redacts after formatting: PostgreSQL
  error text that echoes a value (`Key (email)=(...)`, `Failing row contains`, `invalid input
  syntax ... "<value>"`), `Bearer`/`Basic` credentials, Shopify and API-key shapes, JWTs, and
  `X-Shopify-Access-Token`/`Authorization`-style headers. This is the *last* line of defence: the code
  itself is audited (a test captures raw records across connect, failed connect, import, webhooks,
  refresh, revocation, throttling, restricted data and disconnect and fails on any token, OAuth code,
  state, signature or customer detail).
* `httpx`/`httpcore` per-request INFO lines are switched off (URLs carry OAuth codes and merchants'
  query strings). `uvicorn.access` redacts `code`, `state`, `hmac`, `token`, ... from request lines.
  The Next.js server does not log the Shopify callback route.
* `log_event` rejects fields such as `content`, `prompt`, `token`, `email` outright.
* AI prompts and replies are never logged: only ids, counts, timings, finish reason and error code.

## 11. Web security

* Dashboard headers (`next.config.ts`, verified on the production server): `X-Frame-Options: DENY`,
  `Content-Security-Policy: frame-ancestors 'none'; base-uri 'self'; object-src 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy`, HSTS (production builds), and no `X-Powered-By`. A script-src CSP and
  `form-action` are deliberately **not** set: Next.js injects inline scripts that need per-request
  nonces, and `form-action` would block the redirect to Shopify's authorize page. Adding a nonce-based
  CSP is a recommended follow-up (finding M-2 below).
* Session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` in production builds, 8 hours. CSRF is covered
  by `SameSite=Lax` plus Server Actions' built-in Origin check.
* API CORS: exact origins from `CORS_ORIGINS`, only the dashboard's methods and headers.
* Request bodies are capped (413 before parsing); public chat and webhooks have their own caps.
* Roles: OWNER/ADMIN manage; MEMBER reads. A route-by-route matrix test (every role x every
  organization route x tenant) fails if a new route is not classified.

## 12. Privacy and retention

* **Disconnect purges imported commerce data** (kept decision). After a disconnect no imported
  product, variant, customer, order or line exists, and no customer detail exists anywhere in the
  database (a test dumps every table). What remains is audit metadata: the connection row (no tokens)
  and the webhook ledger (identifiers only: shop, delivery id, topic, resource id; pruned after 30
  days). The ledger stores a Shopify resource id, which is a pseudonymous identifier.
* A purge that is interrupted, or a page written by a worker already past its last check, is cleaned
  by the maintenance loop for **every** disconnected store with leftover rows, with no age limit.
* `customers/redact` erases a customer's personal fields; `shop/redact` and `app/uninstalled` purge.
* `customers/data_request` is acknowledged and recorded; it must be **acted on within 30 days**:
  `python -m tools.customer_data_export --shop store.myshopify.com --customer-id 123 --out request.json`
  writes that customer's stored data to an owner-only file (the console shows counts only; the file
  contains personal data and must be handled under your data-protection policy).
* **Protected customer data, Level 1 (Shopify):** data must be encrypted at rest and in transit. The
  application serves TLS only through your proxy and encrypts tokens; **encrypting the PostgreSQL
  volume (disk or managed-database encryption) is the operator's obligation** and must be in place
  before the app is approved for protected customer data.

## 13. Dependency hygiene

`pip-audit` (runtime requirements) and `npm audit --omit=dev`: no known vulnerabilities after this
phase. `pip-audit` found `cryptography` 47.0.0 with four advisories (three in X.509/PKCS7 code
BizFlow never calls, one in the OpenSSL bundled in the wheel that AES-GCM does run on); the range was
`<48`, which excluded every fixed release. It is now `cryptography>=50,<51`. There is no Python lock
file: ranges are pinned to a major version, so rebuilds can pick up newer minors (finding L-3).

## 14. Reproducing the verification

```bash
# backend (from backend/)
.venv/Scripts/python -m pytest                       # unit tier, SQLite, offline
docker compose up -d db redis
.venv/Scripts/python -m pytest -m integration        # real PostgreSQL + Redis + worker processes
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy app tools
.venv/Scripts/python -m alembic check
# frontend / widget
cd frontend && npm test && npm run lint && npm run typecheck && npm run build
cd widget && npm test
# browser end-to-end: one command, isolated database, everything started and stopped for you
cd e2e && npm ci && npm test                         # offline; `npm run test:full` also crawls public sites
# mutation testing is an engineering tool, kept out of CI: backend/mutation (see its README)
```

## 15. Findings register

Severity scale: **CRITICAL** (exploitable now, or breaks a security boundary), **HIGH** (a production
blocker in authentication, authorization, tenant isolation, secrets, OAuth, token handling, webhook
authenticity, data integrity, or exposure of sensitive data), **MEDIUM**, **LOW**, **EXTERNAL
LIMITATION** (cannot be resolved inside this repository). Everything found in Phase 4.1 that was
CRITICAL or HIGH is resolved. Nothing CRITICAL was found.

### Found and resolved in this phase

| ID | Sev. | Finding | Resolution |
| --- | :-: | --- | --- |
| R-1 | HIGH | **Customer data could survive a disconnect.** A sync worker reads its stage status, then writes a page. A disconnect (stage row deleted, purge run) landing between those two steps let the page commit after the purge. The only backstop, the purge-retry job, ignored stores disconnected more than two days earlier. | The page is written in a transaction that locks the stage row, which the disconnect needs; the retry job now covers every disconnected store with leftover rows, at any age. Race test on PostgreSQL; fails if the lock is removed. |
| R-2 | HIGH | **`cryptography` 47.0.0 (the library that encrypts Shopify tokens) had four advisories**, one in the OpenSSL bundled in the wheel; the allowed range `<48` excluded every patched release. | `cryptography>=50,<51`; `pip-audit` clean; secret/rotation tests re-run. |
| R-3 | HIGH | **Production could start with unsafe configuration** (a weak or placeholder JWT secret, wildcard or http CORS, a development database password, the fake AI provider, an unreachable proxy setting). | The start-up gate (section 4), proven in a real container. |
| R-4 | MEDIUM | PostgreSQL error text echoes values (`Key (email)=(...)`), and an unhandled integrity error would put a user's email in the server log; a library could include a credential in a message. | One redacting formatter for API and worker; `hide_parameters` on every engine; tested against the real server. |
| R-5 | MEDIUM | `JWT_SECRET_KEY` was a plain string (visible in `repr`/logs of settings); URL passwords were visible too. | `SecretStr` and masking of URL credentials in `repr`. |
| R-6 | MEDIUM | The worker had no crash containment: one bad job or a Redis blip ended the process. No liveness for the worker; no readiness for the API. | Containment, backoff, heartbeat, `/health/ready`, `/health/worker`; proven by killing real processes. |
| R-7 | MEDIUM | Shopify deletes API-created webhook subscriptions after 8 failed deliveries; BizFlow would have gone silently stale. | Subscription audit with re-creation and a catch-up sync. |
| R-8 | MEDIUM | The refresh-token rotation model was wrong (documented as "retired immediately"; Shopify keeps the old token valid for a grace period). A 403 was treated as a shop-level failure. | Corrected to the official rules, with the 30-day limit documented and tested; 403 is "access denied". |
| R-9 | MEDIUM | Provider capabilities were overstated (embeddings claimed for vendors whose models do not fit the 1536-wide column); Anthropic would have needed (and lacked) its own adapter. | Honest catalog, `anthropic_messages` adapter, shared hardened HTTP client (size cap, wall-clock deadline). |
| R-10 | MEDIUM | No database timeouts: a stuck statement or lock could hold a connection for ever. Token-refresh lock waits surfaced as raw database errors. | Server-side limits; lock timeout becomes a transient "unavailable". |
| R-11 | MEDIUM | The dashboard sent no security headers (clickjacking, sniffing) and advertised `X-Powered-By`. | Headers added and verified on the production server. |
| R-12 | MEDIUM | Container images ran as root with a writable filesystem; the development compose published PostgreSQL on all interfaces. | Non-root, read-only, no capabilities; loopback-only ports. |
| R-13 | MEDIUM | Four foreign keys had no useful index (deletes and cascades scan the child table). | Migration `0006`; a test fails for any new unindexed foreign key. |
| R-14 | LOW | An API version fall-forward by Shopify would change behaviour silently. | Version centralised; served version compared and warned; ageing warning. |
| R-15 | LOW | `httpx` INFO request lines can carry URLs with secrets; `.gitignore` did not cover key files, SQLite files or the data-request export. | Switched off in API and worker; ignore rules and a repository secret scan. |
| R-16 | LOW | Stray empty packages `backend/api` and `backend/services`; mixed line endings in `backend/Dockerfile`. | Removed / normalised. |
| R-17 | MEDIUM | **The chat widget could mount twice, or appear on a page that had been left.** Its "one widget per page" guard only looked at an already-mounted widget, but starting awaits the network; the state was also kept per loaded copy of the bundle, so a snippet pasted twice, or the dashboard preview's effect (which React runs twice in development and which appended the script each time), gave every copy its own widget. Found by the real-browser run (two launchers, clicks intercepted; a stray launcher after leaving the page). | Guard state kept on the page, a start cancelled by `destroy()`, a second start refused while one is in flight; the preview loads the bundle once. Six new widget tests plus two for the two-copies case; each of the three mechanisms was removed in turn and the tests failed. |

### Remaining (consciously accepted or external)

| ID | Sev. | Finding | Why it is acceptable now / what would resolve it |
| --- | :-: | --- | --- |
| E-1 | EXTERNAL | Nothing has run against a **real Shopify store** or **real webhooks**. | Needs a Dev Dashboard app and development store. Runbook and checker provided (`docs/shopify.md`). |
| E-2 | EXTERNAL | No AI vendor has been called with a **real key**. | Needs a key. `python -m tools.ai_live_check`. |
| E-3 | EXTERNAL | **Protected customer data approval** and public-app review. | Granted by Shopify per app; development stores are exempt. |
| M-1 | MEDIUM | **Database encryption at rest** (a Shopify requirement for protected customer data) is not something the application can do. | Operator: encrypted volume or managed database, before approval. |
| M-2 | MEDIUM | No nonce-based `script-src` CSP on the dashboard. | Defence in depth only: the dashboard and widget contain no raw-HTML sinks (`innerHTML`, `dangerouslySetInnerHTML`, `eval`), React escapes output, and the session cookie is HttpOnly. Needs a per-request nonce via `proxy.ts`; a recommended follow-up. |
| M-3 | MEDIUM | **No server-side revocation of dashboard sessions** (no refresh tokens; a logged-out token stays valid until its 8 hours end). Pre-existing. | Short lifetime; HttpOnly cookie. Resolve with refresh tokens and a token version. |
| M-4 | MEDIUM | `X-Forwarded-For` is only as trustworthy as your edge proxy. | Documented requirement (section 3); affects per-client rate limits, not authorization. |
| M-5 | MEDIUM | PostgreSQL TLS (`sslmode`) and Redis TLS are the operator's decision; the gate warns, it does not refuse. | Inside a private network this is normal; required across networks. |
| L-1 | LOW | The webhook ledger keeps a Shopify resource id (pseudonymous) for 30 days, including after a disconnect. | Audit trail; contains no personal fields. |
| L-2 | LOW | Webhook recovery measures age from receipt, so a slow event can be queued twice. | Harmless: the atomic claim and idempotent processing make a duplicate a no-op. |
| L-3 | LOW | No Python lock file (ranges pinned to a major version); base images are not pinned by digest. | Pin with `pip-compile` / image digests in your CI. |
| L-4 | LOW | The job queue lives in Redis, which is not a source of truth. A lost queue is repaired by the maintenance loop (orphaned stages, events and crawls are re-queued), not instantly. | By design (Phase 3). |
| L-5 | LOW | Every worker runs the maintenance loop. | Idempotent; only duplicated work. |
| L-6 | LOW | No incremental sync, orders only for the last 60 days, one store per organization. | Documented product limits (`docs/shopify.md`). |
| L-7 | LOW | The Next.js **development** server prints each Server Action call with its arguments, so a typed email address appears in a developer's terminal after a failed login (the password does not). | Next.js documents its request logging as development-mode only, and the production container logged no request lines in the checks run. Do not run `next dev` as a deployment; the callback route is excluded from dev logging. |

## 16. Verification results (final run, 23 October 2026)

Those results describe the Phase 4.1 baseline and do not certify Phase 5. The Phase 5 automation
implementation is underway; its current local checks, unavailable infrastructure checks and exact
remaining production gates are tracked in [phase-5-progress.md](phase-5-progress.md), with design
details in [automation.md](automation.md). In this environment PostgreSQL/Redis could not be started
because the Docker Desktop engine was unavailable, so migration-cycle, integration, worker failure
injection, browser E2E and mutation results are not established for Phase 5. Do not infer Phase 5
production readiness from the Phase 4.1 table below.

All of the below ran on the final tree. The same commands are in section 14.

| Check | Result |
| --- | --- |
| Backend unit tier (SQLite, offline) | **2,008 passed**, 1 skipped (the POSIX-only SIGTERM test; the signal handling itself is unit-tested), 0 failed |
| Backend integration tier (real PostgreSQL, Redis, and real worker processes killed mid-job) | **122 passed**, 1 skipped (the same POSIX-only test), 0 failed, 6 min 40 s |
| Frontend | 15 tests, `eslint`, `tsc`, production `next build`: clean |
| Widget | 47 tests; the new lifecycle tests fail when each guard is removed |
| Browser end-to-end (`cd e2e && npm run test:full`) | **235 checks, 0 failed**: auth 34, security 16, layout 8, Shopify 87, knowledge 35, chat 55; the database it used is removed afterwards |
| Mutation testing (144 mutants) | **138 killed, 6 survived**, all six documented (four equivalent guards, one duplicate, one control); `app/` byte-identical afterwards. The first run had 11 survivors; five were real test gaps and now have tests |
| Static checks | `ruff check`, `ruff format --check`, `mypy app tools` (128 files), `alembic check`: clean |
| Dependency audit | `pip-audit` on the runtime requirements and `npm audit --omit=dev`: no known vulnerabilities (after the `cryptography` upgrade) |
| Containers | Backend and frontend images built; the production topology ran (migration job, API, worker, frontend, database, Redis); an unsafe configuration was refused without printing any value; Redis and PostgreSQL outages were injected and recovered without restarting the API or worker; no secret appeared in any container's log |
| Real-log scan | The end-to-end stack's API, worker, fake-Shopify and dashboard logs were scanned for 13 token, key, OAuth, ciphertext, password and customer-data patterns: nothing, apart from L-7 above and the app's own error *codes* (`code=invalid_state`) |
| Live Shopify store, live webhooks, live AI vendor | **Not run: EXTERNALLY BLOCKED** (no credentials). Runbooks and checker tools: `docs/shopify.md`, `docs/ai-providers.md` |
