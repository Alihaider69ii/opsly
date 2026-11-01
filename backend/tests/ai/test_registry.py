import json
import logging
import uuid

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.ai import factory
from app.services.ai.agent import ChatAgent
from app.services.ai.catalog import (
    BUILTIN_PROVIDERS,
    Capability,
    ProviderSpec,
    non_production_provider_ids,
)
from app.services.ai.errors import AIErrorCode, EmbeddingError, LLMError
from app.services.ai.provider import AIRequest, AIResponse, ChatMessage
from app.services.ai.provider_config import ProviderConfig
from app.services.ai.providers.anthropic_messages import AnthropicMessagesProvider
from app.services.ai.providers.fake import FakeEmbeddingProvider, FakeLLMProvider
from app.services.ai.providers.gemini_embedding import GeminiEmbeddingProvider
from app.services.ai.providers.openai_compatible import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
)
from app.services.ai.registry import (
    AIProviderRegistry,
    default_registry,
    resolve_embedding_config,
    resolve_llm_config,
)
from tests.chat.helpers import SpyEmbedder, StubSearch, make_hit

_ORG = uuid.uuid4()
KEY = "sk-test-SUPERSECRET-0123456789"
OTHER_KEY = "sk-other-ANOTHERSECRET-9876543210"


@pytest.fixture(autouse=True)
def _reuse_one_ssl_context(monkeypatch):
    """Building a real httpx client loads the CA bundle (about a second on Windows); these tests
    build dozens of providers, so share one context. Nothing here makes a network request."""
    import ssl

    import httpx._transports.default as transport

    context = ssl.create_default_context()
    monkeypatch.setattr(transport, "create_ssl_context", lambda *a, **k: context)


def settings(**overrides) -> Settings:
    base = {"database_url": "sqlite://", "jwt_secret_key": "k" * 40}
    return Settings(_env_file=None, **{**base, **overrides})


def base_url_of(provider) -> str:
    return str(provider._endpoint.client.base_url).rstrip("/")


def auth_header_of(provider) -> str | None:
    return provider._endpoint.client.headers.get("authorization")


# --- the catalog is consistent ---------------------------------------------------------------------


def test_catalog_ids_are_unique_and_every_adapter_is_registered():
    ids = [spec.id for spec in BUILTIN_PROVIDERS]
    assert len(ids) == len(set(ids))
    for spec in BUILTIN_PROVIDERS:
        if Capability.LLM in spec.capabilities:
            assert spec.adapter in default_registry._llm_adapters, spec.id
        if Capability.EMBEDDING in spec.capabilities:
            assert spec.adapter in default_registry._embedding_adapters, spec.id


def test_the_expected_providers_are_known_and_capabilities_are_not_assumed():
    assert set(default_registry.provider_ids()) >= {
        "openai",
        "openrouter",
        "groq",
        "together",
        "deepseek",
        "mistral",
        "gemini",
        "custom_openai_compatible",
        "fake",
    }
    assert "openrouter" in default_registry.provider_ids(Capability.LLM)
    assert "openrouter" not in default_registry.provider_ids(Capability.EMBEDDING)
    assert "deepseek" not in default_registry.provider_ids(Capability.EMBEDDING)
    assert "openai" in default_registry.provider_ids(Capability.EMBEDDING)
    assert "gemini" in default_registry.provider_ids(Capability.LLM)
    assert "gemini" in default_registry.provider_ids(Capability.EMBEDDING)


def test_only_the_fake_provider_is_barred_from_production():
    assert non_production_provider_ids() == {"fake"}


