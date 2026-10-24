# Progress

## Phase 0: Foundation (complete)

- [x] Monorepo structure (`backend/ frontend/ widget/ database/ docs/`)
- [x] Next.js frontend (TypeScript, Tailwind, App Router)
- [x] FastAPI backend with `GET /health` and `GET /health/db`
- [x] Postgres via env (`DATABASE_URL`), `docker-compose.yml` with pgvector image
- [x] SQLAlchemy 2.0 and Alembic configured; models `users`, `organizations`,
      `organization_members`; migration `0001`
- [x] `.gitignore`, `.env.example`, `README.md`, `docs/architecture.md`
- [x] Backend tests, ruff and mypy clean

## Phase 1: Authentication and organization tenancy (complete)

- [x] Argon2id passwords, JWT access tokens (identity-only claims), register/login/me
- [x] Per-IP rate limiting on login and register; no account enumeration on login
- [x] Organizations: creator becomes OWNER in one transaction; slug generation and validation
- [x] `current_user`, `current_org`, `org_from_path`, `require_org_role`, `scoped_select`
- [x] Members endpoints (OWNER/ADMIN); migration `0002`
- [x] Frontend `/login`, `/signup`, `/onboarding`, `/dashboard`; HttpOnly cookie session via Server
      Actions; Data Access Layer; optimistic `proxy.ts` guard
- [x] 115 backend tests; real-browser end-to-end run

## Phase 2: Knowledge base and secure website crawler (complete)

Data
- [x] `knowledge_sources`, `documents`, `knowledge_chunks` with `organization_id`, composite tenant
      foreign keys, cascades; migration `0003` enables pgvector explicitly
- [x] `vector(1536)` embeddings with `embedding_model`; no ANN index (measured, documented)

Crawler
- [x] URL validation and conservative canonicalization (`urls.py`)
- [x] SSRF protection: resolve-validate-pin per hop, manual redirect validation, port allow-list,
      IPv4-in-IPv6 unwrapping, proxy env ignored (`ssrf.py`, `fetcher.py`)
- [x] Limits: pages, depth, response size (also decompressed), per-fetch deadline, total time,
      redirects, content types
- [x] robots.txt (RFC 9309 via Protego; 4xx allow, 5xx disallow, Crawl-delay)
- [x] HTML extraction (lxml), same-site breadth-first crawl with priorities, content-hash dedupe
- [x] Incremental re-crawl (unchanged pages not re-embedded; removed pages deleted; flaky pages kept)

Pipeline
- [x] Deterministic chunking with heading carry and overlap
- [x] `EmbeddingProvider` abstraction: OpenAI-compatible provider + deterministic fake
- [x] `search_knowledge`: organization-scoped exact cosine search (`services/rag/search.py`)
- [x] Redis queue + worker (`python -m app.workers.main`): atomic claim, heartbeat, stale-crawl
      reaper, orphaned-job re-queue, graceful shutdown

API and UI
- [x] `POST/GET /api/knowledge/sources`, `GET/DELETE .../{id}`, `POST .../{id}/crawl`,
      `GET .../{id}/documents[/{document_id}]`; OWNER/ADMIN for writes
- [x] Frontend `/knowledge`, `/knowledge/[id]`, `/knowledge/[id]/documents/[documentId]`, live
      status auto-refresh, re-crawl, two-step delete

Verification
- [x] 611 fast unit tests + 35 Postgres/Redis integration tests; ruff, mypy, `alembic check` clean
- [x] 11 deliberate security regressions (mutation checks) all caught by the tests
- [x] Real-browser end-to-end with the real worker: crawled example.com and a multi-page site, a
      real-DNS loopback hostname refused, re-crawl, delete, cross-tenant isolation

## Phase 3: AI brain, RAG retrieval and website chat agent (complete)

Provider abstraction and agent
- [x] `LLMProvider` protocol; OpenAI-compatible provider (httpx) and a dev-only fake; model name from
      `LLM_MODEL`; `OPENAI_API_KEY` as a secret; fake refused in production
- [x] RAG on Phase 2 `search_knowledge` and `EmbeddingProvider` (no duplicates): `RAG_TOP_K`,
      `RAG_SIMILARITY_THRESHOLD`, organization-scoped, bounded and attributed context
- [x] Structured prompts (system rules, history, knowledge as untrusted data, user message);
      no relevant knowledge means a fixed fallback and no model call; canary leak detection

Conversations and API
- [x] `chat_widgets`, `conversations`, `messages` (migration `0004`, composite tenant foreign keys)
- [x] Public API keyed by a public key (`/api/public/chat/{public_key}/...`); the organization is
      never accepted from the client; per-conversation hashed secret token; one reply at a time
- [x] Dashboard `GET/PATCH /api/chat/widget`; `CHAT_HISTORY_LIMIT` history
- [x] Rate limiting through the one shared limiter (now Redis-capable), body-size cap, credential-less
      CORS, message validation, safe errors, structured logging that rejects sensitive field names

Widget and UI
- [x] `widget/`: Shadow DOM script (about 10 KB), `textContent` rendering, history, loading and error
      states, responsive, accessible; 39 jsdom tests
