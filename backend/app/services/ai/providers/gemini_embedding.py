"""Gemini's native embedding API: a different protocol from the OpenAI-compatible chat endpoint.

Google serves Gemini chat through an OpenAI-compatibility layer (``providers/openai_compatible.py``
handles that, unchanged), but that layer has no embedding model. Embeddings use Gemini's own REST
protocol instead: authentication via an ``x-goog-api-key`` header (not ``Authorization: Bearer``),
a request body shaped as ``{"requests": [{"model", "content": {"parts": [...]}, ...}]}``, and a
response shaped as ``{"embeddings": [{"values": [...]}]}``. Hence its own adapter, registered under
its own ``"gemini"`` adapter key (see ``registry.py``) -- chat for this provider is still the exact
same ``OpenAICompatibleChatProvider`` used by every other vendor on that protocol.

Verified against Google's AI API reference (ai.google.dev/api/embeddings), November 2026: the
``batchEmbedContents`` request/response shapes, the ``model`` field needing a ``models/`` prefix, and
``outputDimensionality`` as a top-level (documented, if deprecated-in-favour-of-a-nested-config)
request field -- chosen over the nested ``embedContentConfig`` form because its field name and
casing are unambiguous in the docs, while the nested form's are not. No vendor-specific error body
shape is documented, so failures classify the same way every other adapter's do: by HTTP status
(``errors.classify_http_status``), via the shared ``_http.Endpoint``.
"""

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from app.services.ai.errors import AIErrorCode, EmbeddingError
from app.services.ai.provider_config import ProviderConfig
from app.services.ai.providers._http import Endpoint as _Endpoint


class GeminiEmbeddingProvider:
    """Calls Gemini's ``batchEmbedContents`` endpoint.

    ``model_id`` (stored on every chunk) is ``namespace:model``; search only compares vectors with
    the same ``model_id``, so vectors from different models or endpoints are never mixed. There is
    no query/document distinction here (Gemini documents no stable prompt convention for
    ``gemini-embedding-2``'s successor to the old ``task_type`` parameter), so both call sites in
    ``services/ai/rag.py`` and ``services/ai/rag/indexing.py`` go through this same ``embed``.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        dimensions: int,
        base_url: str,
        provider_id: str = "gemini",
        namespace: str | None = None,
        batch_size: int = 64,
        timeout: float = 30.0,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
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
            auth_header="x-goog-api-key",
        )

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "GeminiEmbeddingProvider":
        assert config.base_url is not None  # the registry guarantees a resolved base URL
        return cls(
            api_key=config.secret,
            model=config.model,
            dimensions=config.dimensions,
            base_url=config.base_url,
            provider_id=config.provider_id,
            namespace=config.namespace,
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
        model_path = f"models/{self._model}"
        payload: dict[str, Any] = {
            "requests": [
                {
                    "model": model_path,
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": self._dimensions,
                }
                for text in batch
            ]
        }
        response = endpoint.post(f"/{model_path}:batchEmbedContents", payload)
        try:
            items = response.json()["embeddings"]
            vectors = [[float(x) for x in item["values"]] for item in items]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR) from None
        if len(vectors) != len(batch):
            raise endpoint.error(AIErrorCode.PROVIDER_ERROR)
        if any(len(v) != self._dimensions for v in vectors):
            # Not a transient fault: this model/request did not produce the size the database
            # column holds.
            raise EmbeddingError(
                AIErrorCode.MISCONFIGURED,
                f"the embedding model returned vectors of size {len(vectors[0])} but "
                f"{self._dimensions} is required; choose a model that supports "
                f"{self._dimensions} dimensions",
                provider=endpoint.provider_id,
            )
        return vectors
