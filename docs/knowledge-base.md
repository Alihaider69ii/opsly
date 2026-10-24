# Knowledge base and website crawler

A business adds its website. BizFlow crawls it safely in the background, extracts the useful text,
splits it into chunks, embeds them, and stores everything in PostgreSQL/pgvector, scoped to the
organization. The result is a knowledge base the AI agent (Phase 3) retrieves from.

```
Browser --> Next.js --> FastAPI --POST /api/knowledge/sources--> Postgres (source = PENDING)
                           |                                          ^
                           +-- enqueue job --> Redis list             | status, documents, chunks
                                                  |                   |
                                          Worker (python -m app.workers.main)
                                                  |  claim PENDING->CRAWLING (atomic UPDATE)
                                                  v
                          SiteCrawler -> SafeFetcher (SSRF-hardened) -> extract -> chunk -> embed
```

## Data model

All three tables are tenant-owned (`organization_id`, indexed). Migration `0003`.

| Table | Purpose | Notes |
| --- | --- | --- |
| `knowledge_sources` | One place knowledge comes from (today: a website) | `type` (WEBSITE, plus reserved PDF/FAQ/MANUAL/PRODUCT_CATALOG/OTHER), `status` (PENDING, CRAWLING, COMPLETED, FAILED), `url`, `last_crawled_at`, heartbeat, safe `error_code`/`error_message`, `crawl_stats` (JSON). Unique per (organization, type, url). |
| `documents` | One page/document from a source | `url`, `title`, cleaned `content` (never raw HTML), `content_hash` (SHA-256), `status` (PENDING, READY, FAILED). Unique per (source, url). |
| `knowledge_chunks` | A retrievable slice + its embedding | `chunk_index`, `content`, `character_count`, `embedding vector(1536)`, `embedding_model`. Unique per (document, chunk_index). |

- **Tenant integrity in the database itself.** Children reference their parent with a *composite*
  foreign key that includes `organization_id` (`documents (knowledge_source_id, organization_id)` ->
  `knowledge_sources (id, organization_id)`, and the same for chunks -> documents). A chunk cannot
  point at another organization's document even through a bug or a hand-written query; Postgres
  rejects it. Tested on both SQLite (FKs enabled) and Postgres.
- **Cascades.** Deleting a source removes its documents and chunks; deleting an organization removes
  everything.
- **Source types.** Stored as VARCHAR + CHECK like organization roles. The reserved types are in the
  enum, but the API only accepts types that have an implementation (`IMPLEMENTED_TYPES`), so adding
  PDF later is a code change, not a redesign.
- **Safe errors.** `error_message` is always a fixed catalogue string chosen by `error_code`
  (`services/knowledge/errors.py`). Exception text, stack traces and URLs from logs never reach the
  database or the API.

## pgvector

- The migration runs `CREATE EXTENSION IF NOT EXISTS vector`. The Docker image only *ships* the
  extension; it is not enabled until a migration does so. Verified from a fresh database
  (extension absent before, present after) in `tests/integration/test_migrations.py`.
  `IF NOT EXISTS` keeps it safe on managed Postgres where an administrator pre-created it; the
  database role otherwise needs permission to create extensions. Downgrade drops the tables but
  leaves the extension (it is database-wide).
- **Dimension 1536**, fixed in the column (`EMBEDDING_DIM`, frozen separately inside the migration).
  OpenAI's `text-embedding-3-*` models can all be asked for 1536 dimensions, so changing among them
  needs no migration. A model with a different size needs a migration plus a re-embed; the worker
  refuses to start a crawl if `EMBEDDING_DIMENSIONS` does not match the column.
- **`embedding_model`** is stored on every chunk (`openai:text-embedding-3-small`,
  `fake:hash-v1`). Search only compares vectors from the model it is given, so switching models
  never silently mixes incompatible vector spaces; old chunks simply stop matching until re-embedded.
