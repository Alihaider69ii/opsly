# AI providers (Phase 4, hardened in Phase 4.1)

BizFlow does not depend on any one AI vendor. The chat agent and the knowledge base talk to two
small interfaces, `LLMProvider` and `EmbeddingProvider`; which vendor sits behind each is
configuration, and the two are chosen **independently** (answers from one vendor, vectors from
another).

This extends the Phase 3 abstraction. It did not add a competing one: `LLMProvider` and
`EmbeddingProvider` are the same names, now backed by a registry instead of hard-wired classes.

## The path from a name to a model call

```
LLM_PROVIDER=groq                          settings (today: environment)
        |
catalog.py   ProviderSpec "groq"           plain data: capabilities, default URL, quirks
        |
registry.py  resolve_llm_config()          validates, applies defaults  ->  ProviderConfig
        |
registry.py  AIProviderRegistry.build_llm  picks the adapter named by spec.adapter
        |
providers/openai_compatible.py             the adapter: AIRequest -> HTTP -> AIResponse
providers/anthropic_messages.py            (a different protocol, so a different adapter)
```

| Piece | File | Responsibility |
| --- | --- | --- |
| `ProviderSpec` | `services/ai/catalog.py` | One row per vendor, as data. Imports nothing from the app. |
| `ProviderConfig` | `services/ai/provider_config.py` | Fully resolved settings for one provider: model, base URL, `SecretStr` key, timeout. |
| `AIProviderRegistry` | `services/ai/registry.py` | The only code that turns a provider id into an implementation. |
| `resolve_*_config` | `services/ai/registry.py` | The **environment** configuration source (see "Organization-level selection"). |
| adapters | `services/ai/providers/` | `openai_compatible` (chat and embeddings), `anthropic_messages` (chat), `fake`. Both real adapters share one hardened HTTP client (`_http.py`). |
| `get_llm_provider`, `get_embedding_provider` | `services/ai/factory.py` | Cached entry points for request handling. |

No application code branches on a vendor name. `if provider == "openai"` appears nowhere; vendor
differences are fields of the catalog row:

| Difference | Where it is expressed |
| --- | --- |
| Default endpoint | `ProviderSpec.default_base_url` |
| Needs an endpoint URL (custom gateways) | `ProviderSpec.requires_base_url` |
| Needs an API key (local servers often do not) | `ProviderSpec.requires_api_key` |
| Output-limit field (`max_tokens` vs `max_completion_tokens`) | `ProviderSpec.llm_max_tokens_field`, or `LLM_MAX_TOKENS_FIELD` |
| Embedding models that accept a `dimensions` parameter | `ProviderSpec.embedding_dimension_models` |
| Supports chat, embeddings, or both | `ProviderSpec.capabilities` |
| Refused in production | `ProviderSpec.production_allowed` |

## Providers included, and what each can really do

**Not every vendor is served by the generic OpenAI-compatible adapter.** Anthropic's Messages API is
a different protocol (a top-level `system` field, `x-api-key` plus `anthropic-version` headers,
typed content blocks, a required `max_tokens`) and has its own adapter. A preset is listed for a
capability only when it can really deliver it: for embeddings that means a documented model that
produces the **1536-wide** vector the `knowledge_chunks.embedding` column stores. Several vendors
serve an OpenAI-style `/embeddings` endpoint whose models are 384, 768, 1024 or 3072 wide; those are
deliberately **not** listed (and the adapter rejects any response of the wrong width, so a mistake
fails closed instead of corrupting the index).

