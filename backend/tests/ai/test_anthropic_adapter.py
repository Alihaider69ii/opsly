"""The Anthropic Messages adapter against the contract in Anthropic's documentation
(https://platform.claude.com/docs/en/api/messages and .../api/errors, verified October 2026).

Nothing here talks to Anthropic: every response is a mocked one built from the documented shapes.
That proves the translation, not that a live key works; see docs/ai-providers.md."""

import json
import logging
import uuid

import httpx
import pytest

from app.core.config import Settings
from app.services.ai.agent import ChatAgent
from app.services.ai.errors import AIErrorCode, LLMError
from app.services.ai.provider import AIRequest, ChatMessage
from app.services.ai.providers.anthropic_messages import (
    ANTHROPIC_VERSION,
    AnthropicMessagesProvider,
)
from tests.chat.helpers import SpyEmbedder, StubSearch, make_hit

API_KEY = "sk-ant-api03-SUPERSECRET-0123456789"
MESSAGES = [
    ChatMessage("system", "You are a helper."),
    ChatMessage("system", "Answer briefly."),
    ChatMessage("user", "Hi"),
    ChatMessage("assistant", "Hello!"),
    ChatMessage("user", "Shipping?"),
]


def provider(handler, **kwargs) -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(
        api_key=kwargs.pop("api_key", API_KEY),
        model=kwargs.pop("model", "configured-model"),
        base_url=kwargs.pop("base_url", "https://api.anthropic.com"),
        transport=httpx.MockTransport(handler),
        sleep=kwargs.pop("sleep", lambda seconds: None),
        **kwargs,
    )


def req(max_tokens: int = 50, messages=MESSAGES, **kwargs) -> AIRequest:
    return AIRequest(messages=messages, max_tokens=max_tokens, **kwargs)


def ok(blocks=None, stop_reason="end_turn", **extra):
    body = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "served-model",
        "content": blocks if blocks is not None else [{"type": "text", "text": "Two days."}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 12, "output_tokens": 5},
        **extra,
    }
    return httpx.Response(200, headers={"request-id": "req_011CS"}, json=body)


def error(status, kind, message="nope", headers=None):
    return httpx.Response(
        status,
        headers=headers or {},
        json={
            "type": "error",
            "error": {"type": kind, "message": message},
            "request_id": "req_err",
        },
    )


# --- the request is exactly what the Messages API specifies --------------------------------------------


def test_the_request_follows_the_messages_api_contract():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return ok()

    provider(handler).generate(req(max_tokens=77, temperature=0.3, metadata={"conversation": "c1"}))
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == API_KEY
    assert seen["headers"]["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
    assert "authorization" not in seen["headers"]  # not an OpenAI-style bearer token
    body = seen["body"]
    assert body["model"] == "configured-model" and body["max_tokens"] == 77
    assert body["system"] == "You are a helper.\n\nAnswer briefly."  # system is not a message
    assert body["messages"] == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "Shipping?"},
    ]
    assert body["temperature"] == 0.3
    assert "stream" not in body and "metadata" not in body  # request metadata is never forwarded


def test_temperature_is_clamped_to_anthropics_range_and_omitted_when_unset():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return ok()

    p = provider(handler)
    p.generate(req(temperature=1.8))  # valid for OpenAI-style APIs, not for Anthropic
    p.generate(req(temperature=-0.5))
    p.generate(req())
    assert [b.get("temperature") for b in bodies] == [1.0, 0.0, None]


def test_a_request_can_override_the_model_for_itself_only():
    models = []

    def handler(request):
        models.append(json.loads(request.content)["model"])
        return ok()

    p = provider(handler)
    p.generate(req(model="claude-other"))
    p.generate(req())
    assert models == ["claude-other", "configured-model"]


def test_no_system_message_means_no_system_field():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok()

    provider(handler).generate(req(messages=[ChatMessage("user", "Hi")]))
    assert "system" not in seen["body"]


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [ChatMessage("system", "only a system prompt")],
        [ChatMessage("user", "Hi"), ChatMessage("assistant", "a prefilled turn")],
    ],
)
def test_a_conversation_that_cannot_end_with_a_user_turn_fails_locally_without_a_request(messages):
    calls = []

    def handler(request):
        calls.append(request)
        return ok()

    with pytest.raises(LLMError) as exc:
        provider(handler).generate(req(messages=messages))
    assert exc.value.code == AIErrorCode.INVALID_REQUEST and calls == []


# --- the reply is normalized ------------------------------------------------------------------------------


