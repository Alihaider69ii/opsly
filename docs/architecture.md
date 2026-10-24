# Architecture

## Product

BizFlow AI is an AI automation layer between a business's systems (Shopify, website, WhatsApp) and
its customers. It combines business data and conversations in one system to answer questions,
recommend products, capture leads, maintain customer profiles and run follow-ups. It is a
multi-tenant SaaS, initially for D2C/e-commerce.

```
Business systems -> BizFlow AI -> Customers
```

## Repository

```
bizflow-ai/
  backend/    FastAPI app, SQLAlchemy models, Alembic migrations, tests, ops tools, mutation runner
  frontend/   Next.js dashboard (App Router, TypeScript, Tailwind)
  widget/     Embeddable website chat widget (separate package)
  e2e/        Real-browser end-to-end tests with an orchestrator that starts an isolated stack
  database/   DB-level assets; migrations live in backend/
  docs/
  docker-compose.yml             development Postgres + Redis (loopback only)
  docker-compose.production.yml  a working production topology (see production.md)
```

Modular monolith: one backend deployable, modules separated by folder. No microservices.

### Backend layout

```
backend/app/
  main.py          app factory, CORS, router registration (business routes live under /api)
  core/            config.py (env settings), security.py (Argon2id + JWT primitives),
                   rate_limit.py (the one limiter: memory or Redis; auth and public chat),
                   logging.py (log_event), public_http.py (CORS + body cap for the public API),
                   secrets.py (SecretManager: AES-256-GCM for integration credentials),
                   startup_checks.py (production gate: refuse to start unsafely),
                   request_context.py (request ids), body_limit.py (global body cap)
  db/              base.py (Base, mixins), database.py (engine/session), scoping.py (tenant-scoped
                   queries), models/, migrations/
  api/deps.py      request dependencies: current_user, current_org, org_from_path, require_org_role
  api/<module>/    HTTP routers per domain: health/, auth/, organizations/, knowledge/, chat/
                   (dashboard widget settings), public_chat/ (no-login visitor API),
                   integrations/ (Shopify dashboard API), webhooks/ (Shopify webhook intake)
  schemas/         Pydantic request/response models (auth, user, organization, knowledge, chat,
                   shopify)
  services/        business logic.
                     auth/, organizations/     identity and tenancy
                     crawler/                  urls, ssrf, fetcher, robots, extract, crawler
                     rag/                      chunking, indexing, search (vector search)
                     ai/                       provider catalog + registry + adapters (LLM and
                                               embeddings, vendor-neutral), rag, prompts, agent
                     chat/                     widgets, conversations, tokens, locking, messages
                     knowledge/                source lifecycle, crawl runner, error catalogue
                     integrations/shopify/     scopes, validation, client, auth (token lifecycle),
                                               oauth, mapper, fetch, store (upserts), sync,
                                               webhooks, lifecycle (disconnect/purge), service
                     commerce/                 read service over imported products/customers/orders
                   Planned: whatsapp/ leads/ automation/ (integration code stays isolated)
  workers/         queue.py (Redis job queue: crawl, Shopify sync, Shopify webhook jobs, automation
                   heartbeats), main.py (the worker process: crash containment, maintenance loop),
                   heartbeat.py / healthcheck.py (liveness for the orchestrator)
backend/tools/     operator tools, run as `python -m tools.<name>`: ai_live_check,
                   shopify_live_check, customer_data_export, reencrypt_secrets
backend/mutation/  mutation-testing runner and catalogs (engineering tool, not CI)
```

Deviations from the suggested structure: none structural. Migrations live at
`app/db/migrations` (configured in `alembic.ini`). `services/*` subpackages are created when the
phase that needs them starts, rather than up front. Routers stay thin (HTTP concerns only); rules
live in `services/`, which raise domain exceptions that the routers map to status codes.

### Frontend layout

```
frontend/src/
  proxy.ts           optimistic guard (cookie present?) for the authenticated pages
  lib/               api.ts (server-only backend client), session.ts (cookie), dal.ts (Data Access
                     Layer), form.ts, request.ts, types.ts
  app/(auth)/        /login, /signup and their Server Actions (login, signup, logout)
  app/onboarding/    create the first organization
  app/dashboard/     authenticated page proving user + organization context
  app/knowledge/     knowledge base: add a website, source list with live status, source detail
                     with discovered pages, page text; re-crawl and delete (Server Actions)
  app/website-chat/  embed snippet, widget settings, live preview of the real widget
  app/integrations/shopify/  connect, status, sync progress, Sync now, two-step Disconnect, and the
                     OAuth callback route handler (forwards Shopify's signed query to the API)
  components/        form-ui, centered-card, redirect-if-signed-in, app-header, status-badge
```

