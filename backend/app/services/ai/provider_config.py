"""``ProviderConfig``: everything an adapter needs to be built, already resolved and validated.

A config *source* (today: environment settings; later possibly a per-organization database row whose
API key is decrypted with ``SecretManager``) produces a ``ProviderConfig``; the registry turns it
into an adapter. Adapters never read settings, and settings never name adapters, so changing where
configuration comes from does not touch either side.
"""

import hashlib
from dataclasses import dataclass

from pydantic import SecretStr

from app.services.ai.catalog import ProviderSpec


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


@dataclass(frozen=True)
class ProviderConfig:
    spec: ProviderSpec
    model: str
    base_url: str | None
    api_key: SecretStr | None  # a SecretStr, so repr()/logging never shows it
    timeout: float = 30.0
    temperature: float | None = None  # LLM only
    max_tokens_field: str = "max_tokens"  # LLM only
    dimensions: int = 1536  # embeddings only
    batch_size: int = 64  # embeddings only

    @property
    def provider_id(self) -> str:
        return self.spec.id

    @property
    def secret(self) -> str | None:
        """The API key as plain text, for the adapter's HTTP client only."""
        value = self.api_key.get_secret_value().strip() if self.api_key else ""
        return value or None

    @property
    def namespace(self) -> str:
        """Identifies the endpoint family that produced a model's output.

        It is the provider id, plus a short hash of the base URL when that URL is not the provider's
        default (a gateway, a self-hosted server, a custom endpoint). Stored vectors are tagged with
        ``namespace:model`` so identically named models behind different endpoints are never mixed.
        For the default OpenAI configuration this is just ``openai``, so vectors written before the
        registry existed keep their ``openai:<model>`` tag and stay valid.
        """
        defaults = (self.spec.default_embedding_base_url, self.spec.default_base_url)
        if self.base_url is None or any(
            default is not None and _normalize_url(self.base_url) == _normalize_url(default)
            for default in defaults
        ):
            return self.spec.id
        digest = hashlib.sha256(_normalize_url(self.base_url).encode()).hexdigest()[:8]
        return f"{self.spec.id}@{digest}"

    def cache_key(self) -> tuple[object, ...]:
        """A hashable identity for provider caching. The key is hashed, never stored in clear."""
        secret = hashlib.sha256((self.secret or "").encode()).hexdigest()
        return (
            self.spec.id,
            self.model,
            self.base_url,
            secret,
            self.timeout,
            self.temperature,
            self.max_tokens_field,
            self.dimensions,
            self.batch_size,
        )
