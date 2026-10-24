"""Deterministic offline providers for local development and tests. NOT real models.

``FakeLLMProvider`` answers by quoting the first knowledge block found in the prompt, which is
enough to exercise the whole chat pipeline (retrieval, prompt building, storage, widget) without an
API key. ``FakeEmbeddingProvider`` produces hashed bag-of-words vectors: texts that share words get
positive cosine similarity, which is enough to exercise retrieval but is not semantic. Both are
refused when ENVIRONMENT=production (the catalog marks the ``fake`` provider non-production).
"""

import hashlib
import math
import re
from collections.abc import Iterator, Sequence

from app.db.models.knowledge import EMBEDDING_DIM
from app.services.ai.provider import AIRequest, AIResponse, ChatMessage
from app.services.ai.provider_config import ProviderConfig

_SOURCE_CONTENT = re.compile(
    r"^Content:\n(.+?)(?:\n\nSOURCE \d+|\n</business_information>|\Z)", re.MULTILINE | re.DOTALL
)
NO_INFO = "I don't have that information."


class FakeLLMProvider:
    @classmethod
    def from_config(cls, config: ProviderConfig) -> "FakeLLMProvider":
        return cls()

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-llm-v1"

    def generate(self, request: AIRequest) -> AIResponse:
        text = self._answer(request.messages)
        return AIResponse(
            text=text[: request.max_tokens * 4],
            provider=self.name,
            model=request.model or self.model,
            finish_reason="stop",
        )

    def generate_stream(self, request: AIRequest) -> Iterator[str]:
        for word in self._answer(request.messages).split(" "):
            yield word + " "

    @staticmethod
    def _answer(messages: Sequence[ChatMessage]) -> str:
        for message in messages:
            match = _SOURCE_CONTENT.search(message.content)
            if message.role == "system" and match:
                first = " ".join(match.group(1).split())
                return f"Here is what I found: {first[:400]}"
        return NO_INFO


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = EMBEDDING_DIM) -> None:
        self._dimensions = dimensions

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "FakeEmbeddingProvider":
        return cls(config.dimensions)

    @property
    def model_id(self) -> str:
        return "fake:hash-v1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in re.findall(r"\w+", text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]