`widget/` is a separate package (vanilla JS, esbuild, jsdom tests). Its build publishes
`frontend/public/widget.js`, the file customers embed.

### Key decisions

- **Sync SQLAlchemy 2.0 + psycopg 3.** Simpler and less error-prone than async for the MVP; FastAPI
  runs sync endpoints in a threadpool. Revisit only if profiling demands it.
- **UUID primary keys**, `created_at`/`updated_at` via mixins, deterministic constraint naming.
- **Enums stored as VARCHAR + CHECK**, not native PG enums, so adding values is a trivial migration.
- **Timestamps:** `created_at` is set application-side (microsecond precision) as well as by a
  server default, so "earliest row" ordering is deterministic on every database.
- **pgvector** for embeddings in the same Postgres. The Docker image ships the extension; migration
  `0003` enables it explicitly. Exact (non-ANN) search; see [knowledge-base.md](knowledge-base.md).
- **Two test tiers.** The default tier runs on in-memory SQLite (foreign keys enabled) and needs no
  services. `pytest -m integration` runs against the real Postgres/pgvector and Redis from
  docker-compose, in its own databases, building the schema with the real migrations.
- **Queue and worker.** A Redis list delivers jobs; Postgres is the source of truth for job state
  (atomic claim, heartbeat, reaper). No Celery. The worker is its own process
  (`python -m app.workers.main`). Three job families share it, in priority order: Shopify webhooks
  (small), website crawls, and Shopify imports (long, time-sliced, re-queue themselves).
- **Vendor-neutral AI.** `LLMProvider` and `EmbeddingProvider` are implemented by adapters chosen by
  a registry from a data catalog; no code branches on a vendor. See [ai-providers.md](ai-providers.md).
- **Secrets at rest.** Integration credentials are encrypted with AES-256-GCM by one reusable
  `SecretManager`, with the ciphertext bound to the row it belongs to; keys are versioned and
  rotatable. Nothing secret is ever returned by an API or logged.

## Authentication

```
Browser --(form post)--> Next.js Server Action --(Bearer JWT)--> FastAPI --> Postgres
   ^                            |
   +---- HttpOnly cookie <------+   (the token never reaches browser JavaScript)
```

**Backend.** `POST /api/auth/register` creates the user. `POST /api/auth/login` verifies the
password and returns a signed access token plus the user. Everything else expects
`Authorization: Bearer <token>`. The token is an HS256 JWT carrying **only** `sub` (user id), `iat`
and `exp`. Organization and role are never in the token; they are resolved from the database on each
request, so membership changes take effect immediately.

- **Passwords:** Argon2id (argon2-cffi defaults: 64 MiB, t=3, p=4) with a per-hash salt, never stored
  or logged in plaintext. Length 10 to 128 (the upper bound limits hashing work per request).
- **Emails:** trimmed and lower-cased in the service layer (the single source of truth); unique.
- **Login errors:** a wrong password, an unknown email and a deactivated account all return the same
  `401 Invalid email or password`. An unknown email still costs one Argon2 verification, so response
  time does not reveal which accounts exist.
- **Tokens:** the algorithm is pinned (never taken from the token or from config); `sub`, `iat`,
  `exp` are required. Expired, tampered, wrong-key, `alg=none` and wrong-algorithm tokens are
  rejected. `current_user` also requires the user to exist and be `is_active`, so deactivating an
  account revokes access immediately, even for unexpired tokens.
- **Rate limiting:** login and register are limited per client IP (default 10 per 60 s; `429` with
  `Retry-After`). The limiter is in-process; use a shared store (Redis) once there are multiple
  backend workers. See "Client IP and the frontend proxy" below.
- **Not built yet (deliberately):** email verification, password reset, refresh tokens, MFA.

### Session decision (frontend)