| Id | Adapter | Chat | Embeddings (1536) | Default models / notes |
| --- | --- | :-: | :-: | --- |
| `openai` | `openai_compatible` | yes | yes | `gpt-4o-mini`, `text-embedding-3-small` (honours `dimensions`). The only vendor preset that can embed. |
| `anthropic` | `anthropic_messages` | yes | no | No default model: set `LLM_MODEL`. Anthropic has no embeddings API. |
| `openrouter` | `openai_compatible` | yes | no | Aggregator; pick the model with `LLM_MODEL`. |
| `groq` | `openai_compatible` | yes | no | |
| `together` | `openai_compatible` | yes | no | Serves `/embeddings`, but no documented model is 1536 wide. |
| `deepseek` | `openai_compatible` | yes | no | |
| `mistral` | `openai_compatible` | yes | no | `mistral-embed` is 1024 wide and uses `output_dimension`, not `dimensions`. |
| `gemini` | `openai_compatible` | yes | no | Google's OpenAI-compatibility layer is documented as beta; embedding width is not documented. |
| `custom_openai_compatible` | `openai_compatible` | yes | yes | Any OpenAI-style server: set `*_BASE_URL`; no key required (Ollama, vLLM, LM Studio, a company gateway). **You** are responsible for its embedding model being 1536 wide. |
| `fake` | `fake` | yes | yes | Deterministic, offline, **not semantic**. Refused when `ENVIRONMENT=production`. |

So "answers from Anthropic, embeddings from OpenAI" is the natural pairing for Claude models, and
`LLM_PROVIDER` and `EMBEDDING_PROVIDER` are independent for exactly this reason. A vendor that is
chat-only here stays chat-only until a 1536-wide model exists for it or the column is widened by a
migration.

### Adapter behaviour (both real adapters)

* One shared HTTP client: bounded retries with capped backoff (honouring `Retry-After` up to 10 s),
  **no redirects**, environment proxy settings ignored, a **response-size cap (32 MiB of decoded
  bytes)** and a **wall-clock deadline of twice the timeout** for reading a body (so a vendor that
  trickles bytes cannot hold a worker), an oversized success is a `provider_error` and is not retried.
* HTTP 402 (payment required) is reported as `auth`; 429 as `rate_limited`; timeouts and 5xx as
  retryable; a malformed or empty reply as `provider_error`. Messages are fixed wording and never
  contain the prompt, the reply, a header or the key.
* Anthropic specifics: `system` messages are joined into the top-level field; a conversation whose
  last turn is not from the user is rejected locally (the API would refuse it); `temperature` is
  clamped to the 0-1 range Anthropic accepts; text blocks are concatenated; a `refusal` stop reason
  maps to `content_filtered`; `generate_stream` reads server-sent events.

### What is and is not verified

| Claim | Status |
| --- | --- |
| Request shape, authentication header, error mapping, parsing, timeouts, size caps, redirects, slow bodies | **VERIFIED**: mocked HTTP *and* real local HTTP servers over real sockets (`tests/ai/test_http_real_sockets.py`, 24 cases) |
| The vendors' API contracts (endpoints, headers, fields, embedding widths) | **VERIFIED** against each vendor's official documentation, October 2026 |
| Any preset against a **live vendor account** | **EXTERNALLY BLOCKED**: no API key exists in this environment. Not claimed. |

To turn the last row into **LIVE-TESTED**, set the provider variables (in the environment or the
git-ignored `backend/.env`) and run:

```bash
cd backend
python -m tools.ai_live_check              # real chat + invalid key + timeout + unknown model
python -m tools.ai_live_check --embedding  # also a real embedding (width must be 1536) and a grounded RAG answer
```

It goes through the same registry, adapters and chat agent as production, spends a few tokens, prints
model, latency, finish reason and usage but never the key, a full prompt or a full reply, and exits
non-zero if the real request fails or an expected failure is mis-classified (for example a bad key
that does not come back as `auth`). Record the outcome in `docs/production.md` section 2.

## The two vendor-neutral objects

The agent builds an `AIRequest` and reads an `AIResponse`. It never sees an SDK object, an HTTP
response or a vendor error.

```python
AIRequest(messages, max_tokens, model=None, temperature=None, metadata={})
AIResponse(text, provider, model, usage, latency_ms, finish_reason, request_id)
AIUsage(prompt_tokens, completion_tokens, total_tokens)     # each may be None
```

- `model=None` means the configured default; a per-request model is possible but unused today.
- `metadata` is for tracing (a conversation id); adapters never forward it to a vendor.
- Vendors that omit usage, `finish_reason` or a request id produce `None`, never an error.
- `AIUsage.of(prompt, completion)` derives the total when the vendor gives only the parts.