- **No ANN index (HNSW/IVFFlat), deliberately.** Measured on pgvector 0.8.7 with 40,000 random
  1536-d vectors across 40 tenants (1,000 each):
  - exact search for one tenant, using only the `organization_id` index: **7-17 ms**;
  - with an HNSW index present, the planner still chose the organization index (HNSW would not even
    be used);
  - forcing the HNSW index with the tenant filter and `LIMIT 10` returned **2 rows**, not 10: an
    approximate graph scan applies the filter afterwards and under-delivers when the tenant is a
    small share of the table.

  Exact per-tenant search is correct and fast at this scale. Revisit when a single tenant holds
  roughly 100k+ chunks: use HNSW with `hnsw.iterative_scan` (pgvector >= 0.8) or partition by
  organization. `test_no_ann_index_on_embeddings_by_design` documents the decision in code.

## Crawl flow

1. `POST /api/knowledge/sources` validates the URL **without any network access** (scheme, port,
   credentials, internal IP literals, `localhost`, legacy numeric hosts), canonicalizes it, creates
   the source as `PENDING`, enqueues a job and returns immediately. If Redis is down the source is
   marked `FAILED` (`queue_unavailable`) instead of sitting `PENDING` forever.
2. The worker pops the job and **claims** it with `UPDATE ... SET status='CRAWLING' WHERE id=? AND
   status='PENDING'`. A duplicate or stale job matches no row and is skipped.
3. `SiteCrawler` walks the site (below). For each page: extract -> chunk -> embed (outside any DB
   transaction) -> store document + chunks. A document is `READY` only after its chunks exist, and a
   non-`READY` document never has chunks, so retrieval can never see half-indexed content.
4. On success the source becomes `COMPLETED` with `last_crawled_at` and `crawl_stats`. Any failure
   becomes `FAILED` with a catalogue message. Pages already indexed stay.
5. **Re-crawl** reuses the same machinery and is incremental: unchanged pages (same content hash)
   are not re-chunked or re-embedded; changed pages replace their chunks; pages gone from the site
   are deleted. A 404 removes a document, but a 5xx/timeout keeps the last good copy, and a crawl
   that fails never deletes anything.

### Scope and limits (all configurable)