# --- every preset resolves and builds from data alone ---------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [s for s in BUILTIN_PROVIDERS if Capability.LLM in s.capabilities and s.adapter != "fake"],
    ids=lambda s: s.id,
)
def test_every_llm_preset_builds_through_the_registry(spec):
    overrides = {"llm_provider": spec.id, "llm_api_key": SecretStr(KEY), "llm_model": "some-model"}
    if spec.requires_base_url:
        overrides["llm_base_url"] = "https://gateway.example/v1"
    built = factory.build_llm_provider(settings(**overrides))
    expected_class = {
        "openai_compatible": OpenAICompatibleChatProvider,
        "anthropic_messages": AnthropicMessagesProvider,
        "gemini": OpenAICompatibleChatProvider,  # same protocol and class, just its own adapter key
    }[spec.adapter]
    assert isinstance(built, expected_class)
    assert built.name == spec.id and built.model == "some-model"
    expected = overrides.get("llm_base_url") or spec.default_base_url
    assert base_url_of(built) == expected.rstrip("/")
    if spec.adapter == "anthropic_messages":  # its own protocol: x-api-key, not a bearer token
        assert built._endpoint.client.headers.get("x-api-key") == KEY
        assert auth_header_of(built) is None
    else:
        assert auth_header_of(built) == f"Bearer {KEY}"


@pytest.mark.parametrize(
    "spec",
    [
        s
        for s in BUILTIN_PROVIDERS
        if Capability.EMBEDDING in s.capabilities and s.adapter != "fake"
    ],
    ids=lambda s: s.id,
)
def test_every_embedding_preset_builds_through_the_registry(spec):
    overrides = {
        "embedding_provider": spec.id,
        "embedding_api_key": SecretStr(KEY),
        "embedding_model": "some-embedder",
    }
    if spec.requires_base_url:
        overrides["embedding_base_url"] = "https://gateway.example/v1"
    built = factory.build_embedding_provider(settings(**overrides))
    expected_class = {
        "openai_compatible": OpenAICompatibleEmbeddingProvider,
        "gemini": GeminiEmbeddingProvider,
    }[spec.adapter]
    assert isinstance(built, expected_class)
    assert built.model_id.startswith(f"{spec.id}")
    assert built.model_id.endswith(":some-embedder")


def test_vendor_quirks_come_from_catalog_data():
    openai = resolve_llm_config(settings(llm_provider="openai", llm_api_key=SecretStr(KEY)))
    groq = resolve_llm_config(
        settings(llm_provider="groq", llm_api_key=SecretStr(KEY), llm_model="m")
    )
    assert openai.max_tokens_field == "max_completion_tokens"
    assert groq.max_tokens_field == "max_tokens"
    override = resolve_llm_config(
        settings(
            llm_provider="groq",
            llm_api_key=SecretStr(KEY),
            llm_model="m",
            llm_max_tokens_field="max_completion_tokens",
        )
    )
    assert override.max_tokens_field == "max_completion_tokens"
    with pytest.raises(LLMError) as exc:
        resolve_llm_config(
            settings(
                llm_provider="groq",
                llm_api_key=SecretStr(KEY),
                llm_model="m",
                llm_max_tokens_field="max-tokens; DROP TABLE",
            )
        )
    assert exc.value.code == AIErrorCode.MISCONFIGURED


# --- the generic OpenAI-compatible provider ------------------------------------------------------------------


def test_a_custom_endpoint_needs_a_base_url_but_not_necessarily_a_key():
    with pytest.raises(LLMError) as exc:
        factory.build_llm_provider(settings(llm_provider="custom_openai_compatible", llm_model="m"))
    assert exc.value.code == AIErrorCode.UNCONFIGURED and "LLM_BASE_URL" in str(exc.value)

    local = factory.build_llm_provider(
        settings(
            llm_provider="custom_openai_compatible",
            llm_base_url="http://localhost:11434/v1",
            llm_model="llama3",
        )
    )
    assert base_url_of(local) == "http://localhost:11434/v1"
    assert auth_header_of(local) is None  # a self-hosted model with no key sends no credentials


def test_a_custom_endpoint_still_needs_a_model_name():
    with pytest.raises(LLMError) as exc:
        factory.build_llm_provider(
            settings(llm_provider="custom_openai_compatible", llm_base_url="https://gw.example/v1")
        )
    assert exc.value.code == AIErrorCode.MISCONFIGURED and "LLM_MODEL" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gateway.example/v1",
        "gateway.example/v1",
        "https://user:pass@gateway.example/v1",
        "https://gateway.example/v1 \nX-Evil: 1",
        "https:///v1",
    ],
)
def test_malformed_base_urls_are_rejected(url):
    with pytest.raises(LLMError) as exc:
        resolve_llm_config(
            settings(llm_provider="custom_openai_compatible", llm_base_url=url, llm_model="m")
        )
    assert exc.value.code == AIErrorCode.MISCONFIGURED
    assert "pass" not in str(exc.value)


