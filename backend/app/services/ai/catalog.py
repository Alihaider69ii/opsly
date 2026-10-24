"""The provider catalog: which AI providers BizFlow knows, as plain data.

This module deliberately imports nothing from the application, so configuration code can read it
without circular imports. Application code never branches on a provider id; it asks the registry
(``registry.py``) for an implementation and the registry reads these rows.

Adding a vendor that speaks the OpenAI-compatible HTTP protocol is one new ``ProviderSpec`` row.
Adding a vendor with its own protocol is one adapter class plus one row (see docs/ai-providers.md).

Capabilities are declared conservatively, from each vendor's own documentation (verified October
2026, see docs/ai-providers.md for the sources):

* a provider is listed for **chat** when it serves the protocol its ``adapter`` speaks;
* a provider is listed for **embeddings** only when a documented embedding model can produce the
  vector width BizFlow stores (1536, the ``knowledge_chunks.embedding`` column). Several vendors
  serve an OpenAI-style ``/embeddings`` endpoint whose models are 384, 768, 1024 or 3072 wide; those
  are deliberately NOT listed, because they could never be stored. The adapter also refuses any
  response of the wrong width, so a mistake fails closed instead of corrupting the index.

A declared capability is not a guarantee that a particular key or model works. No preset has been
exercised against a live account in this repository (tests use mocked HTTP and real local sockets).
"""

import enum
from dataclasses import dataclass


class Capability(enum.StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    adapter: str  # which registered adapter implements this provider
    capabilities: frozenset[Capability]
    default_base_url: str | None = None
    requires_base_url: bool = False
    requires_api_key: bool = True
    default_llm_model: str | None = None
    default_embedding_model: str | None = None
    # Settings attribute consulted when the generic LLM_API_KEY / EMBEDDING_API_KEY is unset. It
    # keeps existing deployments that only set OPENAI_API_KEY working for the "openai" provider.
    legacy_api_key_setting: str | None = None
    # OpenAI's newer models reject ``max_tokens``; most other compatible vendors only know it.
    llm_max_tokens_field: str = "max_tokens"
    # Embedding model-name prefixes that accept a
    # ``dimensions`` parameter (OpenAI's text-embedding-3 family).
    embedding_dimension_models: tuple[str, ...] = ()
    production_allowed: bool = True


_BOTH = frozenset({Capability.LLM, Capability.EMBEDDING})
_LLM_ONLY = frozenset({Capability.LLM})

BUILTIN_PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="openai",
        label="OpenAI",
        adapter="openai_compatible",
        capabilities=_BOTH,
        default_base_url="https://api.openai.com/v1",
        default_llm_model="gpt-4o-mini",
        default_embedding_model="text-embedding-3-small",
        legacy_api_key_setting="openai_api_key",
        llm_max_tokens_field="max_completion_tokens",
        embedding_dimension_models=("text-embedding-3",),
    ),
    ProviderSpec(
        # A different protocol (not OpenAI-compatible), hence its own adapter. Chat only: Anthropic
        # has no embeddings API. No default model: model names are configuration (LLM_MODEL).
        id="anthropic",
        label="Anthropic (Messages API)",
        adapter="anthropic_messages",
        capabilities=_LLM_ONLY,
        default_base_url="https://api.anthropic.com",
    ),
    ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        adapter="openai_compatible",
        capabilities=_LLM_ONLY,
        default_base_url="https://openrouter.ai/api/v1",
    ),
    ProviderSpec(
        id="groq",
        label="Groq",
        adapter="openai_compatible",
        capabilities=_LLM_ONLY,
        default_base_url="https://api.groq.com/openai/v1",
    ),
    ProviderSpec(
        # Serves /v1/embeddings, but none of its documented embedding models is 1536 wide.
        id="together",
        label="Together AI",
        adapter="openai_compatible",
        capabilities=_LLM_ONLY,
        default_base_url="https://api.together.ai/v1",
    ),
    ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        adapter="openai_compatible",
        capabilities=_LLM_ONLY,
        default_base_url="https://api.deepseek.com",
    ),
    ProviderSpec(
        # mistral-embed is 1024 wide and takes ``output_dimension`` (not OpenAI's ``dimensions``).
        id="mistral",
        label="Mistral",
        adapter="openai_compatible",
        capabilities=_LLM_ONLY,
        default_base_url="https://api.mistral.ai/v1",
    ),
    ProviderSpec(
        # Google's OpenAI-compatibility layer is documented as beta; its embedding models' width and
        # ``dimensions`` support are not documented, so embeddings are not claimed.
        id="gemini",
        label="Google Gemini (OpenAI-compatible endpoint)",
        adapter="openai_compatible",
        capabilities=_LLM_ONLY,
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    ProviderSpec(
        id="custom_openai_compatible",
        label="Custom OpenAI-compatible endpoint",
        adapter="openai_compatible",
        capabilities=_BOTH,
        requires_base_url=True,
        requires_api_key=False,  # self-hosted gateways (Ollama, vLLM, ...) often need none
    ),
    ProviderSpec(
        id="fake",
        label="Fake (offline development and tests)",
        adapter="fake",
        capabilities=_BOTH,
        requires_api_key=False,
        default_llm_model="fake-llm-v1",
        default_embedding_model="hash-v1",
        production_allowed=False,
    ),
)


def non_production_provider_ids() -> frozenset[str]:
    """Providers that must never run with ENVIRONMENT=production."""
    return frozenset(spec.id for spec in BUILTIN_PROVIDERS if not spec.production_allowed)
