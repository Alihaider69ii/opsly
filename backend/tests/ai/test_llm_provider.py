import json

import httpx
import pytest

from app.services.ai.errors import AIError, AIErrorCode, LLMError, classify_http_status
from app.services.ai.provider import AIRequest, AIResponse, AIUsage, ChatMessage
from app.services.ai.providers.fake import FakeLLMProvider
from app.services.ai.providers.openai_compatible import OpenAICompatibleChatProvider

API_KEY = "sk-test-SUPERSECRET-0123456789"
MESSAGES = [
    ChatMessage("system", "You are a helper."),
    ChatMessage("user", "Hi"),
    ChatMessage("assistant", "Hello!"),
    ChatMessage("user", "Shipping?"),
]


def provider(handler, **kwargs) -> OpenAICompatibleChatProvider:
    kwargs.setdefault("base_url", "https://llm.example/v1")
    kwargs.setdefault("api_key", API_KEY)
    return OpenAICompatibleChatProvider(
        model=kwargs.pop("model", "configured-model"),
        transport=httpx.MockTransport(handler),
        sleep=kwargs.pop("sleep", lambda seconds: None),
        **kwargs,
    )


def req(max_tokens: int = 50, **kwargs) -> AIRequest:
    return AIRequest(messages=MESSAGES, max_tokens=max_tokens, **kwargs)


def ok(content="Hello there", **extra):
    body = {
        "id": "chatcmpl-123",
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        **extra,
    }
    return httpx.Response(200, json=body)


# --- the request goes out exactly as configured ------------------------------------------------------


def test_request_shape_comes_entirely_from_configuration_and_the_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok()

    p = provider(
        handler,
        model="my-configured-model",
        base_url="https://llm.example/v1/",
        max_tokens_field="max_completion_tokens",
    )
    p.generate(req(321))
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == f"Bearer {API_KEY}"
    body = seen["body"]
    assert body["model"] == "my-configured-model" and body["max_completion_tokens"] == 321
    assert "max_tokens" not in body
    assert body["messages"] == [{"role": m.role, "content": m.content} for m in MESSAGES]
    assert "temperature" not in body and "stream" not in body


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_the_name_of_the_token_limit_field_is_configuration_not_a_vendor_branch(field):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok()

    provider(handler, max_tokens_field=field).generate(req(77))
    other = ({"max_tokens", "max_completion_tokens"} - {field}).pop()
    assert seen["body"][field] == 77 and other not in seen["body"]


def test_a_request_can_override_the_model_and_temperature_for_itself_only():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return ok()

    p = provider(handler, model="default-model", temperature=0.9)
    p.generate(req(model="special-model", temperature=0.1))
    p.generate(req())
    assert (bodies[0]["model"], bodies[0]["temperature"]) == ("special-model", 0.1)
    assert (bodies[1]["model"], bodies[1]["temperature"]) == ("default-model", 0.9)


def test_temperature_is_omitted_unless_the_request_or_the_provider_sets_one():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok()

    provider(handler).generate(req())
    assert "temperature" not in seen["body"]
    provider(handler, temperature=0.2).generate(req())
    assert seen["body"]["temperature"] == 0.2


def test_request_metadata_is_never_sent_to_the_vendor():
    seen = {}

    def handler(request):
        seen["raw"] = request.content.decode()
        return ok()

    provider(handler).generate(req(metadata={"conversation": "conv-123-secret-trace"}))
    assert "conv-123-secret-trace" not in seen["raw"]


def test_no_authorization_header_is_sent_when_the_endpoint_needs_no_key():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return ok()

    provider(handler, api_key=None).generate(req())
    assert "authorization" not in seen["headers"]


# --- the reply is normalized -----------------------------------------------------------------------------


def test_the_reply_is_a_vendor_neutral_response_object():
    result = provider(lambda r: ok(), provider_id="openrouter").generate(req())
    assert isinstance(result, AIResponse)
    assert result.text == "Hello there"
    assert result.provider == "openrouter" and result.model == "served-model"
    assert result.finish_reason == "stop"
    assert result.usage == AIUsage(prompt_tokens=12, completion_tokens=5, total_tokens=17)
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert result.request_id == "chatcmpl-123"  # falls back to the body id


def test_the_vendor_request_id_header_wins_and_is_bounded():
    def handler(request):
        response = ok()
        response.headers["x-request-id"] = "req_" + "a" * 500
        return response

    assert len(provider(handler).generate(req()).request_id) == 128