def test_a_base_url_override_applies_to_a_known_provider_too():
    p = factory.build_llm_provider(
        settings(
            llm_provider="openai",
            llm_api_key=SecretStr(KEY),
            llm_base_url="https://eu.gateway.example/openai/v1",
        )
    )
    assert base_url_of(p) == "https://eu.gateway.example/openai/v1"
    assert p.model == "gpt-4o-mini"  # model still comes from the catalog default


# --- unknown providers and unsupported capabilities -------------------------------------------------------


def test_an_unknown_provider_is_a_clear_configuration_error():
    with pytest.raises(LLMError) as exc:
        factory.build_llm_provider(settings(llm_provider="skynet"))
    assert exc.value.code == AIErrorCode.MISCONFIGURED
    assert "skynet" in str(exc.value) and "openai" in str(exc.value)  # lists what is known
    with pytest.raises(EmbeddingError) as exc2:
        factory.build_embedding_provider(settings(embedding_provider="skynet"))
    assert exc2.value.code == AIErrorCode.MISCONFIGURED


def test_provider_ids_are_normalized_and_must_be_plain_identifiers():
    assert settings(llm_provider="  OpenRouter ").llm_provider == "openrouter"
    for bad in ("open ai", "open-ai", "../etc", "", "x" * 60):
        with pytest.raises(ValueError):
            settings(llm_provider=bad)


def test_a_provider_is_only_used_for_capabilities_it_declares():
    with pytest.raises(EmbeddingError) as exc:
        factory.build_embedding_provider(
            settings(embedding_provider="openrouter", embedding_api_key=SecretStr(KEY))
        )
    assert exc.value.code == AIErrorCode.MISCONFIGURED and "embeddings" in str(exc.value)
    with pytest.raises(EmbeddingError):
        factory.build_embedding_provider(
            settings(embedding_provider="deepseek", embedding_api_key=SecretStr(KEY))
        )


# --- the LLM and the embeddings are independent --------------------------------------------------------


def test_llm_and_embeddings_can_come_from_different_vendors_with_different_keys():
    cfg = settings(
        llm_provider="openrouter",
        llm_api_key=SecretStr(KEY),
        llm_model="anthropic/some-model",
        embedding_provider="openai",
        embedding_api_key=SecretStr(OTHER_KEY),
    )
    llm = factory.build_llm_provider(cfg)
    embedder = factory.build_embedding_provider(cfg)
    assert llm.name == "openrouter" and base_url_of(llm) == "https://openrouter.ai/api/v1"
    assert embedder.model_id == "openai:text-embedding-3-small"
    assert auth_header_of(llm) == f"Bearer {KEY}"
    assert auth_header_of(embedder) == f"Bearer {OTHER_KEY}"  # each capability has its own key


def test_a_fake_llm_can_run_with_real_embeddings_and_vice_versa():
    a = settings(llm_provider="fake", embedding_provider="openai", embedding_api_key=SecretStr(KEY))
    assert isinstance(factory.build_llm_provider(a), FakeLLMProvider)
    assert isinstance(factory.build_embedding_provider(a), OpenAICompatibleEmbeddingProvider)
    b = settings(llm_provider="openai", llm_api_key=SecretStr(KEY), embedding_provider="fake")
    assert isinstance(factory.build_llm_provider(b), OpenAICompatibleChatProvider)
    assert isinstance(factory.build_embedding_provider(b), FakeEmbeddingProvider)


def test_a_misconfigured_embedding_provider_does_not_break_the_llm_and_vice_versa():
    cfg = settings(llm_provider="fake", embedding_provider="openai")  # no embedding key
    assert factory.build_llm_provider(cfg).name == "fake"
    with pytest.raises(EmbeddingError):
        factory.build_embedding_provider(cfg)


# --- API keys -------------------------------------------------------------------------------------------