def test_the_reply_becomes_a_vendor_neutral_response():
    response = provider(lambda r: ok()).generate(req())
    assert response.text == "Two days."
    assert (response.provider, response.model) == ("anthropic", "served-model")
    assert response.usage is not None
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (12, 5)
    assert response.usage.total_tokens == 17  # derived: Anthropic reports only the two parts
    assert response.finish_reason == "stop" and response.request_id == "req_011CS"
    assert response.latency_ms >= 0


def test_only_text_blocks_are_returned_other_block_kinds_are_ignored():
    blocks = [
        {"type": "thinking", "thinking": "private reasoning"},
        {"type": "text", "text": "Hello "},
        {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}},
        {"type": "text", "text": "world"},
    ]
    response = provider(lambda r: ok(blocks)).generate(req())
    assert response.text == "Hello world" and "private reasoning" not in response.text


@pytest.mark.parametrize(
    ("stop_reason", "finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_use"),
    ],
)
def test_stop_reasons_map_to_the_neutral_finish_reasons(stop_reason, finish):
    assert provider(lambda r: ok(stop_reason=stop_reason)).generate(req()).finish_reason == finish


def test_a_truncated_reply_is_still_returned_with_its_finish_reason():
    response = provider(lambda r: ok(stop_reason="max_tokens")).generate(req())
    assert response.text and response.finish_reason == "length"


def test_usage_and_request_id_are_optional_not_required():
    def handler(request):
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )

    response = provider(handler).generate(req())
    assert response.usage is None and response.request_id is None
    assert response.model == "configured-model"


def test_a_refusal_is_reported_as_filtered_content():
    with pytest.raises(LLMError) as exc:
        provider(lambda r: ok(stop_reason="refusal")).generate(req())
    assert exc.value.code == AIErrorCode.CONTENT_FILTERED


@pytest.mark.parametrize(
    "blocks",
    [
        [],
        [{"type": "text", "text": ""}],
        [{"type": "text", "text": "   "}],
        [{"type": "tool_use", "id": "t", "name": "n", "input": {}}],
    ],
)
def test_an_empty_reply_is_a_provider_error_not_a_blank_answer(blocks):
    with pytest.raises(LLMError) as exc:
        provider(lambda r: ok(blocks)).generate(req())
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"no": "content"}),
        httpx.Response(200, json={"content": "not a list"}),
        httpx.Response(200, json={"content": [None, 3]}),
    ],
)
def test_malformed_replies_are_provider_errors(response):
    with pytest.raises(LLMError) as exc:
        provider(lambda r: response).generate(req())
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR


# --- errors, per Anthropic's documented table ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (400, "invalid_request_error", AIErrorCode.INVALID_REQUEST),
        (401, "authentication_error", AIErrorCode.AUTH),
        (402, "billing_error", AIErrorCode.AUTH),
        (403, "permission_error", AIErrorCode.AUTH),
        (404, "not_found_error", AIErrorCode.MISCONFIGURED),  # a wrong base URL
        (413, "request_too_large", AIErrorCode.INVALID_REQUEST),
        (429, "rate_limit_error", AIErrorCode.RATE_LIMITED),
        (500, "api_error", AIErrorCode.UNAVAILABLE),
        (504, "timeout_error", AIErrorCode.TIMEOUT),
        (529, "overloaded_error", AIErrorCode.UNAVAILABLE),
    ],
)
def test_every_documented_http_error_maps_to_a_normalized_code(status, kind, code):
    with pytest.raises(LLMError) as exc:
        provider(lambda r: error(status, kind)).generate(req())
    assert exc.value.code == code


def test_an_unknown_model_is_a_model_error():
    def handler(request):
        return error(404, "not_found_error", "model: claude-nope")

    with pytest.raises(LLMError) as exc:
        provider(handler).generate(req())
    assert exc.value.code == AIErrorCode.MODEL_UNAVAILABLE


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "invalid_request_error"), (401, "authentication_error"), (403, "permission_error")],
)
def test_permanent_failures_are_not_retried(status, kind):
    calls = []

    def handler(request):
        calls.append(1)
        return error(status, kind)

    with pytest.raises(LLMError):
        provider(handler).generate(req())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "kind"), [(429, "rate_limit_error"), (529, "overloaded_error"), (500, "api_error")]
)
def test_transient_failures_are_retried_a_bounded_number_of_times(status, kind):
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        return error(status, kind)

    with pytest.raises(LLMError) as exc:
        provider(handler, sleep=sleeps.append, max_attempts=3).generate(req())
    assert len(calls) == 3 and len(sleeps) == 2 and exc.value.retryable