| Setting | Default | Meaning |
| --- | --- | --- |
| `CRAWL_MAX_PAGES` | 50 | Requests per crawl (failed fetches count) |
| `CRAWL_MAX_DEPTH` | 3 | Link hops from the start page |
| `CRAWL_MAX_RESPONSE_BYTES` | 2,000,000 | Per response, after decompression |
| `CRAWL_MAX_DOCUMENT_CHARS` | 100,000 | Stored text per page |
| `CRAWL_CONNECT/READ_TIMEOUT_SECONDS` | 5 / 10 | Per socket operation |
| `CRAWL_REQUEST_TIMEOUT_SECONDS` | 20 | Wall-clock cap for one fetch (stops slow-loris servers) |
| `CRAWL_TOTAL_TIMEOUT_SECONDS` | 600 | Wall-clock cap for a whole crawl (stops, not fails) |
| `CRAWL_MAX_REDIRECTS` | 5 | Per fetch |
| `CRAWL_DELAY_SECONDS` | 0.5 | Politeness delay (or the site's `Crawl-delay`, capped at 10 s) |
| `CRAWL_ALLOWED_PORTS` | 80,443 | Only these ports are ever contacted |
| `KNOWLEDGE_MAX_SOURCES_PER_ORG` | 10 | Abuse limit |
| `CRAWL_MIN_INTERVAL_SECONDS` | 60 | Cooldown between crawls of one source |

- **Same site only:** the seed host and its `www.`/non-`www.` twin. Other hosts, subdomains and
  redirects to other domains are not followed (a seed that redirects elsewhere fails with a clear
  message).
- **Breadth-first with priorities:** FAQ/about/contact/shipping/returns/policy URLs first, then
  product-like paths, then the rest; URLs with query strings go last. A page cap therefore can't be
  consumed by a thousand product variants before the policies.
- **Skipped:** images, video, archives, executables, stylesheets, scripts, PDFs (a future source
  type) by extension, and anything whose `Content-Type` is not HTML.
- **Not supported yet:** pages rendered only by JavaScript (no headless browser; such a site fails
  with a "no readable text" message), sitemaps, login-protected pages, per-source limits.

## SSRF model

The URL is user-supplied and fetched from inside our network, so the destination has to be proven
public *at connection time*, not inferred from text.

1. **Syntax gate** (`urls.py`): only `http`/`https`; no `user:pass@`; no backslashes or control
   characters; ports restricted to `CRAWL_ALLOWED_PORTS`; `localhost`, `*.localhost`, `*.local`,
   `*.internal`, single-label names and legacy numeric hosts (`2130706433`, `0x7f.1`, `0177.0.0.1`)
   are refused. This only gives fast feedback.
2. **Resolve and validate** (`ssrf.py`): our code resolves the name (with a timeout) and requires
   **every** returned address to be public. One private record among many public ones blocks the
   host. Blocked: loopback, RFC 1918, link-local (incl. `169.254.169.254`), CGNAT (incl. Alibaba's
   `100.100.100.200`), `192.0.0.0/24` (Oracle's `192.0.0.192`), unique-local IPv6 (AWS's
   `fd00:ec2::254`), multicast, reserved, documentation ranges, and IPv4 smuggled inside IPv6
   (`::ffff:a.b.c.d`, NAT64, 6to4, Teredo). Backed by `ipaddress.is_global` plus an explicit list.
3. **Pin the connection** (`fetcher.py`): the request goes to the *validated IP*, with the original
   name as the `Host` header and as the TLS SNI / certificate-verification name. There is no second
   DNS lookup, so **DNS rebinding** cannot swap in an internal address between check and connect.
   Each redirect hop is resolved exactly once.
4. **Redirects are manual.** Every `Location` goes through steps 1-3 again, plus the same-site
   check. A public site redirecting to `http://169.254.169.254/`, `http://localhost/`, a hostname
   that resolves privately, a non-HTTP scheme or a non-allowed port is refused and nothing is sent.
5. **Environment is ignored:** `trust_env=False` (a proxy would resolve names itself and bypass the
   checks); no connection reuse across hosts; `Accept-Encoding` limited to gzip/deflate.
6. **Resource limits:** response size (also *after* decompression, with a bounded decoder that is
   tested against memory growth from a gzip bomb), a wall-clock deadline per fetch, per-read
   timeouts, a redirect cap, a content-type allow-list, page/depth/time caps per crawl, queue and
   link-per-page caps.
7. **The worker re-checks everything.** A source row that somehow contains an internal address is
   refused by the worker before any request.
8. **Error messages reveal nothing internal.** A blocked destination reports the same generic
   message regardless of which address or range was hit, so the crawler is not an oracle for
   probing the internal network.

Verified with a real-DNS case in the browser: `http://127.0.0.1.nip.io` (a public name that resolves
to loopback) is accepted by the API and refused by the worker (`blocked_url`).

**Limitations.** Protection is only as good as the addresses the OS resolver returns; run the
worker in a network segment that cannot reach internal services anyway (egress firewall). IPv6
literals and zone ids are handled, but exotic DNS64 setups are not tested. TLS validation relies on
the system trust store. The in-process rate limiter and caps are per worker.

## robots.txt

Fetched once per origin through the same hardened fetcher, parsed with Protego (RFC 9309: longest
match, `*` and `$` wildcards, our own user-agent group `BizFlowBot` beats `*`).

| robots.txt response | Behavior |
| --- | --- |
| 2xx | Parsed and obeyed. Only the first 500 KiB are read. |
| 4xx (404, 410, 401, 403...) | No restrictions (RFC 9309 section 2.3.1.3). |
| 5xx / network error / timeout | Treated as **disallow everything**, after one retry (section 2.3.1.4). |
| Redirect | Followed only within the site; an off-site redirect counts as unreachable. |
| Blocked / off-site (permanent) | Not retried. |

`Crawl-delay` is honoured up to a 10 s cap. If the start page itself is disallowed the crawl fails
with `robots_disallowed`; if robots.txt could not be fetched because the site is unreachable, the
real cause (DNS, blocked, timeout...) is reported rather than blaming robots.txt.

## Content extraction

`extract.py` parses HTML with lxml (no network, no huge trees, recover mode) and keeps only text.
Links are collected from the whole page first (navigation is how FAQ and shipping pages are found).
Removed: script/style/noscript/iframe/svg/form controls, `nav`, `aside`, dialogs, site-wide
`header`/`footer` (unless inside `main`/`article`), and cookie/consent/newsletter/modal/drawer
containers (by class/id, only when short). A `<main>` that is a tiny fraction of a large page is
ignored in favour of the body. Output keeps structure: `# headings`, `- list items`, `a | b` table
rows, blank lines between blocks; identical consecutive blocks collapse. Encodings are detected
(header, BOM, meta, UTF-8, falling back to cp1252). Pathological input (5,000-deep nesting, garbage
bytes) never raises.

Trade-offs, on purpose: text hidden with `display:none` is **kept**, because collapsed FAQ
accordions are exactly that; a footer holding contact details is dropped on pages that have a
`<main>`; very short banner-like blocks can occasionally be removed.

> **Prompt injection.** Crawled text is untrusted input that Phase 3 will place in an LLM prompt.
> A page can contain instructions aimed at the model. Treat it as data, delimit it clearly in the
> prompt, and never give the model tools whose effects depend on trusting that text.

## URL canonicalization

Conservative on purpose. Two URLs are merged only when they are the same resource by RFC 3986 or
differ by well-known tracking parameters.

| Rule | Example |
| --- | --- |
| Lower-case scheme and host; IDNA for non-ASCII hosts; drop trailing dot | `HTTPS://Bücher.DE./` -> `https://xn--bcher-kva.de/` |
| Drop default ports (80/443), keep others | `https://x.com:443/a` -> `https://x.com/a` |
| Drop the fragment | `/a#top` -> `/a` |
| Resolve `.` and `..` segments | `/a/./b/../c` -> `/a/c` |
| Normalize percent-encoding (upper-case hex, decode unreserved, encode unsafe) | `/%7Euser`, `/a b` -> `/~user`, `/a%20b` |
| Remove `utm_*`, `fbclid`, `gclid`, `dclid`, `gbraid`, `wbraid`, `msclkid`, `yclid`, `igshid`, `mc_cid`, `mc_eid`, `_hsenc`, `_hsmi`, `_ga`, `_gl` | `/p?utm_source=x&v=1` -> `/p?v=1` |
| **Not** done: reordering query parameters, removing trailing slashes, case-folding paths, honoring `rel=canonical` | `?a=1&b=2` and `?b=2&a=1` stay distinct |

Pages that stay distinct by URL but have identical text are caught by the content hash (one
document kept). Variants such as `?variant=1` / `?variant=2` are never merged.

## Chunking

`services/rag/chunking.py`, deterministic, tested with 25 randomized documents plus targeted cases.

- Blocks are packed up to `CHUNK_SIZE` (1200 characters, a **hard** cap including carried heading and
  overlap). A new heading starts a new chunk once the current one has at least a quarter of that;
  tiny sections merge into neighbours.
- When a chunk ends because it is full, the next one starts with its section heading and the last
  `CHUNK_OVERLAP` (150) characters of the previous chunk. At section boundaries there is no overlap.
- Oversized blocks split at lines, then sentences, then words, then characters.
- Exact duplicate chunks within a document are dropped; order is preserved; indexes are contiguous.
- For embedding, the page title is prefixed to each chunk ("Returns Policy\n\nYes, within 30 days.")
  so context-free answers stay retrievable. The stored chunk text is unprefixed.

## Embedding provider abstraction

`services/ai/embeddings.py`: `EmbeddingProvider` (`model_id`, `dimensions`, `embed(texts)`). Since
Phase 4 the implementation is chosen by the provider registry, not hard-wired: see
[ai-providers.md](ai-providers.md) for the catalog, the OpenAI-compatible adapter (any vendor or
local server that speaks the `/embeddings` API), the fake provider and the normalized errors.

- The OpenAI-compatible adapter batches, retries 429/5xx/timeouts with backoff and does not retry
  4xx. Errors carry a stable code only: never response bodies, request payloads or the API key.
- `fake`: deterministic hashed bag-of-words, **non-semantic**, for tests and local development.
  Refused when `ENVIRONMENT=production`.
- Configuration: `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_BASE_URL`,
  `EMBEDDING_API_KEY` (a `SecretStr`: never in `repr`, logs, API responses or the database),
  `EMBEDDING_DIMENSIONS` (must equal the column width). `OPENAI_API_KEY` remains a fallback for the
  `openai` provider only. Missing key -> crawls fail with a clear `embedding_unconfigured` message
  instead of crashing.
- **Changing the model is safe.** Every chunk is tagged with `embedding_model`
  (`<provider>[@<endpoint hash>]:<model>`); search compares only vectors with the current tag, so
  vectors from two models are never mixed. Old vectors are not deleted: they are invisible until the
  next crawl of the source re-embeds the page (even if its text is unchanged), replacing a page's
  chunks only after the new vectors exist. While vectors are stale, search logs a
  `rag.stale_vectors` warning. Procedure in [ai-providers.md](ai-providers.md#changing-the-embedding-model).

## Vector search

`services/rag/search.py::search_knowledge(db, organization_id=, query_embedding=, embedding_model=,
top_k=5, min_score=None, source_id=None)` returns `SearchHit` objects (chunk, document and source
ids, URL, title, content, chunk index, cosine similarity).

- Always starts from `scoped_select(KnowledgeChunk, organization_id)` and also constrains the joined
  document's organization, `READY` status and the embedding model. `top_k` is clamped to 1-50; the
  query dimension is validated.
- Exact cosine distance (`<=>`). A `source_id` from another organization cannot widen the search.
- Covered in `tests/integration/test_vector_search.py` on real pgvector, including a tenant whose
  perfect match is invisible to everyone else, and a small tenant next to a much larger one.

## Worker and queue

- Redis list `bizflow:queue:crawl` carries `{"type":"crawl","source_id":...}`. Redis only delivers;
  **Postgres is the source of truth.** No Celery, no acknowledgements, no visibility timeouts.
- `python -m app.workers.main`: single-threaded loop (run several processes for concurrency).
  Every 60 s it also (a) fails `CRAWLING` sources whose heartbeat stopped for
  `CRAWL_STALE_AFTER_SECONDS` (crashed or killed worker) and (b) re-queues `PENDING` sources whose
  job was lost. A crawl heart-beats after each page and notices deletion of its source.
- SIGINT/SIGTERM finish the current page, mark the source `interrupted` and exit.
- Failure modes: queue down at request time -> source `FAILED`/`queue_unavailable`; worker dies ->
  reaper fails the crawl; duplicate job -> second claim is a no-op; source deleted mid-crawl ->
  crawl stops, no orphans (foreign keys cascade).

## Organization isolation

- API: every route resolves the organization through `current_org` (verified membership). Source and
  document ids from other organizations return the same 404 as ids that do not exist.
  Reading requires membership; adding, re-crawling and deleting need OWNER or ADMIN.
- Queries start from `scoped_select` or filter on the verified `organization_id`.
- Worker: loads the source by id (trusted process) and stamps every document/chunk with that
  source's `organization_id`.
- Database: composite foreign keys (above) make cross-tenant references impossible.
- Search: organization-scoped by construction.
- Tests: unit tier (SQLite with FKs on) and integration tier (Postgres); a mutation check that
  removes each scoping filter fails the isolation tests.

## Tests

```bash
cd backend
pytest                       # fast unit tier (no services needed)
docker compose up -d db redis
pytest -m integration        # Postgres + pgvector + Redis tier
```

Integration tests use their own databases (`bizflow_test`, recreated from the real migrations each
session, and a scratch DB for the up/down/up migration cycle) and their own Redis keys; they fail
with instructions if the services are down. They cover the migration, the real vector column and
`<=>` queries, search isolation, DB-level tenant constraints, the Redis queue, and the whole
pipeline (API -> Redis -> worker -> a real local website over sockets -> Postgres -> vector search).

## Known limitations

- No JavaScript rendering, sitemaps, login-protected pages, or PDF/FAQ/manual sources yet.
- The OpenAI provider is tested against mocked HTTP only; it has not been exercised with a live API
  key in this repository's test runs.
- Exact vector search will need an ANN strategy for very large tenants (see above).
- One crawl at a time per worker process; per-source limits are global settings.
- The embedding model cannot be swapped without a re-embed (old chunks stop matching by design).
