"""Builds the configured providers from settings. No vendor names appear here: the registry resolves
the provider id to an implementation (see ``registry.py``).

``get_llm_provider`` and ``get_embedding_provider`` are the entry points for request handling; they
are resolved independently, so the LLM and the embeddings can come from different vendors.
"""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.ai.embeddings import EmbeddingProvider
from app.services.ai.provider import LLMProvider
from app.services.ai.registry import (
    default_registry,
    resolve_embedding_config,
    resolve_llm_config,
)


def build_llm_provider(settings: Settings) -> LLMProvider:
    return default_registry.build_llm(resolve_llm_config(settings, default_registry))


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return default_registry.build_embedding(resolve_embedding_config(settings, default_registry))


# Successful providers are cached for the life of the process (they own an HTTP client). The cache
# key is derived from the *resolved* configuration, so changing any relevant setting builds a new
# provider. lru_cache never caches a raised exception, so fixing a configuration problem takes
# effect on the next request without a restart.
@lru_cache
def _cached_llm(key: tuple[object, ...]) -> LLMProvider:
    return build_llm_provider(get_settings())


@lru_cache
def _cached_embedder(key: tuple[object, ...]) -> EmbeddingProvider:
    return build_embedding_provider(get_settings())


def get_llm_provider() -> LLMProvider:
    return _cached_llm(resolve_llm_config(get_settings()).cache_key())


def get_embedding_provider() -> EmbeddingProvider:
    return _cached_embedder(resolve_embedding_config(get_settings()).cache_key())