def test_retry_after_is_honoured_and_a_later_attempt_can_succeed():
    answers = iter([error(429, "rate_limit_error", headers={"retry-after": "7"}), ok()])
    sleeps = []
    response = provider(lambda r: next(answers), sleep=sleeps.append).generate(req())
    assert response.text == "Two days." and sleeps == [7.0]


def test_timeouts_and_connection_failures_are_normalized():
    def slow(request):
        raise httpx.ReadTimeout("read timed out")

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as t:
        provider(slow, max_attempts=1).generate(req())
    with pytest.raises(LLMError) as d:
        provider(down, max_attempts=1).generate(req())
    assert t.value.code == AIErrorCode.TIMEOUT and d.value.code == AIErrorCode.UNAVAILABLE


def test_errors_never_carry_the_key_the_prompt_or_the_vendors_text(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return error(
            401, "authentication_error", f"invalid x-api-key {API_KEY} for prompt Shipping?"
        )

    with pytest.raises(LLMError) as exc:
        provider(handler).generate(req())
    shown = str(exc.value) + repr(exc.value) + caplog.text
    assert API_KEY not in shown and "Shipping?" not in shown and "invalid x-api-key" not in shown


def test_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(LLMError):
        provider(handler, max_attempts=1).generate(req())
    assert calls == ["https://api.anthropic.com/v1/messages"]


# --- streaming ------------------------------------------------------------------------------------------------


def sse(*events: dict) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def delta(text):
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }


def test_stream_yields_text_fragments_and_stops_at_message_stop():
    events = [
        {"type": "message_start", "message": {"id": "msg_1"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        delta("Two "),
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "x"},
        },
        delta("days."),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
        delta("never reached"),
    ]
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return sse(*events)

    assert "".join(provider(handler).generate_stream(req())) == "Two days."
    assert seen["body"]["stream"] is True


def test_a_mid_stream_error_event_is_normalized_not_swallowed():
    events = [
        delta("Part"),
        {"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
    ]
    out = []
    with pytest.raises(LLMError) as exc:
        for piece in provider(lambda r: sse(*events)).generate_stream(req()):
            out.append(piece)
    assert out == ["Part"] and exc.value.code == AIErrorCode.UNAVAILABLE


def test_a_refusal_during_streaming_is_reported():
    events = [delta("I"), {"type": "message_delta", "delta": {"stop_reason": "refusal"}}]
    with pytest.raises(LLMError) as exc:
        list(provider(lambda r: sse(*events)).generate_stream(req()))
    assert exc.value.code == AIErrorCode.CONTENT_FILTERED


def test_streaming_http_errors_use_the_same_mapping():
    with pytest.raises(LLMError) as exc:
        list(provider(lambda r: error(401, "authentication_error")).generate_stream(req()))
    assert exc.value.code == AIErrorCode.AUTH


def test_a_garbled_stream_event_is_a_provider_error():
    body = "data: {not json}\n\n"
    with pytest.raises(LLMError) as exc:
        list(provider(lambda r: httpx.Response(200, text=body)).generate_stream(req()))
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR


# --- the agent needs no change ---------------------------------------------------------------------------------


def test_the_chat_agent_runs_unchanged_on_the_anthropic_adapter():
    """The point of the abstraction: the same agent, a different protocol, no branch on the vendor."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return ok([{"type": "text", "text": "Orders ship in two days."}])

    llm = provider(handler, model="claude-test")
    org = uuid.uuid4()
    agent = ChatAgent(
        llm=llm,
        embedder=SpyEmbedder(),
        settings=Settings(
            _env_file=None,
            database_url="sqlite://",
            jwt_secret_key="k" * 40,
            llm_provider="fake",
            llm_max_output_tokens=123,
        ),
        search=StubSearch({org: [make_hit("Orders ship in two days.", score=0.9)]}),
    )
    reply = agent.respond(
        None,  # type: ignore[arg-type]
        organization_id=org,
        business_name="Shop",
        question="How long does shipping take?",
        history=[],
    )
    assert reply.content == "Orders ship in two days."
    assert (reply.prompt_tokens, reply.completion_tokens) == (12, 5)
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["body"]["max_tokens"] == 123
    assert "Shop" in seen["body"]["system"]  # the agent's system rules moved to the system field
    assert seen["body"]["messages"][-1]["role"] == "user"
