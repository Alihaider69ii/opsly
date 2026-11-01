import json
import math

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.db.models.knowledge import EMBEDDING_DIM
from app.services.ai.embeddings import EmbeddingError, embedding_input
from app.services.ai.factory import build_embedding_provider
from app.services.ai.providers.fake import FakeEmbeddingProvider
from app.services.ai.providers.gemini_embedding import GeminiEmbeddingProvider
from app.services.ai.providers.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.services.ai.registry import resolve_embedding_config

API_KEY = "sk-test-SUPERSECRET-0123456789"


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- fake provider -------------------------------------------------------------------------------


def test_fake_provider_is_deterministic_normalized_and_the_right_size():
    provider = FakeEmbeddingProvider()
    a1, a2 = provider.embed(["free shipping over fifty dollars"] * 2)
    assert a1 == a2
    assert len(a1) == EMBEDDING_DIM == provider.dimensions
    assert math.isclose(math.sqrt(sum(x * x for x in a1)), 1.0, rel_tol=1e-9)


def test_fake_provider_ranks_texts_that_share_words_higher():
    provider = FakeEmbeddingProvider()
    query, near, far = provider.embed(
        ["how long does shipping take", "shipping takes three to five days", "our soap is vegan"]
    )
    assert cosine(query, near) > cosine(query, far)
    assert cosine(query, query) == pytest.approx(1.0)


def test_fake_provider_handles_empty_and_symbol_only_text():
    for text in ("", "   ", "!!! ???"):
        vector = FakeEmbeddingProvider().embed([text])[0]
        assert len(vector) == EMBEDDING_DIM and math.isclose(sum(x * x for x in vector), 1.0)


def test_fake_provider_order_matches_input_order():
    provider = FakeEmbeddingProvider()
    texts = ["alpha beta", "gamma delta", "epsilon zeta"]
    assert provider.embed(texts) == [provider.embed([t])[0] for t in texts]
    assert provider.model_id == "fake:hash-v1"


def test_embedding_input_prefixes_the_title_only_when_there_is_one():
    assert embedding_input("Returns", "30 days.") == "Returns\n\n30 days."
    assert embedding_input(None, "30 days.") == "30 days."


# --- OpenAI provider (HTTP mocked; nothing here ever touches the network) --------------------------


def make_provider(handler, **kwargs):
    kwargs.setdefault("dimensions", 4)
    kwargs.setdefault("base_url", "https://api.openai.com/v1")
    kwargs.setdefault("send_dimensions", True)
    return OpenAICompatibleEmbeddingProvider(
        api_key=API_KEY,
        model=kwargs.pop("model", "text-embedding-3-small"),
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: None,
        **kwargs,
    )


def ok_response(request, dims=4):
    inputs = json.loads(request.content)["input"]
    data = [
        {"index": i, "embedding": [float(i + 1)] * dims, "object": "embedding"}
        for i in range(len(inputs))
    ]
    return httpx.Response(200, json={"data": list(reversed(data)), "model": "x"})  # shuffled order


def test_successful_embedding_returns_vectors_in_input_order():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return ok_response(request)

    provider = make_provider(handler)
    vectors = provider.embed(["one", "two", "three"])
    assert vectors == [[1.0] * 4, [2.0] * 4, [3.0] * 4]
    assert seen["url"] == "https://api.openai.com/v1/embeddings"
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["body"]["model"] == "text-embedding-3-small"
    assert seen["body"]["dimensions"] == 4  # text-embedding-3 models are asked for our column size
    assert provider.model_id == "openai:text-embedding-3-small"


def test_dimensions_parameter_is_sent_only_to_models_the_catalog_says_accept_it():
    from app.services.ai.registry import resolve_embedding_config

    def built(**overrides):
        return OpenAICompatibleEmbeddingProvider.from_config(
            resolve_embedding_config(
                settings(
                    embedding_provider="openai", openai_api_key=SecretStr(API_KEY), **overrides
                )
            )
        )

    assert built(embedding_model="text-embedding-3-small")._send_dimensions is True
    assert built(embedding_model="text-embedding-ada-002")._send_dimensions is False
    # other vendors are not assumed to understand it
    other = OpenAICompatibleEmbeddingProvider.from_config(
        resolve_embedding_config(
            settings(
                embedding_provider="custom_openai_compatible",
                embedding_base_url="https://gateway.example/v1",
                embedding_api_key=SecretStr(API_KEY),
                embedding_model="text-embedding-3-small",
            )
        )
    )
    assert other._send_dimensions is False


