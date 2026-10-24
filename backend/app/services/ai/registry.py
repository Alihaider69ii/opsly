"""The AI provider registry: provider id -> configuration -> adapter -> capability.

    ProviderSpec (catalog row)  +  settings
            |
    resolve_*_config()      validates, applies defaults, returns a ProviderConfig
            |
    AIProviderRegistry.build_*()   picks the adapter registered for ``spec.adapter``
            |
    LLMProvider / EmbeddingProvider

This is the only code that turns a provider id into an implementation. The agent, RAG and business
code depend on the ``LLMProvider`` / ``EmbeddingProvider`` interfaces and never on a vendor.

Where configuration comes from is separate from how providers are built: ``resolve_*_config`` below
is the *environment* source. A future per-organization source would produce the same
``ProviderConfig`` from a database row (decrypting the stored API key with ``SecretManager``) and
call the same ``build_*`` methods; see docs/ai-providers.md.
"""

from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from pydantic import SecretStr

from app.core.config import Settings
from app.db.models.knowledge import EMBEDDING_DIM
from app.services.ai.catalog import BUILTIN_PROVIDERS, Capability, ProviderSpec
from app.services.ai.embeddings import EmbeddingProvider
from app.services.ai.errors import AIError, AIErrorCode, EmbeddingError, LLMError
from app.services.ai.provider import LLMProvider
from app.services.ai.provider_config import ProviderConfig
from app.services.ai.providers.anthropic_messages import AnthropicMessagesProvider
from app.services.ai.providers.fake import FakeEmbeddingProvider, FakeLLMProvider
from app.services.ai.providers.openai_compatible import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
)

LLMAdapterFactory = Callable[[ProviderConfig], LLMProvider]
EmbeddingAdapterFactory = Callable[[ProviderConfig], EmbeddingProvider]

ALLOWED_MAX_TOKENS_FIELDS = ("max_tokens", "max_completion_tokens")


class AIProviderRegistry:
    def __init__(self, specs: Iterable[ProviderSpec] = BUILTIN_PROVIDERS) -> None:
        self._specs: dict[str, ProviderSpec] = {spec.id: spec for spec in specs}
        self._llm_adapters: dict[str, LLMAdapterFactory] = {}
        self._embedding_adapters: dict[str, EmbeddingAdapterFactory] = {}

    # -- registration (data and adapters) ------------------------------------------------------

    def register_spec(self, spec: ProviderSpec) -> None:
        if spec.id in self._specs:
            raise ValueError(f"provider '{spec.id}' is already registered")
        self._specs[spec.id] = spec

    def register_llm_adapter(self, adapter: str, factory: LLMAdapterFactory) -> None:
        self._llm_adapters[adapter] = factory

    def register_embedding_adapter(self, adapter: str, factory: EmbeddingAdapterFactory) -> None:
        self._embedding_adapters[adapter] = factory

    # -- lookup ------------------------------------------------------------------------------

    def provider_ids(self, capability: Capability | None = None) -> list[str]:
        return sorted(
            spec.id
            for spec in self._specs.values()
            if capability is None or capability in spec.capabilities
        )

    def spec(self, provider_id: str, error_cls: type[AIError] = AIError) -> ProviderSpec:
        spec = self._specs.get(provider_id)
        if spec is None:
            known = ", ".join(self.provider_ids())
            raise error_cls(
                AIErrorCode.MISCONFIGURED, f"unknown AI provider '{provider_id}'; known: {known}"
            )
        return spec

    # -- construction ------------------------------------------------------------------------

    def build_llm(self, config: ProviderConfig) -> LLMProvider:
        spec = config.spec
        if Capability.LLM not in spec.capabilities:
            raise LLMError(
                AIErrorCode.MISCONFIGURED, f"provider '{spec.id}' does not support text generation"
            )
        factory = self._llm_adapters.get(spec.adapter)
        if factory is None:
            raise LLMError(
                AIErrorCode.MISCONFIGURED, f"no LLM adapter '{spec.adapter}' is registered"
            )
        return factory(config)

    def build_embedding(self, config: ProviderConfig) -> EmbeddingProvider:
        spec = config.spec
        if Capability.EMBEDDING not in spec.capabilities:
            raise EmbeddingError(
                AIErrorCode.MISCONFIGURED, f"provider '{spec.id}' does not support embeddings"
            )
        factory = self._embedding_adapters.get(spec.adapter)
        if factory is None:
            raise EmbeddingError(
                AIErrorCode.MISCONFIGURED, f"no embedding adapter '{spec.adapter}' is registered"
            )
        return factory(config)


def _builtin_registry() -> AIProviderRegistry:
    registry = AIProviderRegistry()
    registry.register_llm_adapter("openai_compatible", OpenAICompatibleChatProvider.from_config)
    registry.register_embedding_adapter(
        "openai_compatible", OpenAICompatibleEmbeddingProvider.from_config
    )
    registry.register_llm_adapter("anthropic_messages", AnthropicMessagesProvider.from_config)
    registry.register_llm_adapter("fake", FakeLLMProvider.from_config)
    registry.register_embedding_adapter("fake", FakeEmbeddingProvider.from_config)
    return registry


