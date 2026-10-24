# BizFlow AI

AI automation layer for D2C / e-commerce businesses: connects a brand's Shopify store, website and
WhatsApp Business account, and uses AI to answer customers, capture leads and run follow-ups.

Status: **Phase 4.1 baseline complete; Phase 5 implementation underway and not yet production-gated**
(authentication, organization tenancy, knowledge base and website
crawler, AI brain with RAG, an embeddable website chat widget, vendor-neutral AI providers, a Shopify
integration that imports products, customers and orders, and the production hardening around all of
it). **Not yet run against a real Shopify store or a real AI vendor account** (no credentials exist in
this repository's environment): those steps are documented runbooks with checker tools, and every doc
labels what is implemented, verified, live-tested or blocked. Start with
[docs/production.md](docs/production.md); then [docs/progress.md](docs/progress.md),
[docs/architecture.md](docs/architecture.md), [docs/knowledge-base.md](docs/knowledge-base.md),
[docs/ai-brain.md](docs/ai-brain.md), [docs/ai-providers.md](docs/ai-providers.md) and
[docs/shopify.md](docs/shopify.md).

## Layout

| Path | Purpose |
| --- | --- |
| `backend/` | FastAPI + SQLAlchemy + Alembic, crawler, AI providers, RAG, LLM agent, chat API, Shopify integration, worker |
| `frontend/` | Next.js (App Router) + TypeScript + Tailwind |
| `widget/` | Embeddable website chat widget (vanilla JS, built to `frontend/public/widget.js`) |
| `e2e/` | Browser end-to-end tests: one command starts an isolated stack and runs them |
| `database/` | DB-level assets (migrations live in `backend/`) |
| `docs/` | Production guide, architecture, progress, knowledge base, AI brain, AI providers, Shopify |
| `docker-compose.yml` | **Development** Postgres and Redis only (bound to loopback) |
| `docker-compose.production.yml` | A working single-host **production** topology (see [docs/production.md](docs/production.md)) |

## Prerequisites

Python 3.12+, Node 20+, Docker (for Postgres with pgvector, and Redis).

## Run locally

Five processes: Postgres, Redis, the API, the worker (crawls websites and runs Shopify imports and
webhooks) and the frontend (plus a one-time widget build).

```bash
# 1. Postgres 16 + pgvector, and Redis
docker compose up -d db redis

# 2. Backend setup (once)
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt
cp .env.example .env              # then set JWT_SECRET_KEY (see below); the app won't start without it
alembic upgrade head              # creates the schema and enables the pgvector extension

# 3. API  (http://localhost:8000, docs at /docs)
uvicorn app.main:app --reload

# 4. Worker (a second terminal, same venv; crawls websites, imports Shopify data, applies webhooks)
python -m app.workers.main

# 5. Widget (once, and after editing widget/src): builds frontend/public/widget.js
cd widget
npm install
npm run build

# 6. Frontend (http://localhost:3000)
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

Generate the JWT secret (paste the output into `backend/.env` as `JWT_SECRET_KEY=`):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then open http://localhost:3000, sign up, create your business, open **Knowledge Base**, enter
`https://example.com` and click **+ Add Website**. The source goes PENDING -> CRAWLING -> COMPLETED
and its pages appear under **Open**.

### AI providers: fake, OpenAI, or anything OpenAI-compatible

BizFlow is not tied to one vendor. The language model and the embeddings are chosen independently
with `LLM_PROVIDER` / `EMBEDDING_PROVIDER` (`openai`, `openrouter`, `groq`, `together`, `deepseek`,
`mistral`, `gemini`, `custom_openai_compatible` for any OpenAI-style endpoint such as a local model
server, or `fake`), each with its own `*_MODEL`, `*_BASE_URL` and `*_API_KEY`. The example settings
use `fake`: deterministic, offline and **not semantic**, enough to run the whole pipeline locally and
refused when `ENVIRONMENT=production`. For real answers set, for example, `LLM_PROVIDER=openai`,
`LLM_API_KEY=...` and `EMBEDDING_PROVIDER=openai`, `EMBEDDING_API_KEY=...` in `backend/.env` (never
commit it); `OPENAI_API_KEY` still works for the `openai` provider. Without a usable key a crawl fails
with a clear "embeddings are not configured" message and the chat answers "temporarily unavailable"
(the reason is only in the server log). Changing the embedding model is safe and non-destructive:
see [docs/ai-providers.md](docs/ai-providers.md).

### Connect a Shopify store

1. Set `SECRET_ENCRYPTION_KEY` in `backend/.env`; it encrypts stored Shopify tokens and the Shopify
   page refuses to connect without it:

   ```bash
   python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```

2. Create an app in the Shopify Dev Dashboard (or Partner Dashboard), add
   `http://localhost:3000/integrations/shopify/callback` as an allowed redirect URL, and put its
   credentials in `backend/.env`: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` (the defaults for
   `SHOPIFY_REDIRECT_URI`, `SHOPIFY_API_VERSION` and `SHOPIFY_SCOPES` are right for local use).
3. Restart the API and the worker, open **Shopify**, enter `your-store.myshopify.com` and approve.
   The first import runs in the worker; counts and progress update on the page.

Live updates (webhooks) need a public https address Shopify can reach, set as
`SHOPIFY_WEBHOOK_BASE_URL`; on localhost leave it blank and use **Sync now**. Details, scopes, the
security model and limitations: [docs/shopify.md](docs/shopify.md).

### Try the website chat

1. Add a website under **Knowledge Base** and wait for **COMPLETED**.
2. Open **Website Chat**: the page shows your embed snippet and settings, and the chat button
   appears at the bottom right of the page itself. Ask about something on the site.
3. A question the site cannot answer returns the fallback message, and the model is not called.

To embed it elsewhere, paste the snippet from the page into any HTML; if you set **Allowed
websites**, that site must be listed.

## The flow

`/signup` creates the account and logs you in, `/onboarding` creates your organization (you become
its OWNER), `/dashboard` shows your business, `/knowledge` is the knowledge base, and
`/website-chat` configures and previews the chat widget, and `/integrations/shopify` connects and
syncs a Shopify store. The browser never sees the access token: it
lives in an HttpOnly cookie set by the Next.js server (see
[Session decision](docs/architecture.md#session-decision-frontend)).

## API

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | public | Create account |
| POST | `/api/auth/login` | public | Returns a Bearer access token |
| GET | `/api/auth/me` | auth | Current user |
| POST | `/api/organizations` | auth | Create organization (caller becomes OWNER) |
| GET | `/api/organizations` | auth | My organizations |
| GET | `/api/organizations/current` | auth | Current organization |
| GET | `/api/organizations/{id}` | member | One organization |
| GET | `/api/organizations/{id}/members` | OWNER/ADMIN | List members |
| POST | `/api/organizations/{id}/members` | OWNER/ADMIN | Add a registered user |
| POST | `/api/knowledge/sources` | OWNER/ADMIN | Add a website; queues a crawl |
| GET | `/api/knowledge/sources` | member | List sources (status, pages, last crawl) |
| GET | `/api/knowledge/sources/{id}` | member | One source |
| POST | `/api/knowledge/sources/{id}/crawl` | OWNER/ADMIN | Re-crawl |
| DELETE | `/api/knowledge/sources/{id}` | OWNER/ADMIN | Delete source, pages and chunks |
| GET | `/api/knowledge/sources/{id}/documents` | member | Discovered pages (paginated) |
| GET | `/api/knowledge/sources/{id}/documents/{doc_id}` | member | One page's cleaned text |
| GET | `/api/chat/widget` | member | Widget settings and public key |
| PATCH | `/api/chat/widget` | OWNER/ADMIN | Enable/disable, welcome message, allowed websites |
| GET | `/api/public/chat/{public_key}/config` | public | Business name and welcome message |
| POST | `/api/public/chat/{public_key}/conversations` | public | Start a conversation; returns a secret token |
| POST | `/api/public/chat/{public_key}/messages` | public + token | Send a message, get the reply |
| GET | `/api/public/chat/{public_key}/conversations/{id}` | public + token | Conversation history |
| GET | `/api/integrations/shopify` | member | Connection, sync progress and imported counts (never tokens) |
| POST | `/api/integrations/shopify/connect` | OWNER/ADMIN | Start OAuth for a `*.myshopify.com` store; returns the Shopify URL |
| GET | `/api/integrations/shopify/callback` | OWNER/ADMIN | Complete OAuth (the frontend forwards Shopify's signed query) |
| POST | `/api/integrations/shopify/sync` | OWNER/ADMIN | Queue a manual sync |
| POST | `/api/integrations/shopify/disconnect` | OWNER/ADMIN | Discard credentials, remove webhooks and imported data |
| POST | `/api/webhooks/shopify` | public + HMAC | Shopify webhooks (signature verified, deduplicated, processed by the worker) |

Authenticated calls send `Authorization: Bearer <access_token>`. The public chat endpoints need no
login; the conversation token goes in `X-Conversation-Token`. Tenancy and authorization rules:
[docs/architecture.md](docs/architecture.md#multi-tenancy); chat details:
[docs/ai-brain.md](docs/ai-brain.md).

```bash
# try it from a shell
curl -X POST localhost:8000/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"a-long-passphrase"}'
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"a-long-passphrase"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -X POST localhost:8000/api/organizations -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"My Brand"}'
curl -X POST localhost:8000/api/knowledge/sources -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"type":"WEBSITE","url":"https://example.com"}'
```

## Checks

```bash
cd backend
pytest                                   # fast unit tier (no services needed)
docker compose up -d db redis && pytest -m integration   # real Postgres/pgvector + Redis tier
ruff check . && ruff format --check . && mypy app tools && alembic check