def test_a_provider_that_does_not_send_dimensions_omits_the_field():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok_response(request)

    make_provider(handler, send_dimensions=False).embed(["x"])
    assert "dimensions" not in seen["body"]


def test_large_inputs_are_split_into_batches():
    sizes = []

    def handler(request):
        sizes.append(len(json.loads(request.content)["input"]))
        return ok_response(request)

    vectors = make_provider(handler, batch_size=10).embed([f"t{i}" for i in range(25)])
    assert sizes == [10, 10, 5] and len(vectors) == 25


def test_empty_input_makes_no_request():
    def handler(request):
        raise AssertionError("no request expected")

    assert make_provider(handler).embed([]) == []


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_api_key_is_an_auth_error_and_is_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": f"bad key {API_KEY}"}})

    with pytest.raises(EmbeddingError) as exc:
        make_provider(handler).embed(["x"])
    assert exc.value.code == "auth" and len(calls) == 1
    assert API_KEY not in str(exc.value) and "bad key" not in str(exc.value)


def test_rate_limit_is_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": "slow down"})
        return ok_response(request)

    assert make_provider(handler).embed(["x"]) == [[1.0] * 4]
    assert len(calls) == 3


def test_persistent_rate_limit_gives_up_with_a_rate_limited_error():
    with pytest.raises(EmbeddingError) as exc:
        make_provider(lambda r: httpx.Response(429), max_attempts=3).embed(["x"])
    assert exc.value.code == "rate_limited"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retried_then_reported_as_unavailable(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, text="upstream exploded " + API_KEY)

    with pytest.raises(EmbeddingError) as exc:
        make_provider(handler, max_attempts=3).embed(["x"])
    assert exc.value.code == "unavailable" and len(calls) == 3
    assert API_KEY not in str(exc.value) and "exploded" not in str(exc.value)


def test_timeouts_are_retried_then_reported():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(EmbeddingError) as exc:
        make_provider(handler, max_attempts=2).embed(["x"])
    assert exc.value.code == "timeout"


def test_connection_errors_are_reported_as_unavailable():
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(EmbeddingError) as exc:
        make_provider(handler, max_attempts=2).embed(["x"])
    assert exc.value.code == "unavailable"


def test_bad_request_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "input too long"}})

    with pytest.raises(EmbeddingError) as exc:
        make_provider(handler).embed(["x"])
    assert exc.value.code == "invalid_request" and len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": True},
        {"data": [{"index": 0}]},
        {"data": "nope"},
        {"data": []},  # wrong count
        [1, 2, 3],
    ],
)
def test_malformed_provider_responses_are_rejected(payload):
    with pytest.raises(EmbeddingError) as exc:
        make_provider(lambda r: httpx.Response(200, json=payload)).embed(["x"])
    assert exc.value.code == "provider_error"


def test_a_model_with_the_wrong_vector_size_is_a_configuration_error_not_a_transient_one():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    with pytest.raises(EmbeddingError) as exc:
        make_provider(handler).embed(["x"])
    assert exc.value.code == "misconfigured" and len(calls) == 1
    assert "4" in str(exc.value) and "size 2" in str(exc.value)  # tells the operator what to fix


def test_non_json_response_is_rejected():
    with pytest.raises(EmbeddingError) as exc:
        make_provider(lambda r: httpx.Response(200, text="<html>oops</html>")).embed(["x"])
    assert exc.value.code == "provider_error"


# --- Gemini provider (HTTP mocked; nothing here ever touches the network) --------------------------


def make_gemini_provider(handler, **kwargs):
    kwargs.setdefault("dimensions", 1536)
    kwargs.setdefault("base_url", "https://generativelanguage.googleapis.com/v1beta")
    return GeminiEmbeddingProvider(
        api_key=API_KEY,
        model=kwargs.pop("model", "gemini-embedding-2"),
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: None,
        **kwargs,
    )


def gemini_ok_response(request, dims=1536):
    # Unlike OpenAI's /embeddings, batchEmbedContents returns no index to re-sort by -- Google
    # documents the response as "in the same order as provided in the batch request", and the
    # adapter trusts that positional guarantee rather than re-sorting, so this fake vendor must
    # actually preserve order for the test to mean anything.
    requests = json.loads(request.content)["requests"]
    data = [{"values": [float(i + 1)] * dims} for i in range(len(requests))]
    return httpx.Response(200, json={"embeddings": data})