## Normalized errors

Every adapter turns its vendor's failure into one `AIError` with a stable `code`
(`LLMError` / `EmbeddingError` are subclasses). The application branches on the code only.

| Code | Meaning | Retryable |
| --- | --- | :-: |
| `unconfigured` | A required setting (API key, base URL) is missing. Nothing was sent. | no |
| `misconfigured` | A setting is unusable, or the base URL answers 404. | no |
| `auth` | The vendor rejected the credentials (401/403). | no |
| `rate_limited` | HTTP 429. | yes |
| `timeout` | Request or gateway timeout. | yes |
| `unavailable` | 5xx, or the vendor could not be reached. | yes |
| `invalid_request` | The vendor rejected the request itself (other 4xx). | no |
| `model_unavailable` | Unknown model, or no access to it. | no |
| `provider_error` | A reply the adapter cannot interpret. | no |
| `content_filtered` | The vendor filtered the response. | no |

Messages are fixed wording per code plus, at most, the HTTP status. They **never** contain the
prompt, the response body, request headers or the key. A vendor's error body is read only to
classify (for instance a 400 is `model_unavailable` only when the vendor's error *code* says so;
free text that merely mentions "model" does not count) and is then discarded. The public chat
endpoint turns any `AIError` into a generic 503 "temporarily unavailable"; the cause is in the
server log under the code.

## Configuration

```
LLM_PROVIDER=openai                  # a catalog id
LLM_MODEL=                           # blank = the provider's default
LLM_BASE_URL=                        # blank = the provider's default URL
LLM_API_KEY=                         # secret

EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_DIMENSIONS=1536            # must equal the vector column (see below)
```

- Settings refuse `fake` when `ENVIRONMENT=production` at startup. The rest is checked when the
  provider is built: an unknown id fails with the list of known ids, a capability the provider lacks
  (embeddings from `groq`) fails, and `custom_openai_compatible` requires `*_BASE_URL`.
- API keys are `SecretStr`: `repr()`, `str()` and logging never show them. The provider cache key
  contains a **hash** of the key.
- **Legacy `OPENAI_API_KEY`**: used only as the key of the `openai` provider when `LLM_API_KEY` /
  `EMBEDDING_API_KEY` are blank, so deployments that only ever set `OPENAI_API_KEY` keep working
  unchanged. No other provider reads it.
- Base URLs from the environment may be `http://localhost` (that is the point of a local model
  server). They must have no credentials or whitespace.

### Examples

Answers from Groq, embeddings from OpenAI (Groq has no embeddings endpoint, so the two differ):

```
LLM_PROVIDER=groq
LLM_MODEL=<a model your Groq account offers>
LLM_API_KEY=...

EMBEDDING_PROVIDER=openai
EMBEDDING_API_KEY=...
```

Answers from a model on your own machine (Ollama exposes an OpenAI-compatible API), embeddings
unchanged:

```
LLM_PROVIDER=custom_openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=<a model you have pulled>
```

Model names are configuration and vendors retire them; none is hard-coded except the two defaults
in the `openai` catalog row. Embedding vectors must be 1536 numbers wide to fit the current column
(`text-embedding-3-small` is, and honours a `dimensions` request for exactly this reason); a model
with another width needs a migration first.

## Changing the embedding model

This is the one provider change with a data consequence, so it is designed to be safe.

Every chunk stores `embedding_model` = `<namespace>:<model>`. The namespace is the provider id, plus a
short hash of the base URL when it is not the provider's default, so two endpoints that both call a
model `m` are never confused. For the default OpenAI setup the namespace is just `openai`, which
keeps vectors written before this phase valid (`openai:text-embedding-3-small`).

- **Search only compares vectors with the current model's tag.** Vectors from different models are
  never ranked against each other; cosine similarity across models is meaningless.