def test_usage_fields_are_all_optional_because_vendors_differ():
    no_usage = provider(lambda r: ok(usage=None)).generate(req())
    assert no_usage.usage is None
    partial = provider(lambda r: ok(usage={"prompt_tokens": 9})).generate(req())
    assert partial.usage == AIUsage(prompt_tokens=9, completion_tokens=None, total_tokens=None)
    vendor_total = provider(
        lambda r: ok(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99})
    ).generate(req())
    assert vendor_total.usage.total_tokens == 99  # a vendor-supplied total is kept, not recomputed
    junk = provider(
        lambda r: ok(usage={"prompt_tokens": "many", "completion_tokens": True})
    ).generate(req())
    assert junk.usage == AIUsage()


def test_model_falls_back_to_the_requested_one_when_the_vendor_omits_it():
    body = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
    result = provider(lambda r: httpx.Response(200, json=body), model="cfg").generate(
        req(model="asked")
    )
    assert result.model == "asked"


def test_provider_identity_and_secrecy():
    p = provider(lambda r: ok(), model="gpt-x", provider_id="groq")
    assert (p.name, p.model) == ("groq", "gpt-x")
    assert API_KEY not in repr(p) and API_KEY not in str(p)


# --- failures are normalized -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, None, AIErrorCode.AUTH),
        (403, None, AIErrorCode.AUTH),
        (429, None, AIErrorCode.RATE_LIMITED),
        (408, None, AIErrorCode.TIMEOUT),
        (504, None, AIErrorCode.TIMEOUT),
        (500, None, AIErrorCode.UNAVAILABLE),
        (503, None, AIErrorCode.UNAVAILABLE),
        (400, {"error": {"message": "context length exceeded"}}, AIErrorCode.INVALID_REQUEST),
        (422, None, AIErrorCode.INVALID_REQUEST),
        (
            404,
            {"error": {"code": "model_not_found", "message": "no such model"}},
            AIErrorCode.MODEL_UNAVAILABLE,
        ),
        (
            404,
            {"error": {"message": "The model `x` does not exist"}},
            AIErrorCode.MODEL_UNAVAILABLE,
        ),
        (404, None, AIErrorCode.MISCONFIGURED),  # wrong base URL: no model hint
        (400, {"error": {"code": "model_not_found"}}, AIErrorCode.MODEL_UNAVAILABLE),
        # free text that merely mentions a model must NOT be read as "model unavailable"
        (
            400,
            {"error": {"message": "context too long for model gpt-x"}},
            AIErrorCode.INVALID_REQUEST,
        ),
    ],
)
def test_http_failures_map_to_the_same_codes_whatever_the_vendor(status, body, code):
    assert classify_http_status(status, body) == code
    with pytest.raises(LLMError) as exc:
        provider(lambda r: httpx.Response(status, json=body or {})).generate(req())
    assert exc.value.code == code and isinstance(exc.value, AIError)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_an_auth_error_and_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": f"Incorrect API key {API_KEY}"}})

    with pytest.raises(LLMError) as exc:
        provider(handler).generate(req())
    assert exc.value.code == AIErrorCode.AUTH and len(calls) == 1
    assert API_KEY not in str(exc.value) and "Incorrect" not in str(exc.value)
    assert exc.value.provider == "openai" and not exc.value.retryable


def test_errors_never_carry_vendor_text_prompts_or_keys():
    body = {"error": {"message": f"echo of prompt Shipping? and key {API_KEY} trace"}}
    for status in (400, 401, 404, 429, 500):
        with pytest.raises(LLMError) as exc:
            provider(lambda r, s=status: httpx.Response(s, json=body)).generate(req())
        text = str(exc.value) + repr(exc.value)
        assert API_KEY not in text and "Shipping" not in text and "echo" not in text


def test_rate_limit_is_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429) if len(calls) == 1 else ok()

    assert provider(handler).generate(req()).text == "Hello there"
    assert len(calls) == 2


def test_retry_after_is_honoured_but_capped():
    sleeps = []

    def handler(request):
        return httpx.Response(429, headers={"retry-after": "3"})

    with pytest.raises(LLMError):
        provider(handler, sleep=sleeps.append, max_attempts=2).generate(req())
    assert sleeps == [3.0]

    sleeps.clear()
    with pytest.raises(LLMError):
        provider(
            lambda r: httpx.Response(429, headers={"retry-after": "3600"}),
            sleep=sleeps.append,
            max_attempts=2,
        ).generate(req())
    assert sleeps == [10.0]  # a hostile or buggy Retry-After cannot stall a request for an hour

    sleeps.clear()
    with pytest.raises(LLMError):
        provider(
            lambda r: httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            sleep=sleeps.append,
            max_attempts=2,
        ).generate(req())
    assert sleeps == [0.5]  # the HTTP-date form falls back to normal backoff