def test_legacy_openai_api_key_still_works_for_the_openai_provider_only():
    legacy = settings(
        llm_provider="openai", embedding_provider="openai", openai_api_key=SecretStr(KEY)
    )
    assert auth_header_of(factory.build_llm_provider(legacy)) == f"Bearer {KEY}"
    assert auth_header_of(factory.build_embedding_provider(legacy)) == f"Bearer {KEY}"


def test_the_openai_key_is_never_sent_to_another_vendor():
    for provider in ("openrouter", "groq", "deepseek", "together", "mistral", "gemini"):
        with pytest.raises(LLMError) as exc:
            factory.build_llm_provider(
                settings(llm_provider=provider, llm_model="m", openai_api_key=SecretStr(KEY))
            )
        assert exc.value.code == AIErrorCode.UNCONFIGURED, provider
        assert KEY not in str(exc.value)
    with pytest.raises(EmbeddingError) as exc:  # no embedding model it offers fits the column
        factory.build_embedding_provider(
            settings(embedding_provider="together", openai_api_key=SecretStr(KEY))
        )
    assert exc.value.code == AIErrorCode.MISCONFIGURED and KEY not in str(exc.value)
    with pytest.raises(EmbeddingError) as exc3:  # gemini has no legacy key fallback either
        factory.build_embedding_provider(
            settings(embedding_provider="gemini", openai_api_key=SecretStr(KEY))
        )
    assert exc3.value.code == AIErrorCode.UNCONFIGURED and KEY not in str(exc3.value)


def test_the_specific_key_wins_over_the_legacy_one():
    cfg = settings(llm_api_key=SecretStr(OTHER_KEY), openai_api_key=SecretStr(KEY))
    assert auth_header_of(factory.build_llm_provider(cfg)) == f"Bearer {OTHER_KEY}"


def test_missing_or_blank_keys_are_controlled_errors_that_name_the_setting():
    for key in (None, SecretStr(""), SecretStr("   ")):
        with pytest.raises(LLMError) as exc:
            factory.build_llm_provider(settings(llm_provider="openai", llm_api_key=key))
        assert exc.value.code == AIErrorCode.UNCONFIGURED
        assert "LLM_API_KEY" in str(exc.value) and "OPENAI_API_KEY" in str(exc.value)
    with pytest.raises(EmbeddingError) as exc2:
        factory.build_embedding_provider(settings(embedding_provider="openai"))
    assert "EMBEDDING_API_KEY" in str(exc2.value)


def test_keys_never_appear_in_configs_settings_or_errors():
    cfg = settings(
        llm_api_key=SecretStr(KEY),
        embedding_api_key=SecretStr(OTHER_KEY),
        openai_api_key=SecretStr(KEY),
    )
    resolved = resolve_llm_config(cfg)
    for text in (repr(cfg), str(cfg), repr(resolved), str(resolved), repr(resolved.cache_key())):
        assert KEY not in text and OTHER_KEY not in text
    assert KEY not in json.dumps(cfg.model_dump(mode="json"), default=str)
    assert resolved.secret == KEY  # available to the adapter, and only through this property


# --- stored vectors are tagged with the model AND the endpoint -------------------------------------------


def test_model_ids_are_unchanged_for_default_openai_so_existing_vectors_stay_valid():
    openai = factory.build_embedding_provider(
        settings(embedding_provider="openai", openai_api_key=SecretStr(KEY))
    )
    assert openai.model_id == "openai:text-embedding-3-small"
    assert factory.build_embedding_provider(settings(embedding_provider="fake")).model_id == (
        "fake:hash-v1"
    )