The Next.js server acts as a backend-for-frontend. Server Actions call the API server-to-server and
store the access token in a cookie named `bizflow_session`: **HttpOnly** (JavaScript cannot read
it), **Secure** in production, **SameSite=Lax**, path `/`, `Max-Age` equal to the token lifetime.

Why: tokens kept in `localStorage`/`sessionStorage` can be stolen by any XSS, whereas an HttpOnly
cookie cannot. Server Actions only accept same-origin POSTs (Origin check) and the cookie is
`SameSite=Lax`, which together cover CSRF. The backend stays a plain Bearer-token API, so the
widget, webhooks and other clients can use it unchanged. Alternatives rejected: tokens in browser
storage (XSS exposure), and a sealed-cookie session library (an extra dependency that adds nothing
here, since the cookie already holds a signed token the browser cannot read).

- Pages read the session only through the Data Access Layer (`frontend/src/lib/dal.ts`), which
  has the backend validate the token on every request. `src/proxy.ts` is only an optimistic guard
  that bounces cookie-less requests for `/dashboard` and `/onboarding` to `/login`.
- A stale or garbage cookie fails verification, lands on `/login`, and is overwritten on the next
  login. There is no redirect loop because the guard never redirects *away* from `/login` based on
  cookie presence alone.
- Server Actions re-check the session themselves; they are public endpoints.
- **Limits, accepted for the MVP:** no server-side revocation (logout clears the cookie, but the
  token stays valid until it expires, 8 h by default, unless the user is deactivated); a password
  change does not invalidate existing tokens; no refresh tokens. Planned hardening: short-lived
  access tokens with refresh rotation, and a per-user token version.

### Client IP and the frontend proxy

Because the browser talks to Next.js and Next.js talks to the backend, the backend would otherwise
see every user as the Next.js server's IP, and the per-IP rate limit would become global. The
frontend therefore forwards the original `X-Forwarded-For` chain on login and register, and
uvicorn's trusted-proxy handling (`FORWARDED_ALLOW_IPS`, default loopback) resolves the real client.
In deployment, the frontend must sit behind a proxy or platform that sets `X-Forwarded-For`, and the
backend must not be directly reachable by untrusted clients while it trusts that header.

## Multi-tenancy

```
User -> OrganizationMember (role) -> Organization -> business data
```

- A user can belong to many organizations; the membership row carries the role. The creator of an
  organization becomes its **OWNER**, created in the same transaction as the organization.
- `organizations`: `name`, unique `slug`, optional `website` (http/https only) and `business_type`.
  Slugs are generated from the name (ASCII-folded, de-duplicated with a short random suffix) or
  supplied explicitly and validated; a small set of route-like words is reserved.
- Every business-owned table carries `organization_id` (FK, indexed).

### How isolation is enforced (backend, not UI)

1. **Authenticate:** every non-public route depends on `current_user`.
2. **Prove membership:** an organization is only ever resolved through
   `services/organizations.resolve_org_context`, which looks up a membership row for *this* user and
   *this* organization and returns an `OrgContext(user, organization, role)`. There is no other way
   to obtain one, so holding an `OrgContext` is proof of membership.
   - `org_from_path`: for `/organizations/{organization_id}/...`. Authenticate, verify membership,
     resolve, then act.
   - `current_org`: for routes without an id in the path. The optional `X-Organization-ID` header
     only *selects among* organizations the user already belongs to; without it, the earliest
     membership is used. A non-member id is rejected like a missing one.
3. **Do not leak existence:** a non-member gets `404`, identical to a nonexistent organization.
4. **Scope every query:** tenant data is read through
   `db/scoping.scoped_select(Model, organization_id)`, which raises `TypeError` for a model without
   `organization_id`. Chain `.where(Model.id == x)` onto it and a row id belonging to another tenant
   resolves to nothing (IDOR-safe).
5. **Rules for every future feature:** a route touching business data depends on `CurrentOrg` or
   `PathOrg`; it never accepts `organization_id` in a body or query string as authority; queries
   start from `scoped_select`; new tenant tables get an `organization_id` FK and index.

Tests assert all of this, including a mutation check: removing the user filter from
`resolve_org_context` makes 12 isolation tests fail. Postgres row-level security remains a possible
defense-in-depth layer. Public surfaces (website widget, webhooks) resolve the organization from a
public site id or the integration record, never from a secret held by the client.

## Authorization