def test_persistent_rate_limit_is_reported():
    with pytest.raises(LLMError) as exc:
        provider(lambda r: httpx.Response(429)).generate(req())
    assert exc.value.code == AIErrorCode.RATE_LIMITED and exc.value.retryable


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried_then_reported_without_details(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, text="internal trace " + API_KEY)

    with pytest.raises(LLMError) as exc:
        provider(handler, max_attempts=2).generate(req())
    assert exc.value.code in (AIErrorCode.UNAVAILABLE, AIErrorCode.TIMEOUT) and len(calls) == 2
    assert "trace" not in str(exc.value) and API_KEY not in str(exc.value)


def test_timeout_and_connection_errors():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(LLMError) as exc:
        provider(slow, max_attempts=2).generate(req())
    assert exc.value.code == AIErrorCode.TIMEOUT
    with pytest.raises(LLMError) as exc:
        provider(down, max_attempts=2).generate(req())
    assert exc.value.code == AIErrorCode.UNAVAILABLE


def test_invalid_request_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "context length exceeded"}})

    with pytest.raises(LLMError) as exc:
        provider(handler).generate(req())
    assert exc.value.code == AIErrorCode.INVALID_REQUEST and len(calls) == 1
    assert "context" not in str(exc.value)


def test_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(LLMError):
        provider(handler).generate(req())
    assert calls == ["https://llm.example/v1/chat/completions"]  # never reaches the redirect target


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": ["a"]}}]}),
        httpx.Response(200, json=[1, 2]),
    ],
)
def test_malformed_provider_responses_are_rejected(response):
    with pytest.raises(LLMError) as exc:
        provider(lambda r: response).generate(req())
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR


def test_content_filter_is_reported():
    body = {"choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}]}
    with pytest.raises(LLMError) as exc:
        provider(lambda r: httpx.Response(200, json=body)).generate(req())
    assert exc.value.code == AIErrorCode.CONTENT_FILTERED


def test_truncated_reply_is_returned_with_its_finish_reason():
    body = {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
    result = provider(lambda r: httpx.Response(200, json=body)).generate(req(5))
    assert (result.text, result.finish_reason) == ("partial", "length")


def test_error_codes_must_be_known_codes():
    with pytest.raises(ValueError):
        LLMError("bad_response")  # the old, vendor-flavoured name no longer exists
    assert LLMError("auth").code is AIErrorCode.AUTH


# --- streaming (optional capability) -----------------------------------------------------------------------


def sse(*events: str) -> httpx.Response:
    body = "".join(f"data: {e}\n\n" for e in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def chunk(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def test_stream_yields_fragments_and_stops_at_done():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return sse(
            json.dumps({"choices": [{"delta": {"role": "assistant"}}]}),
            chunk("Hel"),
            chunk("lo"),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "[DONE]",
            chunk("never seen"),
        )

    assert list(provider(handler).generate_stream(req())) == ["Hel", "lo"]
    assert seen["body"]["stream"] is True


def test_stream_errors_are_mapped():
    with pytest.raises(LLMError) as exc:
        list(provider(lambda r: httpx.Response(401)).generate_stream(req()))
    assert exc.value.code == AIErrorCode.AUTH
    with pytest.raises(LLMError) as exc:
        list(provider(lambda r: sse("not json")).generate_stream(req()))
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR
    filtered = json.dumps({"choices": [{"delta": {}, "finish_reason": "content_filter"}]})
    with pytest.raises(LLMError) as exc:
        list(provider(lambda r: sse(filtered)).generate_stream(req()))
    assert exc.value.code == AIErrorCode.CONTENT_FILTERED


# --- the fake provider ---------------------------------------------------------------------------------------


def test_fake_llm_quotes_the_first_source_and_admits_ignorance_without_one():
    knowledge = ChatMessage(
        "system",
        "BUSINESS INFORMATION\n\n<business_information>\nSOURCE 1\nTitle: T\nURL: u\nContent:\n"
        "Orders ship in two days.\n\nSOURCE 2\nTitle: U\nURL: v\nContent:\nOther.\n</business_information>",
    )
    fake = FakeLLMProvider()
    answer = fake.generate(AIRequest([knowledge], max_tokens=200))
    assert isinstance(answer, AIResponse) and answer.provider == "fake"
    assert "Orders ship in two days." in answer.text and "Other." not in answer.text
    nothing = fake.generate(AIRequest([ChatMessage("user", "hi")], max_tokens=50))
    assert nothing.text.startswith("I don't have")
    streamed = "".join(fake.generate_stream(AIRequest([knowledge], max_tokens=200)))
    assert streamed.strip().startswith("Here is what")