def test_the_same_model_name_behind_different_endpoints_gets_a_different_model_id():
    def model_id(**kw):
        return factory.build_embedding_provider(
            settings(embedding_api_key=SecretStr(KEY), embedding_model="same-name", **kw)
        ).model_id

    default = model_id(embedding_provider="openai")
    gateway = model_id(embedding_provider="openai", embedding_base_url="https://gw.example/v1")
    gateway_slash = model_id(
        embedding_provider="openai", embedding_base_url="https://gw.example/v1/"
    )
    other = model_id(embedding_provider="openai", embedding_base_url="https://other.example/v1")
    custom = model_id(
        embedding_provider="custom_openai_compatible", embedding_base_url="https://gw.example/v1"
    )
    assert default == "openai:same-name"
    assert gateway.startswith("openai@") and gateway.endswith(":same-name")
    assert gateway == gateway_slash  # a trailing slash is not a different endpoint
    assert len({default, gateway, other, custom}) == 4  # nothing silently shares vectors
    assert "gw.example" not in gateway  # only a hash, never the URL


def test_the_default_url_written_out_explicitly_is_still_the_default_namespace():
    explicit = factory.build_embedding_provider(
        settings(
            embedding_provider="openai",
            embedding_api_key=SecretStr(KEY),
            embedding_base_url="https://api.openai.com/v1/",
        )
    )
    assert explicit.model_id == "openai:text-embedding-3-small"


def test_gemini_chat_and_embeddings_use_different_default_endpoints_but_the_default_namespace():
    llm = resolve_llm_config(settings(llm_provider="gemini", llm_api_key=SecretStr(KEY), llm_model="m"))
    embedding = resolve_embedding_config(
        settings(embedding_provider="gemini", embedding_api_key=SecretStr(KEY))
    )
    assert llm.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert embedding.base_url == "https://generativelanguage.googleapis.com/v1beta"
    # the embedding default endpoint is still "the default" for namespacing, not a custom one
    assert embedding.namespace == "gemini"
    built = factory.build_embedding_provider(
        settings(embedding_provider="gemini", embedding_api_key=SecretStr(KEY))
    )
    assert built.model_id == "gemini:gemini-embedding-2"  # the catalog default model


def test_embedding_dimensions_must_match_the_database_column():
    with pytest.raises(EmbeddingError) as exc:
        resolve_embedding_config(settings(embedding_provider="fake", embedding_dimensions=768))
    assert exc.value.code == AIErrorCode.MISCONFIGURED


# --- environment guards ------------------------------------------------------------------------------------


def test_the_fake_provider_is_refused_in_production_by_settings_and_by_the_registry():
    for field in ("llm_provider", "embedding_provider"):
        with pytest.raises(ValueError, match="not allowed"):
            settings(environment="production", **{field: "fake"})
    prod = settings(environment="production", llm_provider="openai", embedding_provider="openai")
    assert prod.environment == "production"
    # even if validation were bypassed, the registry refuses to build it
    bypassed = settings().model_copy(update={"environment": "production", "llm_provider": "fake"})
    with pytest.raises(LLMError) as exc:
        resolve_llm_config(bypassed)
    assert exc.value.code == AIErrorCode.MISCONFIGURED and "production" in str(exc.value)


# --- adding a vendor needs no change to the agent -----------------------------------------------------------


class AcmeNativeProvider:
    """A vendor with its own protocol: translates AIRequest itself, returns an AIResponse."""

    def __init__(self, config: ProviderConfig) -> None:
        self._model = config.model
        self.seen: list[AIRequest] = []

    @property
    def name(self) -> str:
        return "acme"

    @property
    def model(self) -> str:
        return self._model

    def generate(self, request: AIRequest) -> AIResponse:
        self.seen.append(request)
        return AIResponse(text="Acme says hello.", provider="acme", model=self._model)

    def generate_stream(self, request: AIRequest):
        yield "Acme"


def test_a_new_vendor_is_one_catalog_row_and_one_adapter_and_the_agent_is_untouched():
    registry = AIProviderRegistry()
    registry.register_spec(
        ProviderSpec(
            id="acme",
            label="Acme AI",
            adapter="acme_native",
            capabilities=frozenset({Capability.LLM}),
            default_llm_model="acme-1",
            requires_api_key=False,
        )
    )
    registry.register_llm_adapter("acme_native", AcmeNativeProvider)
    config = resolve_llm_config(settings(llm_provider="acme"), registry)
    llm = registry.build_llm(config)
    assert isinstance(llm, AcmeNativeProvider) and llm.model == "acme-1"

    agent = ChatAgent(
        llm=llm,
        embedder=SpyEmbedder(),
        settings=settings(llm_provider="fake", llm_temperature=0.3),
        search=StubSearch({_ORG: [make_hit("We ship in two days.", score=0.9)]}),
    )
    reply = agent.respond(
        None,  # type: ignore[arg-type]
        organization_id=_ORG,
        business_name="Acme Store",
        question="How long does shipping take?",
        history=[],
    )
    assert reply.content == "Acme says hello." and reply.model == "acme-1"
    (request,) = llm.seen
    assert isinstance(request, AIRequest) and request.temperature == 0.3
    assert all(isinstance(m, ChatMessage) for m in request.messages)