Roles are per organization (a user can be OWNER of one and MEMBER of another).

| Action | OWNER | ADMIN | MEMBER |
| --- | :-: | :-: | :-: |
| View the organization | yes | yes | yes |
| List members | yes | yes | no |
| Add a `MEMBER` | yes | yes | no |
| Add an `ADMIN` | yes | no | no |
| Grant `OWNER` | never | never | never |

Knowledge base (Phase 2): every member can read sources, documents and crawl status; **adding a
website, re-crawling and deleting need OWNER or ADMIN** (they spend crawl/embedding budget or remove
data). Enforced by `require_current_org_role` in the API, not by hiding buttons.

`OWNER` is set once, at organization creation; ownership transfer is a later feature. An
insufficient role gives `403`; a non-member gets `404` (see above). The grant matrix lives in
`services/organizations.GRANTABLE_ROLES`, and the caller's permission is checked *before* any user
lookup, so an unauthorized caller learns nothing about which emails are registered.

## API (Phase 1)

All routes are under `/api` except health. "Auth" means a Bearer token is required.

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/health`, `/health/live` | public | Liveness: the process is up (touches nothing else) |
| GET | `/health/ready` | public | Readiness: PostgreSQL and Redis answer (`503` otherwise) |
| GET | `/health/worker` | public | Is any background worker beating? (`503` if none; alert on it) |
| GET | `/health/db` | public | Database only (kept for existing callers) |
| POST | `/api/auth/register` | public | Create account (`email`, `password`, optional `full_name`); 201 with the user |
| POST | `/api/auth/login` | public | `email`, `password` returns `access_token`, `token_type`, `expires_in`, `user` |
| GET | `/api/auth/me` | auth | Current user |
| POST | `/api/organizations` | auth | Create organization (`name` required; `slug`, `website`, `business_type` optional); caller becomes OWNER |
| GET | `/api/organizations` | auth | Organizations the caller belongs to, with their role |
| GET | `/api/organizations/current` | auth | Current organization (see `current_org`) |
| GET | `/api/organizations/{organization_id}` | member | One organization |
| GET | `/api/organizations/{organization_id}/members` | OWNER/ADMIN | List members |
| POST | `/api/organizations/{organization_id}/members` | OWNER/ADMIN | Add an already-registered user (`email`, `role` of `ADMIN` or `MEMBER`) |

### Knowledge base API (Phase 2)

Organization comes from `current_org` (optional `X-Organization-ID` selects among the caller's own
organizations). Foreign and nonexistent ids both return `404`.

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/knowledge/sources` | OWNER/ADMIN | Add a website (`{"type":"WEBSITE","url":...}`); queues a crawl; 201 with status `PENDING` |
| GET | `/api/knowledge/sources` | member | Sources with status, page count, last crawl, safe error |
| GET | `/api/knowledge/sources/{id}` | member | One source |
| POST | `/api/knowledge/sources/{id}/crawl` | OWNER/ADMIN | Re-crawl (202; 409 if already running; 429 + `Retry-After` during the cooldown) |
| DELETE | `/api/knowledge/sources/{id}` | OWNER/ADMIN | Delete the source, its documents and chunks (204) |
| GET | `/api/knowledge/sources/{id}/documents` | member | Paginated pages (`limit` 1-200, `offset`) with a text preview |
| GET | `/api/knowledge/sources/{id}/documents/{document_id}` | member | One page with its full cleaned text |

### Website chat API (Phase 3)

Dashboard (authenticated, organization from `current_org`):

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/chat/widget` | member | The organization widget: public key, enabled, welcome message, allowed origins |
| PATCH | `/api/chat/widget` | OWNER/ADMIN | Update `enabled`, `welcome_message`, `allowed_origins` |

Public (no login; the public key in the path is the only identifier, the organization is never
accepted from the client):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/public/chat/{public_key}/config` | Business name and welcome message |
| POST | `/api/public/chat/{public_key}/conversations` | Start a conversation; returns `conversation_id` and a secret `token` |
| POST | `/api/public/chat/{public_key}/messages` | Send a message (`X-Conversation-Token` header); returns the assistant reply |
| GET | `/api/public/chat/{public_key}/conversations/{id}` | History (`X-Conversation-Token` header) |

Details, status codes and the security model: **[ai-brain.md](ai-brain.md)**.

