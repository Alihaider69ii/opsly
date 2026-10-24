"""Anthropic's Messages API: a different protocol from the OpenAI family, so a dedicated adapter.

Verified against https://platform.claude.com/docs/en/api/messages and .../api/errors (October
2026). What differs from the OpenAI-compatible adapter, and is therefore handled here and nowhere
else in the application:

* endpoint ``POST /v1/messages``; the key travels in ``x-api-key`` (not ``Authorization: Bearer``)
  and every request names a protocol version in ``anthropic-version``;
* system instructions are a top-level ``system`` field, not a message; ``max_tokens`` is required;
  temperature is 0 to 1; the conversation must end with a user turn (prefilled assistant turns are
  rejected by current models, and the application never prefills);
* the reply is a list of content blocks (text is only one kind) with a ``stop_reason`` of
  ``end_turn``, ``max_tokens``, ``stop_sequence``, ``tool_use`` (or ``refusal``), and token counts
  as ``usage.input_tokens`` / ``usage.output_tokens``;
* errors are ``{"type": "error", "error": {"type": ..., "message": ...}}`` with HTTP 400, 401,
  402, 403, 404, 413, 429, 500, 504 and 529 (overloaded), and a ``request-id`` header;
* Anthropic has no embeddings API, so this provider is chat-only.

Everything is translated into the vendor-neutral ``AIRequest`` / ``AIResponse`` / ``AIError``; the
agent never sees a content block. Bodies, prompts and keys are never logged.
"""

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from app.services.ai.errors import AIErrorCode, LLMError, classify_http_status
from app.services.ai.provider import AIRequest, AIResponse, AIUsage
from app.services.ai.provider_config import ProviderConfig
from app.services.ai.providers._http import Endpoint, int_or_none, safe_json

ANTHROPIC_VERSION = "2023-06-01"  # the protocol version header Anthropic's docs specify

_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}
_STREAM_ERRORS = {
    "authentication_error": AIErrorCode.AUTH,
    "permission_error": AIErrorCode.AUTH,
    "billing_error": AIErrorCode.AUTH,
    "rate_limit_error": AIErrorCode.RATE_LIMITED,
    "timeout_error": AIErrorCode.TIMEOUT,
    "overloaded_error": AIErrorCode.UNAVAILABLE,
    "api_error": AIErrorCode.UNAVAILABLE,
    "invalid_request_error": AIErrorCode.INVALID_REQUEST,
    "not_found_error": AIErrorCode.MODEL_UNAVAILABLE,
}


class AnthropicMessagesProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        provider_id: str = "anthropic",
        timeout: float = 30.0,
        max_attempts: int = 2,
        temperature: float | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider_id = provider_id
        self._model = model
        self._temperature = temperature
        self._endpoint = Endpoint(
            provider_id=provider_id,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            sleep=sleep,
            max_attempts=max_attempts,
            backoff_base=0.5,
            backoff_cap=4.0,
            error_cls=LLMError,
            auth_header="x-api-key",
            extra_headers={"anthropic-version": ANTHROPIC_VERSION},
        )

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "AnthropicMessagesProvider":
        assert config.base_url is not None  # the registry guarantees a resolved base URL
        return cls(
            api_key=config.secret,
            model=config.model,
            base_url=config.base_url,
            provider_id=config.provider_id,
            timeout=config.timeout,
            temperature=config.temperature,
        )

    @property
    def name(self) -> str:
        return self._provider_id

    @property
    def model(self) -> str:
        return self._model

    # -- request --------------------------------------------------------------------------------

    def _payload(self, request: AIRequest, *, stream: bool = False) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        if not turns or turns[-1]["role"] != "user":
            # Known before sending: current models reject a conversation that does not end with a
            # user turn, so fail locally with a normalized error instead of burning a request.
            raise self._endpoint.error(AIErrorCode.INVALID_REQUEST)
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "max_tokens": request.max_tokens,
            "messages": turns,
        }
        if system:
            payload["system"] = system
        temperature = request.temperature if request.temperature is not None else self._temperature
        if temperature is not None:
            payload["temperature"] = min(max(float(temperature), 0.0), 1.0)  # Anthropic: 0 to 1
        if stream:
            payload["stream"] = True
        return payload

    def generate(self, request: AIRequest) -> AIResponse:
        started = time.monotonic()
        response = self._endpoint.post("/v1/messages", self._payload(request))
        return self._parse(response, request, int((time.monotonic() - started) * 1000))

    # -- response -------------------------------------------------------------------------------

    def _parse(self, response: httpx.Response, request: AIRequest, latency_ms: int) -> AIResponse:
        endpoint = self._endpoint
        try:
            body = response.json()
            blocks = body["content"]
            text = "".join(
                b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
            stop = body.get("stop_reason")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR) from None
        if stop == "refusal":
            raise endpoint.error(AIErrorCode.CONTENT_FILTERED)
        if not text.strip():  # no text block, or an empty one (for example only a tool_use block)
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR)
        usage_raw = body.get("usage")
        usage = None
        if isinstance(usage_raw, dict):
            usage = AIUsage.of(
                int_or_none(usage_raw.get("input_tokens")),
                int_or_none(usage_raw.get("output_tokens")),
            )
        request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
        return AIResponse(
            text=text,
            provider=self._provider_id,
            model=str(body.get("model") or request.model or self._model),
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=_STOP_REASONS.get(stop, stop) if isinstance(stop, str) else None,
            request_id=request_id[:128] if request_id else None,
        )

    # -- streaming ------------------------------------------------------------------------------

    def generate_stream(self, request: AIRequest) -> Iterator[str]:
        endpoint = self._endpoint
        try:
            with endpoint.client.stream(
                "POST", "/v1/messages", json=self._payload(request, stream=True)
            ) as response:
                if response.status_code >= 300:
                    response.read()
                    raise endpoint.error(
                        classify_http_status(response.status_code, safe_json(response)),
                        response.status_code,
                    )
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                        kind = event["type"]
                    except (ValueError, KeyError, TypeError):
                        raise endpoint.error(AIErrorCode.PROVIDER_ERROR) from None
                    if kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        text = delta.get("text")
                        if delta.get("type") == "text_delta" and isinstance(text, str) and text:
                            yield text
                    elif kind == "message_delta":
                        if (event.get("delta") or {}).get("stop_reason") == "refusal":
                            raise endpoint.error(AIErrorCode.CONTENT_FILTERED)
                    elif kind == "error":
                        # An error after a 200 response: its type says what it was.
                        error_type = str((event.get("error") or {}).get("type") or "")
                        raise endpoint.error(
                            _STREAM_ERRORS.get(error_type, AIErrorCode.PROVIDER_ERROR)
                        )
                    elif kind == "message_stop":
                        return
        except httpx.TimeoutException:
            raise endpoint.error(AIErrorCode.TIMEOUT) from None
        except httpx.HTTPError:
            raise endpoint.error(AIErrorCode.UNAVAILABLE) from None
