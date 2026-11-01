# Deployment Readiness Audit

Status: **PASS WITH WARNINGS** — see the final report delivered alongside this document for the full verdict, test results, and remaining external setup steps. No destructive action was taken; nothing was committed or deployed.

Scope of this audit: preparing the repository for first real deployment (GitHub for source hosting, Netlify for the Next.js frontend, backend/worker/DB/Redis on a separate host TBD). This is a readiness audit and preparation pass only — no redesign, no Phase 11, no commits, no deploys.

---

## A. Current architecture

**Documented topology** (`docs/production.md`): single-host `docker-compose.production.yml` — a reverse proxy (operator-supplied) in front of `frontend:3000` and `api:8000` (both bound to `127.0.0.1` only), with `db` (pgvector) and `redis` (password-protected) on an internal-only Docker network, plus a one-shot `migrate` service running `alembic upgrade head` before `api`/`worker` start. `api` and `worker` share one hardened image (`read_only: true`, `cap_drop: [ALL]`, non-root).

**Gap**: `docs/production.md` has **no Netlify-specific guidance** and was never updated past Phase 4.1. It documents only the Phase-4.1-era startup-gate checks and the Phase 4.1 migration count ("0001-0006, additive" — stale; actual chain is 0001-0012, see section G). [Finding: docs-production-stale]

**Confirmed independently**: Alembic migration chain is linear and clean — `<base> → 0001 → 0002 → … → 0012 (head)`, single head, no branching (verified via `alembic heads` / `alembic history`, no DB connection required).