cd widget && npm test && npm run build                   # widget tests; build before the frontend build/dev
cd frontend && npm test && npm run lint && npm run typecheck && npm run build

# Browser end-to-end: starts a fake Shopify, the API, the worker and the dashboard against a throwaway
# database, runs real-browser tests, and removes everything. Needs docker compose up -d db redis.
cd e2e && npm ci && npm test              # offline (auth, security headers/cookies, layout, Shopify)
cd e2e && npm run test:full               # also the knowledge-base and chat suites (crawl public sites)
```

Mutation testing (`backend/mutation/`, [README](backend/mutation/README.md)) is an engineering tool,
not part of CI. Live verification against a real Shopify store or AI vendor is a manual runbook:
[docs/shopify.md](docs/shopify.md#live-verification-runbook), [docs/ai-providers.md](docs/ai-providers.md).

## Migrations

```bash
cd backend
alembic upgrade head                                  # includes 0007 automation engine
alembic downgrade -1                                  # roll back one revision
alembic revision --autogenerate -m "describe change"  # create (review the output!)
alembic check                                         # fail if models and DB drift
```

Phase 5 automation APIs, event/execution model, worker recovery, website lead capture, current
verification status and limitations are documented in [docs/automation.md](docs/automation.md) and
[docs/phase-5-progress.md](docs/phase-5-progress.md). Phase 5 has not passed its PostgreSQL/Redis,
browser E2E, failure-injection or mutation gates; do not treat it as production-ready.

## Environment variables

Only the essentials are listed; the full annotated list is in [.env.example](.env.example) and the
crawl limits are explained in [docs/knowledge-base.md](docs/knowledge-base.md#scope-and-limits-all-configurable).

| Variable | Where | Description |
| --- | --- | --- |
| `DATABASE_URL` | backend | SQLAlchemy URL, `postgresql+psycopg://user:pass@host:5432/db` (required) |
| `JWT_SECRET_KEY` | backend | **Required**, 32+ characters; signs access tokens |
| `REDIS_URL` | backend, worker | Job queue (default `redis://localhost:6379/0`) |
| `EMBEDDING_PROVIDER` / `LLM_PROVIDER` | worker / API | A provider id (`openai`, `groq`, `custom_openai_compatible`, `fake`, ...); chosen independently (default `openai`; `.env.example` sets `fake`) |
| `EMBEDDING_API_KEY` / `LLM_API_KEY` | worker / API | Secrets; `OPENAI_API_KEY` remains a fallback for the `openai` provider only |
| `EMBEDDING_MODEL` / `LLM_MODEL` | worker / API | Model names are configuration; blank = the provider's default |
| `EMBEDDING_BASE_URL` / `LLM_BASE_URL` | worker / API | Blank = the provider's default endpoint; required for `custom_openai_compatible` |
| `EMBEDDING_DIMENSIONS` | worker | `1536`; must match the vector column |
| `SECRET_ENCRYPTION_KEY` | API, worker | **Secret.** AES-256-GCM key (32 random bytes, base64) for stored integration tokens |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | API, worker | Your Shopify app's credentials; the secret never leaves the server |
| `SHOPIFY_REDIRECT_URI` | API | Must equal a redirect URL registered for the app (the frontend's `/integrations/shopify/callback`) |
| `SHOPIFY_API_VERSION` / `SHOPIFY_SCOPES` | API, worker | Pinned Admin API version (`2026-10`) and the minimum read-only scopes |
| `SHOPIFY_WEBHOOK_BASE_URL` | API, worker | Public https URL of this API for webhook registration; blank on localhost |
| `RAG_TOP_K` / `RAG_SIMILARITY_THRESHOLD` | API | Chunks retrieved (5) and minimum similarity (0.25) |
| `CHAT_HISTORY_LIMIT` | API | Previous messages shown to the model (default 10) |
| `PUBLIC_CHAT_RATE_LIMIT` | API | Messages per client IP and key per window (default 20 per 60 s) |
| `RATE_LIMIT_BACKEND` | API | `memory` (per process) or `redis` (shared between workers) |
| `CRAWL_MAX_PAGES`, `CRAWL_MAX_DEPTH`, ... | worker | Crawl limits (defaults 50 pages, depth 3) |
| `CRAWL_ALLOWED_PORTS` | worker, API | Ports the crawler may contact (default `80,443`) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | backend | Access token lifetime (default `480`) |
| `AUTH_RATE_LIMIT_ATTEMPTS` / `_WINDOW_SECONDS` | backend | Login/register limit per client IP (default 10 per 60 s) |
| `CORS_ORIGINS` | backend | Comma-separated allowed browser origins (default `http://localhost:3000`) |
| `ENVIRONMENT` | backend, worker | `development`, `test`, `staging` or `production`. **`production` makes the API and the worker refuse to start with an unsafe configuration** (weak secrets, wildcard or http CORS, the fake AI provider, ...); see [docs/production.md](docs/production.md#4-the-production-startup-gate) |
| `FORWARDED_ALLOW_IPS` | backend (uvicorn) | Peers allowed to set `X-Forwarded-For`; **required in production**, set to the frontend's address, never `*` unless the API is unreachable except via that proxy |
| `API_DOCS_ENABLED`, `MAX_REQUEST_BODY_BYTES`, `DB_*_TIMEOUT_SECONDS`, `REDIS_QUEUE_KEY`, `SHOPIFY_WEBHOOKS_DISABLED`, `SHOPIFY_WEBHOOK_AUDIT_INTERVAL_SECONDS` | backend, worker | Production hardening knobs with safe defaults (docs are off in production); see [.env.example](.env.example) |
| `POSTGRES_USER/PASSWORD/DB` | docker-compose | Local DB container credentials |
| `API_URL` | frontend | Server-side URL of the backend (default `http://localhost:8000`) |
| `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_APP_URL` | frontend | **Public** URLs written into the embed snippet (defaults `http://localhost:8000` / `:3000`) |

Secrets live only in untracked `.env` files (a test fails if a credential-shaped string or a filled-in
template becomes committable); `.env.example` files hold placeholders. Deploying: copy
`.env.production.example` to `.env.production` and follow [docs/production.md](docs/production.md#3-deployment-topology).