def test_registering_the_same_provider_twice_is_refused():
    registry = AIProviderRegistry()
    with pytest.raises(ValueError):
        registry.register_spec(BUILTIN_PROVIDERS[0])


def test_a_spec_whose_adapter_is_missing_is_a_configuration_error_not_a_crash():
    registry = AIProviderRegistry()
    registry.register_spec(
        ProviderSpec(
            id="orphan",
            label="No adapter",
            adapter="does_not_exist",
            capabilities=frozenset({Capability.LLM, Capability.EMBEDDING}),
            default_llm_model="m",
            default_embedding_model="m",
            requires_api_key=False,
        )
    )
    with pytest.raises(LLMError) as exc:
        registry.build_llm(resolve_llm_config(settings(llm_provider="orphan"), registry))
    assert exc.value.code == AIErrorCode.MISCONFIGURED and "does_not_exist" in str(exc.value)
    with pytest.raises(EmbeddingError):
        registry.build_embedding(
            resolve_embedding_config(settings(embedding_provider="orphan"), registry)
        )


# --- the agent depends on the interface only ----------------------------------------------------------------


def test_the_agent_works_unchanged_against_any_openai_compatible_vendor(caplog):
    caplog.set_level(logging.INFO)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "req-groq-1"},
            json={
                "model": "llama-served",
                "choices": [{"message": {"content": "Two days."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 4},
            },
        )

    llm = OpenAICompatibleChatProvider(
        api_key=KEY,
        model="llama-3",
        base_url="https://api.groq.com/openai/v1",
        provider_id="groq",
        transport=httpx.MockTransport(handler),
    )
    agent = ChatAgent(
        llm=llm,
        embedder=SpyEmbedder(),
        settings=settings(llm_provider="fake", llm_temperature=0.2, llm_max_output_tokens=123),
        search=StubSearch({_ORG: [make_hit("Orders ship in two days.", score=0.9)]}),
    )
    reply = agent.respond(
        None,  # type: ignore[arg-type]
        organization_id=_ORG,
        business_name="Shop",
        question="How long does shipping take?",
        history=[],
    )
    assert reply.content == "Two days." and reply.model == "llama-served"
    assert (reply.prompt_tokens, reply.completion_tokens) == (30, 4)
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["body"]["max_tokens"] == 123 and seen["body"]["temperature"] == 0.2
    assert "provider=groq" in caplog.text and "req-groq-1" in caplog.text
    assert KEY not in caplog.text


# --- caching -------------------------------------------------------------------------------------------


def test_providers_are_cached_per_resolved_configuration_and_failures_are_not(monkeypatch):
    factory._cached_llm.cache_clear()
    current = {"s": settings(llm_provider="openai")}  # no key yet
    monkeypatch.setattr(factory, "get_settings", lambda: current["s"])
    with pytest.raises(LLMError):
        factory.get_llm_provider()
    current["s"] = settings(llm_provider="openai", llm_api_key=SecretStr(KEY))
    first = factory.get_llm_provider()
    assert first is factory.get_llm_provider()  # successes are cached
    current["s"] = settings(llm_provider="openai", llm_api_key=SecretStr(KEY), llm_model="other")
    assert factory.get_llm_provider() is not first  # any relevant change builds a new provider
    current["s"] = settings(llm_provider="openai", llm_api_key=SecretStr(OTHER_KEY))
    assert factory.get_llm_provider() is not first  # a rotated key too
    factory._cached_llm.cache_clear()