default_registry = _builtin_registry()


# --- the environment configuration source ---------------------------------------------------------


def _validated_base_url(
    spec: ProviderSpec, configured: str, env_name: str, error_cls: type[AIError]
) -> str | None:
    """The base URL to use: the configured one (validated), else the provider's default.

    Environment configuration is operator-controlled, so ``http://localhost`` (Ollama, vLLM) is
    allowed. A configuration source that accepts URLs from tenants must additionally apply the
    crawler's SSRF guard before any request is made.
    """
    url = configured.strip()
    if not url:
        if spec.requires_base_url:
            raise error_cls(
                AIErrorCode.UNCONFIGURED, f"{env_name} is required for provider '{spec.id}'"
            )
        return spec.default_base_url
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or any(ch.isspace() or ord(ch) < 32 for ch in url)
    ):
        raise error_cls(
            AIErrorCode.MISCONFIGURED,
            f"{env_name} must be an http(s) URL without credentials or whitespace",
        )
    return url


def _api_key(
    spec: ProviderSpec,
    configured: SecretStr | None,
    settings: Settings,
    env_name: str,
    error_cls: type[AIError],
) -> SecretStr | None:
    for candidate in (
        configured,
        getattr(settings, spec.legacy_api_key_setting, None)
        if spec.legacy_api_key_setting
        else None,
    ):
        if candidate is not None and candidate.get_secret_value().strip():
            return candidate
    if spec.requires_api_key:
        names = env_name + (
            f" (or {spec.legacy_api_key_setting.upper()})" if spec.legacy_api_key_setting else ""
        )
        raise error_cls(AIErrorCode.UNCONFIGURED, f"{names} is not set for provider '{spec.id}'")
    return None


def _check_environment(spec: ProviderSpec, settings: Settings, error_cls: type[AIError]) -> None:
    if not spec.production_allowed and settings.environment.lower() == "production":
        raise error_cls(
            AIErrorCode.MISCONFIGURED, f"provider '{spec.id}' is not allowed in production"
        )


def resolve_llm_config(
    settings: Settings, registry: AIProviderRegistry = default_registry
) -> ProviderConfig:
    spec = registry.spec(settings.llm_provider, LLMError)
    if Capability.LLM not in spec.capabilities:
        raise LLMError(
            AIErrorCode.MISCONFIGURED, f"provider '{spec.id}' does not support text generation"
        )
    _check_environment(spec, settings, LLMError)
    model = settings.llm_model.strip() or (spec.default_llm_model or "")
    if not model:
        raise LLMError(AIErrorCode.MISCONFIGURED, f"LLM_MODEL is required for provider '{spec.id}'")
    max_tokens_field = settings.llm_max_tokens_field.strip() or spec.llm_max_tokens_field
    if max_tokens_field not in ALLOWED_MAX_TOKENS_FIELDS:
        raise LLMError(
            AIErrorCode.MISCONFIGURED,
            f"LLM_MAX_TOKENS_FIELD must be one of {', '.join(ALLOWED_MAX_TOKENS_FIELDS)}",
        )
    return ProviderConfig(
        spec=spec,
        model=model,
        base_url=_validated_base_url(spec, settings.llm_base_url, "LLM_BASE_URL", LLMError),
        api_key=_api_key(spec, settings.llm_api_key, settings, "LLM_API_KEY", LLMError),
        timeout=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        max_tokens_field=max_tokens_field,
    )


def resolve_embedding_config(
    settings: Settings, registry: AIProviderRegistry = default_registry
) -> ProviderConfig:
    if settings.embedding_dimensions != EMBEDDING_DIM:
        raise EmbeddingError(
            AIErrorCode.MISCONFIGURED,
            f"EMBEDDING_DIMENSIONS={settings.embedding_dimensions} does not match the database "
            f"vector column ({EMBEDDING_DIM}); changing it needs a migration and a re-embed",
        )
    spec = registry.spec(settings.embedding_provider, EmbeddingError)
    if Capability.EMBEDDING not in spec.capabilities:
        raise EmbeddingError(
            AIErrorCode.MISCONFIGURED, f"provider '{spec.id}' does not support embeddings"
        )
    _check_environment(spec, settings, EmbeddingError)
    model = settings.embedding_model.strip() or (spec.default_embedding_model or "")
    if not model:
        raise EmbeddingError(
            AIErrorCode.MISCONFIGURED, f"EMBEDDING_MODEL is required for provider '{spec.id}'"
        )
    return ProviderConfig(
        spec=spec,
        model=model,
        base_url=_validated_base_url(
            spec, settings.embedding_base_url, "EMBEDDING_BASE_URL", EmbeddingError
        ),
        api_key=_api_key(
            spec, settings.embedding_api_key, settings, "EMBEDDING_API_KEY", EmbeddingError
        ),
        timeout=settings.embedding_timeout_seconds,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
    )
