"""The OpenAI-compatible adapter family: chat completions and embeddings over plain HTTPS.

One implementation serves OpenAI itself and every vendor or gateway that speaks the same protocol
(OpenRouter, Groq, Together, DeepSeek, Mistral, Gemini's compatible endpoint, Ollama, vLLM, ...).
Differences between them live in configuration (``ProviderSpec`` and ``ProviderConfig``: base URL,
whether the token limit is called ``max_tokens`` or ``max_completion_tokens``, whether embeddings
accept ``dimensions``), never in ``if vendor == ...`` branches.

Failures are normalized to ``AIError`` codes. Vendor response bodies are used only to classify an
error and are never kept, logged or shown; neither are prompts or API keys.
"""

import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx

from app.services.ai.errors import (
    AIErrorCode,
    EmbeddingError,
    LLMError,
    classify_http_status,
)
from app.services.ai.provider import AIRequest, AIResponse, AIUsage
from app.services.ai.provider_config import ProviderConfig
from app.services.ai.providers._http import Endpoint as _Endpoint
from app.services.ai.providers._http import int_or_none as _int_or_none
from app.services.ai.providers._http import safe_json as _safe_json

logger = logging.getLogger(__name__)


class OpenAICompatibleChatProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        provider_id: str = "openai",
        timeout: float = 30.0,
        max_attempts: int = 2,
        temperature: float | None = None,
        max_tokens_field: str = "max_tokens",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider_id = provider_id
        self._model = model
        self._temperature = temperature
        self._max_tokens_field = max_tokens_field
        self._endpoint = _Endpoint(
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
        )

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "OpenAICompatibleChatProvider":
        assert config.base_url is not None  # the registry guarantees a resolved base URL
        return cls(
            api_key=config.secret,
            model=config.model,
            base_url=config.base_url,
            provider_id=config.provider_id,
            timeout=config.timeout,
            temperature=config.temperature,
            max_tokens_field=config.max_tokens_field,
        )

    @property
    def name(self) -> str:
        return self._provider_id

    @property
    def model(self) -> str:
        return self._model

    def _payload(self, request: AIRequest, *, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            self._max_tokens_field: request.max_tokens,
        }
        temperature = request.temperature if request.temperature is not None else self._temperature
        if temperature is not None:
            payload["temperature"] = temperature
        if stream:
            payload["stream"] = True
        return payload

    def generate(self, request: AIRequest) -> AIResponse:
        started = time.monotonic()
        response = self._endpoint.post("/chat/completions", self._payload(request))
        return self._parse(response, request, int((time.monotonic() - started) * 1000))

    def _parse(self, response: httpx.Response, request: AIRequest, latency_ms: int) -> AIResponse:
        endpoint = self._endpoint
        try:
            body = response.json()
            choice = body["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR) from None
        if finish == "content_filter":
            raise endpoint.error(AIErrorCode.CONTENT_FILTERED)
        if not isinstance(content, str) or not content.strip():
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR)
        usage_raw = body.get("usage")
        usage = None
        if isinstance(usage_raw, dict):
            usage = AIUsage.of(
                _int_or_none(usage_raw.get("prompt_tokens")),
                _int_or_none(usage_raw.get("completion_tokens")),
                _int_or_none(usage_raw.get("total_tokens")),
            )
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        if not request_id and isinstance(body.get("id"), str):
            request_id = body["id"]
        return AIResponse(
            text=content,
            provider=self._provider_id,
            model=str(body.get("model") or request.model or self._model),
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish if isinstance(finish, str) else None,
            request_id=request_id[:128] if request_id else None,
        )

    def generate_stream(self, request: AIRequest) -> Iterator[str]:
        endpoint = self._endpoint
        try:
            with endpoint.client.stream(
                "POST", "/chat/completions", json=self._payload(request, stream=True)
            ) as response:
                if response.status_code >= 300:
                    response.read()
                    raise endpoint.error(
                        classify_http_status(response.status_code, _safe_json(response)),
                        response.status_code,
                    )
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        choice = json.loads(data)["choices"][0]
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise endpoint.error(AIErrorCode.PROVIDER_ERROR) from None
                    if choice.get("finish_reason") == "content_filter":
                        raise endpoint.error(AIErrorCode.CONTENT_FILTERED)
                    delta = (choice.get("delta") or {}).get("content")
                    if isinstance(delta, str) and delta:
                        yield delta
        except httpx.TimeoutException:
            raise endpoint.error(AIErrorCode.TIMEOUT) from None
        except httpx.HTTPError:
            raise endpoint.error(AIErrorCode.UNAVAILABLE) from None


class OpenAICompatibleEmbeddingProvider:
    """Calls an OpenAI-style ``/embeddings`` endpoint.

    ``model_id`` (stored on every chunk) is ``namespace:model``; search only compares vectors with
    the same ``model_id``, so vectors from different models or endpoints are never mixed.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        dimensions: int,
        base_url: str,
        provider_id: str = "openai",
        namespace: str | None = None,
        send_dimensions: bool = False,
        batch_size: int = 64,
        timeout: float = 30.0,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._send_dimensions = send_dimensions
        self._batch_size = batch_size
        self._model_id = f"{namespace or provider_id}:{model}"
        self._endpoint = _Endpoint(
            provider_id=provider_id,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            sleep=sleep,
            max_attempts=max_attempts,
            backoff_base=1.0,
            backoff_cap=8.0,
            error_cls=EmbeddingError,
        )

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "OpenAICompatibleEmbeddingProvider":
        assert config.base_url is not None
        return cls(
            api_key=config.secret,
            model=config.model,
            dimensions=config.dimensions,
            base_url=config.base_url,
            provider_id=config.provider_id,
            namespace=config.namespace,
            send_dimensions=any(
                config.model.startswith(prefix) for prefix in config.spec.embedding_dimension_models
            ),
            batch_size=config.batch_size,
            timeout=config.timeout,
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(list(texts[start : start + self._batch_size])))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        endpoint = self._endpoint
        payload: dict[str, Any] = {
            "model": self._model,
            "input": batch,
            "encoding_format": "float",
        }
        if self._send_dimensions:
            payload["dimensions"] = self._dimensions  # these models can emit any size up to native
        response = endpoint.post("/embeddings", payload)
        try:
            data = response.json()
            items = sorted(data["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR) from None
        if len(vectors) != len(batch):
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR)
        if any(len(v) != self._dimensions for v in vectors):
            # Not a transient fault: this model does not emit the size the database column holds.
            raise EmbeddingError(
                AIErrorCode.MISCONFIGURED,
                f"the embedding model returned vectors of size {len(vectors[0])} but "
                f"{self._dimensions} is required; choose a model that supports "
                f"{self._dimensions} dimensions",
                provider=endpoint.provider_id,
            )
        return vectors