**Frontend**: Next.js `16.4.2` (`frontend/package.json:14`), React `19.3.0`. Default (non-static-export) server build — `frontend/next.config.ts` sets no `output` key, and the app uses Server Actions extensively (11 files with `"use server"`: auth, onboarding, knowledge, website-chat, automations, shopify integration, admin, operations, support, email, whatsapp). Requires a Next.js-aware host (Netlify's Next runtime), not a static-file host.

**Widget bundling model**: the widget (`widget/`) is a separate vanilla-JS esbuild package whose build output (`widget/dist/widget.js`) is copied into `frontend/public/widget.js` and served from the **same origin as the dashboard** — not an independent CDN deployment. `frontend/public/widget.js` is git-ignored and was previously built only as a Docker-build precondition (`frontend/Dockerfile:30` fails the build if it's missing), with no equivalent for a plain Netlify `next build`. **Fixed** — see section D/N: `netlify.toml`'s build command now builds `widget/` first.

**Backend**: FastAPI/Uvicorn, one process per container, `api`+`worker` share one non-root Docker image differentiated only by command. **Auth is stateless Bearer-token, no cookies set anywhere on the backend** — confirmed by grep and by reading every auth path. **Worker**: Redis-backed priority job queue (`BLPOP` over 7 lists), DB-row-based job claiming (safe for multiple worker instances), graceful SIGTERM/SIGINT handling that lets in-flight crawl/sync jobs save progress.

**Database**: PostgreSQL + pgvector, Alembic-managed, 12 migrations (`0001`-`0012`), clean linear chain, single head.

**Cache/queue**: Redis — job queue (source of truth is Postgres, Redis is delivery-only) and optionally the distributed rate limiter.

---

## B. Deployment topology recommendation

Confirmed by both backend and frontend audits — the codebase already assumes exactly this split (no server rewrite needed):

- **Netlify**: the Next.js frontend only. `netlify.toml` (created by this audit — see Known Blockers) pins Node 22, chains the widget build in front of `next build`, and relies on Netlify's auto-detected Next.js adapter (verified against current Netlify docs — no `[[plugins]]` block needed or added).
- **Backend host (NOT Netlify)**: FastAPI `api` process — one uvicorn process per container, scales horizontally by running more containers (no in-process multi-worker flag). A container platform (Fly.io, Render, ECS, a VPS running the existing `docker-compose.production.yml`, etc.) is appropriate; Netlify cannot host this (it's a long-running stateful server, not a serverless function).
- **Worker host**: same image as the API, different command (`python -m app.workers.main`); can run as a second container/service on the same platform as the API. Confirmed safe to run multiple instances (DB-row-based job claiming), with one minor redundant-work caveat (`SubscriptionAuditor`, low severity).
- **PostgreSQL**: managed Postgres with the `pgvector` extension available (the app's own migration enables the extension, but the server must support it).
- **Redis**: managed Redis, reachable from both api and worker; used for the job queue and (recommended, see Known Blockers) the distributed rate limiter.
- **Migrations**: run `alembic upgrade head` as a one-shot step against the real `DATABASE_URL` before starting/updating api+worker — this is already how `docker-compose.production.yml`'s `migrate` service works and requires no change.

This is a genuine split-domain deployment (frontend and backend on different hosts/domains) — both audits independently confirmed the existing auth design (bearer token stored in a frontend-only cookie, server-to-server calls to the backend) already supports this without any cross-origin cookie workaround.

---

## C. Required environment variables

Full `Settings` inventory read from `backend/app/core/config.py` (120+ fields). Hard-required-in-production (startup refuses without them, verified against `startup_checks.py`):

| Variable | Purpose | Required/Optional | Consumer |
|---|---|---|---|
| `DATABASE_URL` | Postgres connection (`postgresql+psycopg://...`) | **Required** (no default) | api, worker, migrate |
| `JWT_SECRET_KEY` | Signs access tokens; ≥32 chars, rejected if weak/placeholder | **Required in every environment** (Pydantic-level, not just prod) | api |
| `CORS_ORIGINS` | Comma-separated allowed browser origins | **Required in prod** — must be explicit https, no wildcard, no localhost | api |
| `REDIS_URL` | Job queue + rate-limit backend connection | **Required in prod** (must be explicitly set, not the localhost default) | api, worker |
| `FORWARDED_ALLOW_IPS` | Peers uvicorn trusts for `X-Forwarded-*` | **Required in prod** (empty = hard refusal); never set to `*` unless API is unreachable except via the proxy | api (uvicorn) |
| `LLM_PROVIDER` / `LLM_API_KEY` | AI chat model provider+key | **Required in prod** (the `fake` provider is rejected) | api, worker |
| `EMBEDDING_PROVIDER` / `EMBEDDING_API_KEY` | Embedding provider+key for RAG | **Required in prod** (`fake` rejected) | api, worker |
| `SECRET_ENCRYPTION_KEY` | AES-256-GCM key protecting stored Shopify/WhatsApp tokens, 32 random bytes base64 | **Conditionally required** — only enforced if Shopify or WhatsApp is configured; **no startup check at all if neither is configured today, even though config.py defines no safe default** [Finding: secret-encryption-key-conditional] | api, worker |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | Your Shopify app credentials | Optional — blank disables Shopify; must be paired (both-or-neither) | api, worker |
| `SHOPIFY_REDIRECT_URI` | OAuth callback URL, must exactly match app registration | Required (https) once Shopify configured | frontend-facing, consumed by backend |
| `SHOPIFY_WEBHOOK_BASE_URL` / `SHOPIFY_WEBHOOKS_DISABLED` | Public base URL for registering live-update webhooks, or explicit opt-out | One of the two required once Shopify configured | worker |
| `WHATSAPP_APP_ID` / `WHATSAPP_APP_SECRET` | Meta WhatsApp app credentials | Optional — must be paired | api, worker |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` / `WHATSAPP_WEBHOOKS_DISABLED` | Meta webhook handshake token, or explicit opt-out | One of the two required once WhatsApp configured | api |
| `EMAIL_PROVIDER` / `EMAIL_API_KEY` | Email provider (default `resend`) + key | Required once email features used (`fake` rejected in prod) | api, worker |
| `EMAIL_WEBHOOK_SECRET` / `EMAIL_WEBHOOKS_DISABLED` | Email provider webhook signing secret, or explicit opt-out | One of the two required once email configured | api |
| `EMAIL_INBOUND_DOMAIN` | Real inbound-email domain | Required (non-`.invalid` placeholder) once email configured | api |
| `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD` | Seed-only credential for the platform-admin seed tool | Optional; if `PLATFORM_ADMIN_PASSWORD` is set it must pass the same strength check as `JWT_SECRET_KEY` | `backend/tools/seed_platform_admin.py` only, not read by request handling |
| `LOG_LEVEL` | App log verbosity | Required ≠ `DEBUG` in prod | api, worker |
| `RATE_LIMIT_BACKEND` | `memory` or `redis` | **Not hard-enforced** — `memory` in prod only produces a warning, never blocks startup [Finding: rate-limit-memory-not-blocked] | api |
| `API_DOCS_ENABLED` | Expose `/docs`,`/redoc`,`/openapi.json` | Defaults off in prod; warns if explicitly turned on | api |
| `APP_ORIGIN` / `API_ORIGIN` | Public https origins (compose-file convenience vars, not read directly by `Settings`) | Required by `docker-compose.production.yml` to derive `CORS_ORIGINS`/`SHOPIFY_REDIRECT_URI`/`SHOPIFY_WEBHOOK_BASE_URL`/frontend build args | compose only |

**Frontend** (`frontend/.env.example`, confirmed complete — matches every `NEXT_PUBLIC_*`/`API_URL` reference in code):

| Variable | Purpose | Required/Optional | Consumer |
|---|---|---|---|
| `API_URL` | Server-side (Next server → FastAPI) backend origin, Bearer-token calls | Required — defaults to `http://localhost:8000` if unset, which is wrong in any real deployment | Next.js server runtime (Server Components, Server Actions, Route Handlers) |
| `NEXT_PUBLIC_API_URL` | Client-exposed backend origin, used only to build the public chat-widget embed snippet | Required — same localhost-fallback risk | browser (widget embed only) |
| `NEXT_PUBLIC_APP_URL` | Client-exposed dashboard origin, used in the same embed snippet | Required — same localhost-fallback risk | browser (widget embed only) |

**Gap found**: root `.env.example` (`.env.example`, backend) documents Phase 1-5 settings thoroughly but was **not updated for Phase 6-10** — it has no `WHATSAPP_*`, `EMAIL_*`, or `PLATFORM_ADMIN_*` entries at all, despite `config.py` defining ~45 such settings. `.env.production.example` likewise only covers Shopify/AI/core secrets, not WhatsApp/email/platform-admin. [Finding: env-example-missing-phase6-10] — addressed in section 15 fixes below.

---

## D. Frontend deployment requirements

**Stack**: Next.js `16.4.2`, React `19.3.0`. Build command `next build` (`frontend/package.json:7`), typecheck `next typegen && tsc --noEmit` (`:9`). **Verified**: typecheck PASS (0 errors), `npm run lint` PASS (0 errors/warnings), `npm run build` PASS (all 39 routes compiled, including the Partial-Prerender/dynamic split).

**Netlify compatibility gaps found, all FIXED by this audit** (root `netlify.toml` created):
1. **No `netlify.toml` anywhere in the repo** (confirmed by repo-wide search). **Fixed**: created at repo root with `base = "frontend"` and an explicit build command.
2. **`@netlify/plugin-nextjs` was not declared anywhere.** Checked against Netlify's current (live, fetched during this audit) Next.js docs rather than assumed from memory: the modern Netlify Next.js support (the OpenNext adapter) is **auto-detected from `next` in `package.json` with zero configuration** — Netlify's own docs explicitly recommend *against* declaring `[[plugins]] package = "@netlify/plugin-nextjs"` in `netlify.toml` unless pinning a specific adapter version (which would also require adding that package to `frontend/package.json`, which the task asked not to do unnecessarily), since pinning forgoes automatic adapter updates. **Resolution: no plugin block added** — `netlify.toml` relies on auto-detection, documented inline with a comment citing the reasoning, so a future reader doesn't wonder why it's missing.
3. **No Node version pin for Netlify** — no `engines` field in `frontend/package.json`, no `.nvmrc`. **Fixed**: `NODE_VERSION = "22"` set in `netlify.toml`, matching `frontend/Dockerfile:11`'s `node:22-alpine`.
4. **Widget build was not wired into the frontend build** — `frontend/package.json`'s `build` script is plain `next build`; nothing invoked `cd widget && npm ci && npm run build`. Left as-is, this means `/widget.js` 404s at runtime on Netlify, silently breaking the in-dashboard "Website Chat" preview AND every merchant's embedded chat widget (which loads `https://YOUR-APP/widget.js` from the same domain per `widget/README.md:9`). **This was the single most consequential frontend finding — fixed and empirically verified**: `netlify.toml`'s build command now runs `cd ../widget && npm ci && npm run build` before `next build`. Verified by deleting the existing `frontend/public/widget.js`, then running the exact chained command (`cd frontend && (cd ../widget && npm ci && npm run build) && npm run build`, reproducing Netlify's `base = "frontend"` semantics) from a cold state: the widget build regenerated `frontend/public/widget.js` (`published ...frontend\public\widget.js`), and the subsequent `next build` completed cleanly with all 39 routes — not just asserted, actually reproduced.

**API base URL handling** (server vs. client, confirmed by direct code read):
- Server-side (Server Components/Server Actions, used for all authenticated calls): `API_URL` env var, default fallback `http://localhost:8000` if unset (`frontend/src/lib/api.ts:5`, `frontend/src/app/page.tsx:5`). In the current Docker Compose topology this is set to the internal Docker hostname `http://api:8000` — **that value must NOT be reused on Netlify**; it must be set to the backend's real public origin (e.g. same as `API_ORIGIN`) in Netlify's environment variables, or the app will silently try to reach `localhost`/an unreachable Docker-internal name in production.
- Client-side (public, unauthenticated chat-widget embed snippet only): `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_APP_URL`, same `localhost` fallback pattern, used only in `frontend/src/app/website-chat/page.tsx:12-13`.
- None of these fallbacks are `NODE_ENV`-guarded — a forgotten env var in Netlify's dashboard fails silently (wrong backend origin) rather than failing the build loudly.

**Cross-origin cookie/auth architecture — confirmed SAFE for a split-domain deploy**: the frontend does not rely on the browser sending cookies cross-origin to the backend at all. The session cookie (`bizflow_session`, `httpOnly`, `secure` in production, `sameSite: "lax"`) is set on the frontend's OWN domain and holds the raw backend access token. All authenticated backend calls happen server-to-server (Next.js server → FastAPI) via `Authorization: Bearer <token>` (`frontend/src/lib/api.ts:35`), never a forwarded `Cookie` header. So moving the frontend to Netlify and the backend elsewhere does not require any `SameSite=None`/cross-site-cookie workaround — this architecture already assumed separate domains. The only direct browser→API call is the unauthenticated public chat widget, which explicitly omits credentials.

**`proxy.ts`** (Next 16 middleware, `frontend/src/proxy.ts`): a lightweight optimistic redirect based on cookie presence only (not a security boundary — real auth check happens server-side in `dal.ts`), scoped to `/dashboard`, `/onboarding`, `/knowledge`, `/website-chat`, `/integrations`. Uses only standard `next/server` APIs — confirmed compatible with Netlify's Next runtime.

**`next.config.ts`**: no `rewrites()`/`redirects()`/`images` config to port to `netlify.toml`. `headers()` sets standard security headers (`X-Content-Type-Options`, `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, HSTS in production) — Netlify's Next runtime should honor these as-is; worth a post-deploy smoke test since Netlify sometimes layers its own defaults.

**Hardcoded localhost occurrences** — all are unconditional `?? "http://localhost:..."` fallbacks (not unconditionally hardcoded into logic), listed above; the one genuinely guarded dev-only localhost reference is `frontend/src/lib/shopify.ts:5` (`LOCAL_HOSTS`), only reachable when `NODE_ENV !== "production"` (`frontend/src/app/integrations/shopify/actions.ts:64`).

**`NEXT_PUBLIC_*` inventory**: exactly two used in code (`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_URL`), both documented in `frontend/.env.example` and present with placeholder-only values in `frontend/.env.local` — no mismatch, no real credentials found in `.env.local`.

---

## E. Backend deployment requirements

**Runtime**: `backend/Dockerfile` — non-root user (`uid 10001`), `EXPOSE 8000`, exact CMD:
```
uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --no-server-header --timeout-keep-alive 5
```
Host binds `0.0.0.0` inside the container; the `127.0.0.1:8000` binding in `docker-compose.production.yml` is a compose-level port mapping, not a uvicorn flag — a reverse proxy is required in front of it. **One uvicorn process per container** (no `--workers` flag) — horizontal scaling means more containers, not more processes per container. `--proxy-headers` activates uvicorn's `ProxyHeadersMiddleware`, which only trusts `X-Forwarded-*` from peers listed in `FORWARDED_ALLOW_IPS` (verified against the installed uvicorn package, not just the docs). The same image also serves the worker role under a different command — see section F.

**Health/readiness endpoints** (`backend/app/api/health/router.py`):
| Path | Checks |
|---|---|
| `GET /health`, `GET /health/live` | Nothing — process-up only; this is what the container `HEALTHCHECK` uses, deliberately avoiding DB/Redis so an infra outage doesn't trigger a restart loop |
| `GET /health/ready` | Postgres `SELECT 1` **and** Redis ping; 503 + `Cache-Control: no-store` on failure — intended target for an orchestrator's readiness probe |
| `GET /health/db` | Postgres only, kept for existing callers |

**Authentication model — no cookies on the backend at all.** Confirmed by grep (`set_cookie`/`.cookie(`/`SameSite` — zero matches) and by reading every auth path: login returns a bearer token in the JSON body (`TokenResponse`), and `HTTPBearer` reads `Authorization: Bearer <token>` exclusively, including for platform-admin routes. This means the entire cross-domain `SameSite`/`Secure`/cookie-`domain` risk class the task asked about **does not apply to the backend** — there is nothing to misconfigure there. (The frontend independently sets its own first-party, `httpOnly`, `secure`-in-production cookie on the frontend's *own* domain to hold that token server-side — see section D — so the two pieces fit together correctly for a split-domain deploy.)

**Alembic/migrations at runtime**: `backend/alembic.ini` deliberately leaves `sqlalchemy.url` unset; `backend/app/db/migrations/env.py` falls back to `get_settings().database_url`, i.e. the real `DATABASE_URL` environment variable — confirmed migrations will never silently use a hardcoded dev URL.

*(Env var list: see section C.)*

---

## F. Worker deployment requirements

**Startup command**: `python -m app.workers.main`, run from the same image as the API (`backend/Dockerfile`), differentiated only by this command override (per `docker-compose.production.yml`) — not a separate build.

**Job queue**: Redis-backed, 7 priority lists served via a single `BLPOP` call, in fixed order: shopify-webhook > whatsapp-webhook > email-webhook > automation > crawl (default) > shopify-sync > email-campaign.

**Multi-instance safety — confirmed by design, not just by absence of an obvious bug**: job claiming is DB-row-based (atomic UPDATE), not Redis-ack-based, so duplicate deliveries across N worker processes self-reject. The worker's own code/docstrings explicitly anticipate running several processes for concurrency. One minor exception: `SubscriptionAuditor._last`, an in-process dict tracking Shopify-audit timing, causes redundant (not incorrect — every audit step is idempotent) per-worker audit calls if the worker is ever scaled beyond one process. Low severity, not a blocker (see section N, item 14).

**Graceful shutdown**: SIGTERM/SIGINT handlers set a stop-event; in-flight crawl/sync jobs check it mid-job and save progress/re-queue rather than being killed outright; a `finally` block stops the heartbeat and closes the queue connection on every exit path.

**Health**: `python -m app.workers.healthcheck` is the worker container's own `HEALTHCHECK` override (local heartbeat-file freshness check, or `--cluster` for a Redis-wide check across all worker instances). The API's `GET /health/worker` endpoint separately reports Redis worker-heartbeat freshness (503 if no live worker) for an external monitor that only has access to the API, not the worker container directly.

---

## G. Database requirements

**Migration strategy**: Alembic is authoritative. Production runs `alembic upgrade head` as a one-shot step before the API/worker start (per `docker-compose.production.yml`'s `migrate` service and `docs/production.md`). All 12 migrations (`0001`-`0012`) were read in full by the audit:

- Clean, linear chain, single head, no branching.
- Every migration implements both `upgrade()` and `downgrade()`.
- No destructive unconditional `DROP` on data-bearing tables/columns.
- No seed or demo data insertion is mixed into any schema migration — all seed/demo logic lives exclusively in `backend/tools/seed_*.py`, never in migration files.
- `0003` intentionally leaves the `vector` Postgres extension installed on downgrade (correct — extension is DB-wide, not table-scoped).
- `0012` (platform admin) uses nullable FKs with `ondelete="SET NULL"` for `platform_audit_events`, matching a bug fix documented in `docs/phase-10-final-report.md`.

**Documentation gap**: `docs/production.md` section 8 still says "Migrations 0001-0006, additive" and was never updated for `0007`-`0012`. Not a code defect — a stale doc. [Finding: docs-migration-count-stale]

**Database initialization**: no migration or startup code destroys existing data; `docker-compose.production.yml`'s `db` service persists to a named volume.

**Postgres connection**: `DATABASE_URL` (`postgresql+psycopg://user:pass@host:5432/db`), required, no default. `startup_checks.py` rejects a non-Postgres URL, a missing password, and a well-known-weak password (`bizflow, postgres, password, passw0rd, changeme, admin, root, secret, 123456`) in production; warns (doesn't block) on a remote host with no `sslmode`. Timeouts (`DB_CONNECT_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_SECONDS`, `DB_LOCK_TIMEOUT_SECONDS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`) all have sane defaults so a stuck query/lock/idle transaction can't hold a connection forever.

---

## H. Redis requirements

**Connection**: `REDIS_URL`, required to be explicitly set in production (the localhost default is rejected by `startup_checks.py` if relied upon). Used for both the job queue (see section F for the worker side of this) and, when `RATE_LIMIT_BACKEND=redis`, for distributed rate limiting.

**Finding — rate-limit backend not hard-enforced** [Finding: rate-limit-memory-not-blocked]: `RATE_LIMIT_BACKEND=memory` in production produces only a **warning**, not a blocking startup refusal (`startup_checks.py:258-262`). Worse, even when `RATE_LIMIT_BACKEND=redis` is correctly set, the Redis-backed limiter **silently falls back to the in-process memory limiter on any `redis.RedisError`** at request time (`backend/app/core/rate_limit.py:83-87`), logging only a `logger.warning` — meaning a transient Redis blip silently and temporarily weakens every rate limit in the app (auth, public chat, OAuth, webhook-bad-signature throttling, WhatsApp/email send limits) with no alarm. Recommend: treat this as a monitoring/alerting gap to flag to whoever owns production observability, not a code change required before first deploy (the fallback is a deliberate availability-over-strictness tradeoff per the code's own comment) — but it belongs in the readiness report.

---

## I. OAuth/webhook configuration

All callback/webhook routes identified, each with confirmed signature verification (all use `hmac.compare_digest` — constant-time, no timing-attack surface found):

| Route | Purpose | URL env var that must be set in production | Signature verification |
|---|---|---|---|
| `GET /api/integrations/shopify/callback` | Completes Shopify OAuth | `SHOPIFY_REDIRECT_URI` (`=${APP_ORIGIN}/integrations/shopify/callback`) — must exactly match the redirect URL registered in the Shopify Partner/Dev Dashboard | Present — `verify_oauth_hmac()`, HMAC-SHA256 over sorted query string |
| `POST /api/webhooks/shopify` | Receives Shopify order/product/etc. events | `SHOPIFY_WEBHOOK_BASE_URL` (`=${API_ORIGIN}`) — what the backend tells Shopify to call; required unless `SHOPIFY_WEBHOOKS_DISABLED=true` | Present — `verify_webhook_hmac()`, HMAC-SHA256 over raw body |
| `GET /api/webhooks/whatsapp` | Meta webhook handshake (challenge/response) | No dedicated URL env var — the webhook URL is registered manually in the Meta App dashboard; only `WHATSAPP_WEBHOOK_VERIFY_TOKEN` is configured here | Present — handshake token compared via `hmac.compare_digest` |
| `POST /api/webhooks/whatsapp` | Receives inbound WhatsApp messages/delivery status | Same — no URL env var, registered manually at Meta; `WHATSAPP_APP_SECRET` signs | Present — `verify_meta_signature()`, HMAC-SHA256 against `X-Hub-Signature-256` |
| `POST /api/webhooks/email` | Receives inbound email / delivery status (Resend) | No dedicated URL env var — configured directly with the email provider; `EMAIL_WEBHOOK_SECRET` is the relevant setting | Present — `verify_svix_signature()`, Svix-style HMAC-SHA256 |

All four inbound webhook endpoints apply a failed-signature rate limit (30 bad attempts/60s per client IP) that only counts rejected requests, so legitimate senders are never throttled by it.

**Action required before go-live** (external, cannot be done from this repo): register the production `SHOPIFY_REDIRECT_URI` in the Shopify app's allowed redirect URLs; register the production webhook URL in the Meta WhatsApp App dashboard; register the production webhook URL with the email provider (Resend). None of these can be invented — they depend on the real `APP_ORIGIN`/`API_ORIGIN` chosen at deploy time.

---

## J. Security requirements

**CORS — verified never combines wildcard + credentials**:
- Dashboard/API CORS (`backend/app/main.py`): `allow_credentials=True`, `allow_origins` from `CORS_ORIGINS` env var. `startup_checks.py` hard-rejects (in production) a wildcard, any non-https origin, any `localhost`/`127.0.0.1`/`::1` origin, or an origin with a path/query/fragment.
- Public widget CORS (`/api/public/*` only): `Access-Control-Allow-Origin: *`, but **never** `Access-Control-Allow-Credentials` — this path authenticates via an `X-Conversation-Token` header, not a cookie, and a middleware explicitly strips/overrides any credentialed CORS headers that might otherwise leak onto this path.
- **Verdict: no configuration path allows `CORS *` with credentials.** This was explicitly checked per the task's concern about cross-domain frontend/backend deployment.

**Cookies/CSRF**: the backend sets no cookies at all (pure Bearer-token auth in the JSON body) — the `SameSite`/`Secure`/cookie-`domain` risk class doesn't apply server-side. The frontend sets its own first-party `httpOnly` session cookie (`secure: true` in production, `sameSite: "lax"`) on its own domain only, holding the backend access token; it is never sent to the backend's origin, and the backend is called server-to-server with an `Authorization` header instead. No CSRF exposure from this design since there's no ambient-credential (cookie-based) authority on the backend's own origin for a third-party site to ride.

**Trusted headers**: `X-Forwarded-For`/`X-Forwarded-Proto` are only honored via uvicorn's `--proxy-headers` + `FORWARDED_ALLOW_IPS` allowlist (verified against the installed uvicorn package) — not spoofable by an external client as long as `FORWARDED_ALLOW_IPS` is scoped to the real proxy's address and the API port isn't independently exposed. Production refuses to start with this unset; only warns (doesn't block) if set to `*`.

**Platform-admin auth**: not a separate auth system — a platform admin is just a `User` row with `is_platform_admin=True`, checked fresh from the database on every request (never cached in the JWT), so revoking admin access is immediate. The "view as brand" feature is gated by the caller already being a verified platform admin before the view-as header is honored at all.

**Error handling / secret leakage — one real gap found** [Finding: unredacted-exception-logs]: the app's own `SafeFormatter` (which redacts JWTs, Shopify tokens, API keys, `Bearer`/`Basic` headers, and Postgres error-detail values) is attached only to the `"app"` logger namespace. An unhandled exception is re-raised by Starlette after the app's handler runs and is logged a second time by uvicorn's own `uvicorn.error` logger, which uses uvicorn's default (non-redacting) formatter. The HTTP response returned to the client stays generic either way — this is **not client-exposed** — but if an unexpected exception's `str()` happens to embed a credential (e.g. a raw DB-driver error echoing a connection string, or a provider SDK exception carrying a key in its message), that unredacted text would print to the container's stdout/stderr. Relevant to whoever aggregates/ships these container logs in the chosen hosting platform — worth a log-scrubbing step at the infra layer, or extending `SafeFormatter` to the root/uvicorn loggers too.

**Insecure defaults flagged**:
- `SECRET_ENCRYPTION_KEY` has **no unconditional production gate** — only checked if Shopify or WhatsApp happens to be configured. A deployment that configures Shopify/WhatsApp *later*, after initially deploying without them, would not be stopped by any startup check from running with this unset — it would just silently drop those jobs at runtime ("secret manager not usable").
- `RATE_LIMIT_BACKEND=memory` in production is a warning only, not a blocking refusal (see section H).

---

## K. Secret-management requirements

**Secret scan result: CLEAN.** A dedicated audit scanned the full working tree plus the complete git history (all 5 commits on `main`, filename-level and full-diff-content-level) for API keys, JWT secrets, encryption keys, DB/Redis passwords, Shopify secrets, WhatsApp/Meta tokens, email provider keys, webhook signing secrets, and private keys.

- **No real secret was found anywhere** — in the working tree, in any historical commit, or in any file ever added and later deleted.
- The only two real-looking (high-entropy, correctly-formatted) values found anywhere are `backend/.env`'s `JWT_SECRET_KEY` and `SECRET_ENCRYPTION_KEY` — both are locally-generated dev secrets, confirmed git-ignored (`git check-ignore -v backend/.env` → matched by `.gitignore`'s `.env` rule) and confirmed never tracked (`git ls-files` does not list it).
- Every other credential-shaped string found is an obvious test fixture (`SUPERSECRET`, `correct-horse-battery`, `never-print`, sequential digits) in `backend/tests/**`, which is expected and fine.
- `frontend/.env.local` contains only placeholder `localhost` dev URLs — no credential of any kind.
- The repo ships its own automated secret-hygiene test, `backend/tests/test_repo_secret_hygiene.py`, which scans exactly the committable file set for credential-shaped strings, URL-embedded credentials, untracked non-example env files, and frontend/widget references to server-side secret names — **30/30 passed** against the current working tree, independently corroborating the manual scan.
- **No git-history rewrite is needed.** Nothing requires removal from history.

**Demo-credential exposure check**: the documented demo password (`Demo12345!`, `backend/tools/seed_demo_platform.py`) appears only in backend tool/test/doc files — explicitly confirmed **absent** from `frontend/src/**`, `frontend/public/**`, `widget/src/**`, and `widget/dist/**`. The platform-admin credential is read only from environment variables, never hardcoded anywhere. The widget's shipped bundle (`frontend/public/widget.js`) contains only a public key placeholder and two example-URL comments — no secret, no internal hostname.

**Secret-management requirements for production**:
- Generate fresh values for `JWT_SECRET_KEY` (48+ random chars) and `SECRET_ENCRYPTION_KEY` (32 random bytes, base64) — never reuse the dev values from `backend/.env`.
- Store all production secrets in the hosting platform's secret manager / env var store (Netlify env vars for frontend; whatever the backend host provides) — never in a committed file.
- `SECRET_ENCRYPTION_KEY` loss makes stored Shopify/WhatsApp tokens unreadable (merchants simply reconnect); leaking it with a database copy exposes those tokens — treat it with the same care as a database credential.
- `.gitignore` gap found and should be closed before the first push to GitHub: `.vscode/phase7prompt.md` (20.7 KB, confirmed to contain no secrets — an internal phase-planning doc) is currently untracked but **not ignored**, so a routine `git add -A` would sweep it into the repo. See section N.

---

## L. Demo vs. Production separation

**Three demo/seed tools — gating gap found and FIXED** (`backend/tools/`):

| Tool | Production/environment gate on main path | Destructive-operation risk |
|---|---|---|
| `seed_platform_admin.py` | **Yes (pre-existing)** — refuses to run when `settings.is_production` is true unless `--i-know-what-im-doing` is explicitly passed (`seed_platform_admin.py:55-59`). | No deletes; only upserts one admin `User` row. |
| `seed_demo_platform.py` | **Was none; now fixed.** Previously had no check of `ENVIRONMENT`/`is_production` before seeding the 4 demo brands — only `--reset-demo` (destructive path) checked the **target org**'s `DEMO` tag (collision protection), not whether the **deployment** is production. Added the identical `is_production` + `--i-know-what-im-doing` gate used by `seed_platform_admin.py`, at the top of `main()`, before any DB session opens. Verified: running with `ENVIRONMENT=production` and no override now exits 2 with `FAIL refusing to run against a production environment...` before touching the database. | `--reset-demo` deletes an org, but only if it's already tagged `DEMO` and matches one of 4 hardcoded slugs. |
| `seed_operations_demo.py` | **Was none; now fixed.** Docstring claimed it "refuses to run unless explicitly invoked," but that only meant `--org-id`/`--org-slug` was a required CLI arg. Added the same gate as above. Verified: same refusal behavior confirmed by direct invocation. | No deletes/truncates (idempotent get-or-create, synthetic IDs offset into a reserved high range). |

**Fix verified empirically** (not just by inspection): both tools were invoked directly with `ENVIRONMENT=production` set and a full set of otherwise-valid production settings, with and without `--i-know-what-im-doing` — each refuses (exit code 2, no database session opened) without the flag, and proceeds past the gate with it, exactly mirroring `seed_platform_admin.py`'s existing, already-trusted pattern. [Finding: demo-seed-no-prod-gate — FIXED]

**Startup gate does enforce one piece of demo/prod separation**: `startup_checks.py`'s `_check_platform_admin` check refuses to boot in production if `PLATFORM_ADMIN_PASSWORD` is weak/placeholder — but this only protects the platform-admin seed input, not the demo-brand seed tools above.

**Frontend-side demo credential exposure — confirmed clean**: the secret-scan audit explicitly grepped `frontend/src/**`, `frontend/public/**`, `widget/src/**`, and `widget/dist/**` for the literal demo password and found zero matches — it never reaches browser-served code. The platform-admin credential is likewise never hardcoded anywhere (environment-only).

---

## M. Remaining external setup steps

These cannot be done from this repo/session — they require real accounts, real domains, and human decisions:

1. **GitHub**: create/choose the remote repo, push `main` (after the uncommitted Phase 6-10 work is committed — see section 13/Git state in the final report).
2. **Netlify**: create the site pointed at this repo; `netlify.toml` (already created — see Known Blockers) sets the base directory, Node version, and a build command that also builds `widget/` first; set env vars `API_URL`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_URL` in the Netlify UI to the real production origins (NOT the Docker-internal `http://api:8000` value from `docker-compose.production.yml`).
3. **Backend/worker host**: choose a container host (NOT Netlify — the task explicitly rules this out, and the audit confirms the backend has no serverless-shaped entry point, just a long-running uvicorn process + a long-running worker loop). `docker-compose.production.yml` is the closest existing artifact; it's a single-host topology and would need adaptation for a managed container platform.
4. **Managed PostgreSQL** with the `pgvector` extension available (the app enables it itself via migration `0003`, but the extension binary must exist on the server — confirm the chosen provider supports `pgvector`, e.g. not every managed Postgres does by default).
5. **Managed Redis**, reachable from both the API and worker containers, ideally with TLS (`rediss://`) and a password if exposed beyond a private network.
6. **Real AI provider account + API key** (OpenAI/Anthropic/or any supported OpenAI-compatible vendor) for both `LLM_PROVIDER`/`LLM_API_KEY` and `EMBEDDING_PROVIDER`/`EMBEDDING_API_KEY` — production refuses the `fake` provider.
7. **Shopify Partner/Dev Dashboard app** (if Shopify integration is wanted at launch): register `SHOPIFY_REDIRECT_URI` and configure webhooks against `SHOPIFY_WEBHOOK_BASE_URL`, or set `SHOPIFY_WEBHOOKS_DISABLED=true` to launch without live updates.
8. **Meta WhatsApp Business app** (if wanted at launch): register the production webhook URL in the Meta dashboard, obtain `WHATSAPP_APP_ID`/`WHATSAPP_APP_SECRET`/`WHATSAPP_WEBHOOK_VERIFY_TOKEN`.
9. **Email provider (Resend, per default `EMAIL_PROVIDER`)**: real API key, real inbound domain (replace the `*.invalid` placeholder), register the production webhook URL and secret with the provider.
10. **Reverse proxy / TLS termination** in front of `api`+`frontend` if self-hosting the backend via the existing compose file — it does not terminate TLS itself.

None of the above were invented in this audit — every URL/credential requirement is traced to a specific config check or compose variable.

---

## N. Known blockers

### Must fix before first deploy (code/config changes, within deployment-readiness scope)

1. **`demo-seed-no-prod-gate`** (section L) — `seed_demo_platform.py` and `seed_operations_demo.py` have no production/environment refusal gate on their main seeding path, unlike `seed_platform_admin.py`. An operator could accidentally seed the publicly-documented demo password into a real production database. **Fix applied — see section 15/17.**
2. **`widget-not-built-on-netlify`** (section D) — Netlify's plain `next build` never builds/copies `widget/dist/widget.js` into `frontend/public/widget.js`. Without intervention the deployed site 404s on `/widget.js`, breaking the website-chat preview and every merchant's embedded widget. **Fix applied — see section 15/17** (`netlify.toml` build command now builds `widget/` first).
3. **No `netlify.toml`** anywhere in the repo. **Fix applied** — created with base directory, chained build command (widget + frontend), and Node version pin. Deliberately no `[[plugins]]` block — verified against Netlify's current docs that the Next.js adapter auto-detects with zero config, and declaring the plugin without pinning+installing it is not the recommended path.
4. **No Node version pin for Netlify** (`engines`/`.nvmrc` absent). **Fix applied** — pinned via `netlify.toml`'s `NODE_VERSION`, matching `frontend/Dockerfile`'s `node:22-alpine`.
5. **`.vscode/phase7prompt.md` not git-ignored** (section K) — untracked internal planning doc (confirmed no secrets) that would be swept into the repo on a routine `git add -A`. **Fix applied** — narrow `.vscode/*.md` ignore rule added.
6. **`env-example-missing-phase6-10`** (section C) — root `.env.example`/`.env.production.example` don't document the ~45 WhatsApp/email/platform-admin settings `config.py` already reads. **Fix applied** — both files extended with the missing variables, each documented per the task's required format (purpose/required-optional/placeholder/consumer), without touching any variable already documented.

### Configuration tasks at deploy time (not code defects — operator must set correctly)

7. `docker-compose.production.yml`'s `API_URL=http://api:8000` (internal Docker hostname) must NOT be reused verbatim on Netlify — set `API_URL` in Netlify's env config to the backend's real public origin.
8. `SHOPIFY_REDIRECT_URI`, the WhatsApp webhook URL (Meta dashboard), and the email provider's webhook URL all need the real production `APP_ORIGIN`/`API_ORIGIN` filled in once chosen — none of these can be invented ahead of time.

### Documentation gaps (non-blocking, recommend fixing soon)

9. `docs-production-stale` — `docs/production.md` doesn't mention the WhatsApp/email/platform-admin startup-gate checks, undercounts the migration chain (says "0001-0006" vs actual 0001-0012), and never names `APP_ORIGIN`/`API_ORIGIN`. Left as-is per the task's "do not redesign/rewrite" instruction — flagging for a future doc-only pass rather than editing a Phase 4.1 doc extensively during a readiness audit.
10. Root `README.md`'s status line still says "Phase 5 complete" and doesn't mention Phases 6-10 at all.

### Hardening recommendations (not blockers — flagged for awareness, no code change made)

11. `SECRET_ENCRYPTION_KEY` has no unconditional production startup gate — only enforced if Shopify or WhatsApp is configured at boot time. A later `UPDATE`-in-place enabling of either integration without this set would silently drop jobs rather than fail loudly.
12. `RATE_LIMIT_BACKEND=memory` in production triggers only a warning, not a refusal; the Redis-backed limiter also silently falls back to per-process memory on any transient Redis error. Recommend monitoring/alerting on this at the infra layer.
13. Unhandled-exception logs bypass the app's own secret-redacting log formatter when re-logged by uvicorn's own `uvicorn.error` logger — not client-exposed, but worth scrubbing at the log-aggregation layer or extending `SafeFormatter`'s reach.
14. `SubscriptionAuditor` in-memory audit-timing dict causes redundant (not incorrect) per-worker Shopify audit calls if the worker is ever scaled beyond one process.

### For the user to decide (not auto-fixed — judgment call, not a defect)

15. Repo root has four more untracked phase-prompt files (`phase8.1.md`, `phase8_prompt.md`, `phase9.md`, `phase_10prompt.md`), the same genre as `.vscode/phase7prompt.md` (confirmed no secrets by inspection — these are literal prompts given to the coding agent for each phase). Unlike `.vscode/phase7prompt.md`, these sit at the repo root, which could mean they're intentionally kept as project history (similar to how `docs/phase-9-final-report.md` etc. are committed-worthy reports) rather than scratch files. Left untouched and NOT added to `.gitignore` — decide before the first push to GitHub whether these should be committed, moved into `docs/` as history, or ignored like the `.vscode` one.

---

## Appendix: Audit log (for traceability — not one of the task's requested A-N sections)

- Docs/migrations/demo-seed/startup-gate audit: COMPLETE.
- Secret scan / .gitignore / env-file audit: COMPLETE — clean (no real secret in working tree or git history).
- Backend/worker deployment config audit: COMPLETE.
- Frontend/Netlify/widget audit: COMPLETE.
- Frontend typecheck (`next typegen && tsc --noEmit`): PASS.
- Frontend lint (`npm run lint`): PASS, no errors/warnings.
- Frontend production build (`npm run build`): PASS, all routes compiled.
- Backend unit test suite (`pytest -m "not integration"`), first run with Redis/Postgres not started: 42 failed / 14 errors, all traced to `redis.exceptions.TimeoutError` (no Redis running) — environment artifact, not a code defect.
- Dev Postgres + Redis started (`docker compose up -d db redis`, non-destructive, named volume preserved), full unit suite rerun: **1 failed, 3739 passed, 17 skipped, 142 deselected** — exactly matches the count already documented in `docs/phase-10-final-report.md`. The one failure (`tests/shopify/test_log_audit.py::test_token_refresh_and_a_revoked_token_log_no_token_material`) is the same pre-existing, already-documented Phase 5/6 token-refresh test bug, explicitly out of scope for this audit (not caused by, or related to, any change made here). Containers stopped and removed afterward (`docker compose down`, named volume preserved) to leave the environment as found.
- Fixes applied: production-refusal gate added to `seed_demo_platform.py` and `seed_operations_demo.py` (verified empirically — refuses with exit 2 before any DB session opens); `netlify.toml` created; `.gitignore` extended for `.vscode/*.md`; root `.env.example`/`.env.production.example`/`backend/.env.example` extended with Phase 6-10 (WhatsApp/email/platform-admin) variables. Re-verified after fixes: both edited tool scripts compile and their `--help` output confirms the new flag is wired in; `py_compile` clean.
- `netlify.toml`'s build chain was not just asserted but reproduced: `frontend/public/widget.js` was deleted, then the exact command (`cd ../widget && npm ci && npm run build && cd ../frontend && npm run build`) was run from that cold state and regenerated the file before `next build` completed cleanly.
- The `[[plugins]]`/adapter question was checked against Netlify's live, current documentation (fetched during this audit, not answered from training-data recall) rather than assumed — confirmed the modern Next.js-on-Netlify adapter auto-detects with zero config and that explicitly declaring `@netlify/plugin-nextjs` is only for version-pinning, so no plugin block was added.