- [x] Frontend `/website-chat`: embed snippet, settings, live preview

Verification
- [x] 796 fast unit tests + 51 Postgres/Redis integration tests; 39 widget tests; ruff, mypy,
      `alembic check`, frontend lint/typecheck/build clean
- [x] 18 deliberate regressions (mutation checks) on the Phase 3 controls, all caught; this exposed
      one missing test (SYSTEM messages hidden from visitors), now added
- [x] Real-browser end-to-end (Edge, real API, worker and frontend, a separate-origin third-party
      page for real CORS): grounded answers, fallback without a model call, history after reload,
      markup shown as text, network/429/503 error states with retry, enable/disable and origin
      allow-list, mobile layout, cross-tenant isolation, real rate limiting. It also found that the
      API emitted no INFO logs under uvicorn (fixed: `configure_logging`) and that a restrictive
      allow-list broke the owner's own dashboard preview (fixed: dashboard origins always allowed)
- Not verified: the OpenAI provider against a live key (mocked HTTP only).

## Phase 4: Shopify integration and vendor-neutral AI providers (complete)

AI providers (see [ai-providers.md](ai-providers.md))
- [x] The Phase 3 `LLMProvider` / `EmbeddingProvider` extended, not replaced: a data catalog of
      providers (`openai`, `openrouter`, `groq`, `together`, `deepseek`, `mistral`, `gemini`,
      `custom_openai_compatible`, `fake`), a registry that turns an id into an adapter, and
      `ProviderConfig` as the seam between where configuration comes from and how providers are built
- [x] Vendor-neutral `AIRequest` / `AIResponse` / `AIUsage` and normalized `AIError` codes; the agent
      sees nothing else. Vendor quirks are catalog fields: no `if provider == "openai"` anywhere
      (a test enforces it)
- [x] LLM and embeddings chosen independently; `LLM_*` / `EMBEDDING_*` settings, `OPENAI_API_KEY`
      kept as a fallback for the `openai` provider only
- [x] Changing the embedding model is safe: vectors are tagged `namespace:model`, search never mixes
      models, old vectors are kept but invisible, a re-crawl re-embeds (new vectors first, then old
      removed), and search logs `rag.stale_vectors` while any are stale
- [x] Organization-level provider selection designed and documented, not built (no UI, no plaintext keys)

Secrets
- [x] Reusable `SecretManager` (`core/secrets.py`): AES-256-GCM, envelope with key id, context binding
      to the row, key rotation, fail-closed errors

Shopify (see [shopify.md](shopify.md))
- [x] OAuth authorization code grant with expiring offline tokens (Admin API 2026-10, GraphQL only):
      strict `*.myshopify.com` validation, callback HMAC, hashed single-use 10-minute `state` bound to
      organization, user and shop, rotation-safe token refresh under a row lock
- [x] Minimum read-only scopes centralized in `scopes.py` (`read_products` required;
      `read_customers`, `read_orders` optional)
- [x] Models and migration `0005`: `shopify_connections`, `shopify_sync_states`,
      `shopify_oauth_states`, `shopify_webhook_events`, `products`, `product_variants`, `customers`,
      `orders`, `order_items`; composite tenant foreign keys, tenant-unique Shopify ids, soft deletes
- [x] Sync on the existing Redis/Postgres worker: queued, idempotent upserts, resumable per page,
      time-sliced, atomic stage claims, crash recovery, throttle- and `Retry-After`-aware client,
      per-resource status, reconciliation after complete passes
- [x] Webhooks: raw-body HMAC first, delivery-id deduplication by a database unique constraint,
      identifiers-only ledger, 200 within the time budget, worker re-reads the object (a notification,
      not data), privacy webhooks (`customers/redact`, `shop/redact`, `customers/data_request`)
- [x] Disconnect discards credentials, removes webhook subscriptions and purges imported data
      (decision flagged for review)
- [x] `GET /api/integrations/shopify`, `POST .../connect`, `GET .../callback`, `POST .../sync`,
      `POST .../disconnect`, public `POST /api/webhooks/shopify`
- [x] Dashboard `/integrations/shopify`: connect, status, progress, counts, Sync now, two-step
      Disconnect, reconnect; the OAuth callback is a Next.js route handler that adds the session
- [x] `services/commerce` read service for Phase 5; no Shopify call anywhere in the agent (a test
      enforces the import boundary)

Verification
- [x] 1,333 fast unit tests and 93 Postgres/Redis integration tests (real schema, composite foreign
      keys, concurrent upserts, webhook unique-constraint race, competing sync workers, token-refresh
      locking, simultaneous OAuth completion, embedding-model change on pgvector); 10 frontend and 39
      widget tests; ruff, mypy, `alembic check`, frontend lint, typecheck and production build clean
- [x] 66 deliberate regressions (mutation checks) on tenant filtering, OAuth state and binding,
      shop-domain validation, token protection, webhook verification and deduplication, role checks,
      sync claiming, embedding-model separation and access-log redaction: 60 caught. The first run
      exposed six missing tests (a user in two organizations finishing one's flow in the other, a
      teammate's pending authorization being discarded, the Kelvin-sign look-alike domain, an
      oversized chunked webhook body, a demoted admin completing the callback, and the atomic
      consumption of the OAuth state, which needed a forced race), all now added and re-checked.
      The 6 survivors are four checks that duplicate another guard (for example the explicit replay
      check next to the atomic update that also rejects a replay), one of them listed twice by a
      mislabeled mutant, and one deliberate no-op control