def test_gemini_embedding_returns_1536_wide_vectors_in_input_order():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("x-goog-api-key")
        seen["authorization"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return gemini_ok_response(request)

    provider = make_gemini_provider(handler)
    vectors = provider.embed(["one", "two", "three"])
    assert vectors == [[1.0] * 1536, [2.0] * 1536, [3.0] * 1536]
    assert all(len(v) == 1536 for v in vectors)
    assert seen["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-embedding-2:batchEmbedContents"
    )
    assert seen["auth"] == API_KEY  # x-goog-api-key, not a bearer token
    assert seen["authorization"] is None  # never both
    requests = seen["body"]["requests"]
    assert [r["content"]["parts"][0]["text"] for r in requests] == ["one", "two", "three"]
    assert all(r["model"] == "models/gemini-embedding-2" for r in requests)
    assert all(r["outputDimensionality"] == 1536 for r in requests)
    assert provider.model_id == "gemini:gemini-embedding-2"
    assert provider.dimensions == 1536


def test_gemini_document_and_query_embeddings_use_the_identical_request_shape():
    """No task_type/prefix distinction exists for this model, and the codebase has no query-vs-
    document branch (`services/ai/rag.py` and `services/ai/rag/indexing.py` both call `embed()`
    directly) -- so a query embedding and a document embedding must produce byte-identical request
    shapes for the same text, keeping them comparable in the vector column."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return gemini_ok_response(request, dims=1536)

    provider = make_gemini_provider(handler)
    query_text = "How long does shipping take?"
    document_text = embedding_input("Shipping", "Orders ship in two to three business days.")
    provider.embed([query_text])  # as rag.py calls it for a question
    provider.embed([document_text])  # as rag/indexing.py calls it for a chunk
    assert len(bodies) == 2
    for body in bodies:
        (item,) = body["requests"]
        assert set(item) == {"model", "content", "outputDimensionality"}
        assert set(item["content"]) == {"parts"}


def test_gemini_large_inputs_are_split_into_batches():
    sizes = []

    def handler(request):
        sizes.append(len(json.loads(request.content)["requests"]))
        return gemini_ok_response(request)

    vectors = make_gemini_provider(handler, batch_size=10).embed([f"t{i}" for i in range(25)])
    assert sizes == [10, 10, 5] and len(vectors) == 25


def test_gemini_empty_input_makes_no_request():
    def handler(request):
        raise AssertionError("no request expected")

    assert make_gemini_provider(handler).embed([]) == []


@pytest.mark.parametrize("status", [401, 403])
def test_gemini_rejected_api_key_is_an_auth_error_and_is_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            status, json={"error": {"code": status, "message": f"bad key {API_KEY}"}}
        )

    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(handler).embed(["x"])
    assert exc.value.code == "auth" and len(calls) == 1
    assert API_KEY not in str(exc.value) and "bad key" not in str(exc.value)


def test_gemini_rate_limit_is_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"code": 429, "message": "slow down"}})
        return gemini_ok_response(request)

    assert make_gemini_provider(handler).embed(["x"]) == [[1.0] * 1536]
    assert len(calls) == 3


def test_gemini_persistent_rate_limit_gives_up_with_a_rate_limited_error():
    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(lambda r: httpx.Response(429), max_attempts=3).embed(["x"])
    assert exc.value.code == "rate_limited"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_gemini_server_errors_are_retried_then_reported_as_unavailable(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, text="upstream exploded " + API_KEY)

    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(handler, max_attempts=3).embed(["x"])
    assert exc.value.code == "unavailable" and len(calls) == 3
    assert API_KEY not in str(exc.value) and "exploded" not in str(exc.value)


def test_gemini_timeouts_are_retried_then_reported():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(handler, max_attempts=2).embed(["x"])
    assert exc.value.code == "timeout"


def test_gemini_connection_errors_are_reported_as_unavailable():
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(handler, max_attempts=2).embed(["x"])
    assert exc.value.code == "unavailable"


def test_gemini_bad_request_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": 400, "message": "input too long"}})

    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(handler).embed(["x"])
    assert exc.value.code == "invalid_request" and len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": True},
        {"embeddings": [{"nope": True}]},
        {"embeddings": "nope"},
        {"embeddings": []},  # wrong count
        [1, 2, 3],
    ],
)
def test_gemini_malformed_provider_responses_are_rejected(payload):
    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(lambda r: httpx.Response(200, json=payload)).embed(["x"])
    assert exc.value.code == "provider_error"


def test_gemini_model_with_the_wrong_vector_size_is_a_configuration_error_not_a_transient_one():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"embeddings": [{"values": [1.0, 2.0]}]})

    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(handler, dimensions=1536).embed(["x"])
    assert exc.value.code == "misconfigured" and len(calls) == 1
    assert "1536" in str(exc.value) and "size 2" in str(exc.value)


def test_gemini_non_json_response_is_rejected():
    with pytest.raises(EmbeddingError) as exc:
        make_gemini_provider(lambda r: httpx.Response(200, text="<html>oops</html>")).embed(["x"])
    assert exc.value.code == "provider_error"


# --- configuration -----------------------------------------------------------------------------


def settings(**overrides) -> Settings:
    base = {"database_url": "sqlite://", "jwt_secret_key": "k" * 40}
    return Settings(_env_file=None, **{**base, **overrides})


def test_fake_provider_is_selected_by_configuration():
    assert isinstance(
        build_embedding_provider(settings(embedding_provider="fake")), FakeEmbeddingProvider
    )


def test_openai_provider_requires_an_api_key():
    with pytest.raises(EmbeddingError) as exc:
        build_embedding_provider(settings(embedding_provider="openai"))
    assert exc.value.code == "unconfigured"
    with pytest.raises(EmbeddingError):
        build_embedding_provider(
            settings(embedding_provider="openai", openai_api_key=SecretStr("  "))
        )


def test_openai_provider_is_built_from_configuration():
    provider = build_embedding_provider(
        settings(
            embedding_provider="openai",
            openai_api_key=SecretStr(API_KEY),
            embedding_model="text-embedding-3-large",
        )
    )
    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
    assert (
        provider.model_id == "openai:text-embedding-3-large"
        and provider.dimensions == EMBEDDING_DIM
    )


def test_gemini_provider_requires_an_api_key():
    with pytest.raises(EmbeddingError) as exc:
        build_embedding_provider(settings(embedding_provider="gemini"))
    assert exc.value.code == "unconfigured"
    assert "EMBEDDING_API_KEY" in str(exc.value)


def test_gemini_provider_is_selected_by_configuration_and_defaults_to_gemini_embedding_2():
    provider = build_embedding_provider(
        settings(embedding_provider="gemini", embedding_api_key=SecretStr(API_KEY))
    )
    assert isinstance(provider, GeminiEmbeddingProvider)
    assert provider.model_id == "gemini:gemini-embedding-2" and provider.dimensions == EMBEDDING_DIM


def test_gemini_embedding_model_is_configurable():
    provider = build_embedding_provider(
        settings(
            embedding_provider="gemini",
            embedding_api_key=SecretStr(API_KEY),
            embedding_model="gemini-embedding-2-preview",
        )
    )
    assert provider.model_id == "gemini:gemini-embedding-2-preview"


def test_gemini_embedding_dimensions_other_than_the_database_column_are_refused():
    with pytest.raises(EmbeddingError) as exc:
        build_embedding_provider(
            settings(
                embedding_provider="gemini",
                embedding_api_key=SecretStr(API_KEY),
                embedding_dimensions=768,
            )
        )
    assert exc.value.code == "misconfigured"


def test_gemini_embeddings_are_refused_in_production_without_a_key_but_allowed_with_one():
    with pytest.raises(EmbeddingError):
        build_embedding_provider(settings(environment="production", embedding_provider="gemini"))
    build_embedding_provider(  # valid: EMBEDDING_PROVIDER=gemini, model, 1536 dims, a key
        settings(
            environment="production",
            embedding_provider="gemini",
            embedding_api_key=SecretStr(API_KEY),
            embedding_model="gemini-embedding-2",
            embedding_dimensions=EMBEDDING_DIM,
        )
    )


def test_gemini_api_key_never_appears_in_settings_repr_or_dump():
    s = settings(embedding_provider="gemini", embedding_api_key=SecretStr(API_KEY))
    assert API_KEY not in repr(s) and API_KEY not in str(s)
    assert API_KEY not in json.dumps(s.model_dump(mode="json"), default=str)
    resolved = resolve_embedding_config(s)
    assert API_KEY not in repr(resolved) and API_KEY not in repr(resolved.cache_key())
    assert resolved.secret == API_KEY  # available to the adapter, and only through this property


def test_dimension_mismatch_with_the_database_column_is_refused():
    with pytest.raises(EmbeddingError) as exc:
        build_embedding_provider(settings(embedding_provider="fake", embedding_dimensions=768))
    assert exc.value.code == "misconfigured"


def test_fake_embeddings_are_refused_in_production():
    with pytest.raises(ValueError, match="not allowed"):
        settings(environment="production", embedding_provider="fake")
    settings(environment="production", embedding_provider="openai")  # fine


def test_api_key_never_appears_in_settings_repr_or_dump():
    s = settings(openai_api_key=SecretStr(API_KEY))
    assert API_KEY not in repr(s) and API_KEY not in str(s)
    assert API_KEY not in json.dumps(s.model_dump(mode="json"), default=str)