- **Nothing is deleted automatically.** After you change the setting, existing chunks stay in the
  table but are invisible to search. Until they are re-embedded, a question with no visible
  knowledge gets the normal fallback answer, and the server logs a `rag.stale_vectors` warning with
  the organization and the number of invisible chunks, so "the assistant suddenly knows nothing" has a
  visible cause.
- **Re-embedding happens on the next crawl** of each source (the Re-crawl button, or a new crawl).
  An unchanged page is normally skipped; a page whose stored vectors belong to another model is
  re-embedded even if its text did not change. Per page, the new vectors are written and **then**
  the old chunks are removed, in one transaction. If the new provider is down, the old vectors
  remain and keep serving (as long as the old model is still configured).
- `EMBEDDING_DIMENSIONS` must equal the column width (1536) or startup of the provider fails with a
  clear message. A model with a different width needs a migration (a new column or table) and is not
  covered by "change the setting".

Procedure: set the new `EMBEDDING_*` values, restart the API and worker, click Re-crawl on each
source, watch the `documents_reindexed` count in the crawl summary and confirm no `rag.stale_vectors`
warnings remain.

## Organization-level selection (designed, not built)

Today the provider comes from the environment: one choice for the whole deployment. The code is
arranged so a per-organization choice is an addition, not a rewrite:

1. `resolve_llm_config` / `resolve_embedding_config` are the *environment source*. A second source
   would read an `ai_provider_settings` row (organization id, capability, provider id, model, base
   URL, encrypted key) and produce the **same `ProviderConfig`**.
2. `AIProviderRegistry.build_llm(config)` and `build_embedding(config)` already take a
   `ProviderConfig`. Adapters never read settings.
3. The API key would be stored with `SecretManager` (AES-GCM, context bound to the row id), exactly
   like Shopify tokens. **No plaintext organization keys**: that is why this was not built before
   the secret store existed.
4. A tenant-supplied base URL must go through the crawler's SSRF guard (`services/crawler/ssrf.py`)
   before any request is made. The environment source does not need this because the operator is
   trusted; a tenant is not.
5. The embedding model must be a per-organization property, which the `embedding_model` tag already
   makes safe.

Not built, on purpose: any settings UI, any table, any plaintext key storage.

## Adding a provider

- **An OpenAI-compatible vendor**: one `ProviderSpec` row in `catalog.py` and a test line. Nothing
  else changes.
- **A vendor with its own protocol**: one adapter class that implements `LLMProvider.generate`
  (translate `AIRequest`, translate the reply to `AIResponse`, map every failure to an `AIError`
  code), registered with `registry.register_llm_adapter(...)`, plus a catalog row naming it. Anthropic
  is the worked example (`providers/anthropic_messages.py`, built on the shared `_http.Endpoint`).
  The agent, RAG and the API do not change.

## Testing

All of it runs offline (the live checks above are the only thing that needs a key):
`tests/ai/` (provider behaviour with mocked HTTP: auth header, body shape, usage, status-to-code
mapping, timeouts, malformed replies, key never in errors or logs; the Anthropic adapter; the shared
client's limits against real local sockets; the registry: every catalog row, capability checks,
defaults, validation, namespaces; the agent on the fake provider), `tests/tools/` (the live-check tool
itself), `tests/knowledge/test_embedding_model_change.py` (SQLite) and
`tests/integration/test_embedding_models.py` (real pgvector).

## Known limitations

- No streaming in the chat API yet (`generate_stream` exists on the providers).
- Retries are minimal: the HTTP adapter makes a bounded number of attempts (2 for chat, more for
  embeddings) with capped exponential backoff, honouring `Retry-After` up to 10 seconds, and only
  for the retryable codes. After that the failure surfaces and the visitor sees the generic
  "temporarily unavailable" reply. There is no circuit breaker and no fallback to a second vendor.
- No per-organization provider settings (above), no cost tracking, no token budgets.
- **No preset has been exercised against a live vendor account** (EXTERNALLY BLOCKED, see above).
- Embeddings are OpenAI (or a custom endpoint you vouch for) only, because of the fixed 1536-wide
  column; supporting other vendors' embeddings needs a migration to a wider or per-model column.