- [x] Real-browser end-to-end (Edge, real API, worker and frontend, a fake Shopify served over HTTP
      with real OAuth redirects): 87 checks including declined and forged authorizations, hijack
      and replay of a callback, live sync progress, signed/duplicate/forged webhooks applied by the
      worker, member versus owner, two tenants, disconnect purge, reconnect, revoked access, and a
      scan of everything the browser received and of every server log. It found a header that
      overflowed phones after adding the fourth nav link (fixed) and that uvicorn's access log
      recorded the OAuth `code`, `state` and `hmac` (fixed: redaction filter, tested and
      mutation-checked; the dev server's request log ignores the callback)
- Not verified: a real Shopify store or app (everything runs against a faithful fake), protected
  customer data approval, and any AI provider against a live key (mocked HTTP only).

## Phase 4.1: Production readiness and real-world verification (complete)

Goal: take Phase 4 from "code-complete and heavily tested" to deployable, and be explicit about what
could not be proven. Everything is in [production.md](production.md) (status labels, findings
register, results). No new product features.

Hardening
- [x] Production start-up gate: the API and the worker refuse to start with a weak or placeholder
      secret, wildcard/http/localhost CORS, a development database password, the fake AI provider, an
      unset proxy or Redis setting, an unusable encryption key, or no webhook decision
- [x] Liveness / readiness / worker health endpoints, a worker heartbeat and container health check,
      request ids on every response and log line, a global request-body cap, PostgreSQL statement,
      lock and idle-transaction timeouts, 503 with `Retry-After` for a database or Redis outage
- [x] Worker crash containment (one bad job, a Redis outage or a database outage no longer ends the
      process) and recovery by the maintenance loop, proven by hard-killing real worker processes
      during product, customer and order import, webhook processing and token refresh
- [x] Log redaction after formatting (tokens, bearer credentials, values echoed by PostgreSQL error
      text), `httpx` URL logging off, `SecretStr` for the JWT key, URL credentials masked in settings
- [x] Dashboard security headers; non-root, read-only container images; a production compose topology
      with required secrets; loopback-only development ports

Shopify and AI
- [x] Official-documentation alignment (October 2026): API version policy (centralised, fall-forward
      detection, ageing warning), refresh-token grace-period model, 403 vs shop-unavailable, webhook
      subscription self-healing with a catch-up sync, protected-customer-data obligations
- [x] Disconnect cannot be raced by an in-flight import (row lock), and the purge safety net has no
      age limit; `customers/data_request` export tool; encryption-key re-encryption tool
- [x] Honest provider catalog (embeddings only where a 1536-wide model exists), a dedicated Anthropic
      Messages adapter, a hardened shared HTTP client (size cap, wall-clock deadline), live-check tools
      for Shopify and AI vendors with runbooks

Verification (final numbers in [production.md](production.md#16-verification-results-final-run-23-october-2026))
- [x] 2,008 unit and 122 integration tests, 15 frontend and 47 widget tests, 235 browser checks from one
      reproducible command (`e2e/`), 144 mutants (138 killed, 6 documented survivors), dependency audits
- [x] A route-by-route authorization and tenant matrix, a schema audit for tenant isolation, a
      repository secret scan, a log audit of every Shopify flow, and migration `0006` (foreign-key
      indexes) with a full up/down/up test
- [x] Defects found and fixed along the way, among them: customer data surviving a disconnect race, a
      vulnerable `cryptography` range, a widget that could mount twice or after its page was left,
      and a wrong refresh-token model (see the findings register)
- Not verified: a real Shopify store, real webhooks, protected customer data approval, any AI vendor
  with a real key (EXTERNALLY BLOCKED; runbooks provided), and database encryption at rest (operator).

## Later backlog (in order)

WhatsApp, the automation engine, leads/customers, using the imported Shopify data in the assistant
(product questions, order status), real dashboard data.

AI follow-ups: streaming replies and an async LLM client, a tool-calling registry, conversation
closing and retention, live-key verification of the OpenAI chat provider, relevance tuning with real
data, an evaluation set for answer quality.

Auth follow-ups: email verification, password reset, invitations (replaces add-by-email), refresh
tokens / revocation, organization switcher UI, ownership transfer.

Knowledge follow-ups: JavaScript-rendered sites, sitemaps, PDF/FAQ/manual sources, per-source
limits, ANN indexing for very large tenants, live-key verification of the OpenAI provider.

Shopify follow-ups: incremental sync by `updated_at`, multiple stores per organization, order
history beyond 60 days (`read_all_orders`), per-location inventory, refunds and fulfilments,
verification against a real development store, public-app review.

AI provider follow-ups: per-organization provider settings (encrypted keys, SSRF-checked base URLs),
retries across vendors, cost and token budgets, live-key verification of each preset.