### Shopify API (Phase 4)

Dashboard (authenticated, organization from `current_org`; the organization is never taken from a
URL, body or Shopify's callback):

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/integrations/shopify` | member | Connection, sync progress per resource, granted scopes, imported counts. Never tokens or personal data |
| POST | `/api/integrations/shopify/connect` | OWNER/ADMIN | `{"shop": "name.myshopify.com"}`; returns `authorization_url` |
| GET | `/api/integrations/shopify/callback` | OWNER/ADMIN | Completes OAuth; takes Shopify's signed query string untouched |
| POST | `/api/integrations/shopify/sync` | OWNER/ADMIN | Queue a manual sync (202; 409 if one is running; 429 + `Retry-After` inside the cooldown) |
| POST | `/api/integrations/shopify/disconnect` | OWNER/ADMIN | Discard credentials, delete webhook subscriptions, purge imported data |

Public (no login; authenticated by Shopify's HMAC signature over the raw body):

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/webhooks/shopify` | Verify, deduplicate by delivery id, record, queue, answer 200 |

Details and the security model: **[shopify.md](shopify.md)**.

Interactive docs: `http://localhost:8000/docs`. Errors are `{"detail": ...}`; validation errors are
`422` with per-field detail.

## Knowledge base

Websites are crawled by a background worker (SSRF-hardened, robots.txt-aware, bounded), cleaned,
chunked, embedded and stored with pgvector, all scoped to the organization. Everything about it,
including the SSRF model, crawl limits, chunking, the embedding abstraction, vector search and the
worker, is in **[knowledge-base.md](knowledge-base.md)**.

## AI brain and website chat

A visitor question is answered from that business own knowledge base: query embedding,
organization-scoped vector search, a relevance threshold, then a structured prompt to the LLM. When
nothing relevant is found the model is not called and a fixed fallback is returned. Conversations
and messages are stored per organization with database-enforced tenant integrity. The public API is
keyed by a public widget key and per-conversation secret tokens, and is rate limited, size-capped
and CORS-restricted to credential-less requests. The widget is a Shadow DOM script that holds no
secrets. Everything about it, including provider configuration, the retrieval pipeline, prompt
design, the API and the limitations, is in **[ai-brain.md](ai-brain.md)**.

## AI providers

The chat agent and the knowledge base depend on two interfaces. Which vendor implements them is a
row in a provider catalog plus configuration, chosen separately for the language model and for the
embeddings; the application exchanges only vendor-neutral request, response and error objects with
them. Changing the embedding model never mixes incompatible vectors and never deletes data.
Everything about it is in **[ai-providers.md](ai-providers.md)**.

## Shopify integration

A merchant connects a store through OAuth; BizFlow imports products, variants, customers and orders
into tenant-scoped tables and keeps them current with webhooks. A webhook is only a notification:
the worker re-reads the object from Shopify, so replays and out-of-order delivery are harmless.
Imports are queued, resumable, rate-limit aware and idempotent. Nothing in the AI agent calls
Shopify; Phase 5 will read the imported tables through `services/commerce`. Everything about it,
including the OAuth and token design, sync, webhooks, disconnect semantics and limitations, is in
**[shopify.md](shopify.md)**.

## Running it in production

Phase 4.1 added the operational layer: a start-up gate that makes the API and the worker refuse an
unsafe production configuration; liveness, readiness and worker-health endpoints; request ids on
every response and log line; a global body cap; PostgreSQL statement/lock/idle timeouts; a worker
that survives a crashing job, a Redis outage and a database outage and is recovered by a
maintenance loop; webhook-subscription self-healing; a log pipeline that redacts after formatting;
non-root, read-only container images and a production compose topology; security headers on the
dashboard. The honest ledger of what is implemented, verified, live-tested and externally blocked, the
retry and recovery thresholds, the secrets inventory and the failure-injection results are in
**[production.md](production.md)**.

## Automation engine (Phase 5 implementation)

The implementation is documented in [automation.md](automation.md). PostgreSQL owns workflow
definitions, internal events, execution snapshots/steps, durable due times, leads and tasks. The
existing Redis queue and worker wake and execute persisted work; the worker maintenance pass
recovers queued, due and stale work. There is no second worker architecture or public event-injection
route. Shopify webhook processing remains the source of Shopify events and emits deduplicated
internal events from normalized rows.

Definitions use explicit trigger, condition and action schemas with fail-closed operators. Executions
retain immutable definition/event snapshots, and tenant-aware composite keys constrain ownership
in the database. AI content generation uses the existing provider registry and treats event fields
as untrusted data. Messaging has an explicit provider interface but no configured delivery provider.
Lead capture is visitor-initiated through the existing website chat and does not promote anonymous
visitors to customers. `cart_abandoned` is a typed internal trigger only until a supported cart event
source exists. Phase 5's unverified production gates are tracked in [phase-5-progress.md](phase-5-progress.md).

## Planned subsystems (not yet built)

- **Tool-calling agent:** the LLM provider interface exists; a tool registry (`search_products`,
  `create_lead`, `handoff_to_human`, ...) does not. Every tool will receive the organization
  context, so one brand agent can never read another brand data. The chat agent today has no tools.
- **More knowledge sources:** uploaded PDFs, FAQs and manual entries are new source types on the same
  tables.
- **Using the Shopify data:** product recommendations, order status answers and customer profiles
  read the imported tables through `services/commerce`; no agent tool exists yet.
- **WhatsApp** (`services/whatsapp/`): Meta Cloud API. Each organization connects its own WABA and
  phone number; webhook signature verification; tokens encrypted at rest and never returned by APIs.
- **WhatsApp:** real provider delivery, credentials, webhooks and delivery-state handling.
- **Lead pipeline:** qualification statuses and scoring beyond captured contact records.

## Security baseline

Implemented: Argon2id password hashing, JWT auth, organization-level isolation and role
authorization, Pydantic validation on every input, CORS from env, a required strong JWT secret,
HttpOnly session cookie, per-IP rate limiting on login/register, no secrets in code or frontend.
Phase 2 added: SSRF protection for the crawler (resolve-validate-pin, manual redirect validation,
port allow-list, size/time/page limits, robots.txt), composite tenant foreign keys, safe error
catalogue (no stack traces or internals in the database or API), secrets as `SecretStr`, and
org-level abuse limits on sources and re-crawls. Phase 3 added: a public chat API that never
accepts an organization id from the browser, hashed per-conversation secret tokens, a unified
Redis-capable rate limiter, a body-size cap and credential-less CORS on public routes, grounding
enforced in code (no relevant knowledge, no model call), prompt-injection framing and leak
detection, and logging that rejects sensitive field names. Phase 4 added: AES-256-GCM encrypted
integration credentials with context binding and key rotation, strict `*.myshopify.com` validation
for every Shopify host, OAuth with a hashed, single-use, short-lived `state` bound to user,
organization and shop plus callback HMAC verification, webhook HMAC verification over the raw body
with delivery-id deduplication and a body-size cap, composite foreign keys that make cross-tenant
commerce rows impossible at the database, API schemas built from explicit field lists so a token
or customer's personal data cannot be serialized by accident, and redaction of credential-like query
parameters (the OAuth `code`, `state`, `hmac`) in the access log. Phase 4.1 added: a production
start-up gate; `SecretStr` for every credential including the JWT signing key, with credentials
masked in the `repr` of settings; log redaction after formatting for tokens, bearer credentials and
values that PostgreSQL error messages echo, with `httpx` request lines switched off; request-body
and statement/lock/idle-transaction limits; security headers (no framing, `nosniff`, referrer and
permissions policies, HSTS) on the dashboard; a route-by-route authorization and tenant matrix test;
a schema audit that fails the build for a tenant table without an organization or a cross-tenant
reference; an encryption-key rotation tool; a purge that cannot be raced by an in-flight import; and
dependency audits (`cryptography` moved past four advisories).

Known gaps and trade-offs (Phase 1):

- **Add-member-by-email reveals whether an email is registered** to an OWNER or ADMIN of an
  organization. Acceptable as a stopgap; replace with email invitations (which need the email
  provider that verification and password reset will also need).
- **Registration reveals duplicate emails** (`409`), the usual trade-off for useful signup errors.
  Email verification would let this become uniform later.
- No email verification, password reset, MFA, refresh tokens or token revocation (see above).
- Any authenticated user can create any number of organizations. Add limits with billing/plans.
- The rate limiter is per backend process unless `RATE_LIMIT_BACKEND=redis` (Phase 3).
