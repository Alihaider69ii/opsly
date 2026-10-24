"""LLM capability: the vendor-neutral request/response model and the ``LLMProvider`` interface.

The chat agent builds an ``AIRequest`` and reads an ``AIResponse``; it never sees a vendor SDK
object, an HTTP response or a vendor-specific error. Supporting another vendor (Anthropic, a
self-hosted model, ...) means writing one adapter class that translates ``AIRequest`` into that
vendor's call and back, registering it (see ``registry.py``) and adding a row to the provider
catalog (``catalog.py``). Nothing else in the application changes.

Errors are ``AIError`` subclasses with normalized codes (``errors.py``) and never carry prompts,
response bodies or credentials.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.services.ai.errors import AIError, AIErrorCode, EmbeddingError, LLMError

__all__ = [
    "AIError",
    "AIErrorCode",
    "AIRequest",
    "AIResponse",
    "AIUsage",
    "ChatMessage",
    "EmbeddingError",
    "LLMError",
    "LLMProvider",
]

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class AIRequest:
    """What the application asks of a text-generation model.

    ``model`` overrides the provider's configured default for this one request (None = default).
    ``temperature`` None means "the provider's default". ``metadata`` is for tracing and logging
    (for example a conversation id); adapters must not forward it to a vendor.
    """

    messages: Sequence[ChatMessage]
    max_tokens: int
    model: str | None = None
    temperature: float | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AIUsage:
    """Token accounting. Every field is optional because vendors report different things."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def of(
        cls,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> "AIUsage":
        """Build usage, deriving the total when the vendor only gave the two parts."""
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        return cls(prompt_tokens, completion_tokens, total_tokens)


@dataclass(frozen=True)
class AIResponse:
    """One complete model reply, normalized across vendors."""

    text: str
    provider: str  # catalog provider id, e.g. "openai", "openrouter"
    model: str  # the model that actually served the request, as the vendor reports it
    usage: AIUsage | None = None
    latency_ms: int = 0
    finish_reason: str | None = None  # e.g. "stop", "length"
    request_id: str | None = None  # the vendor's request id, for support tickets


class LLMProvider(Protocol):
    @property
    def name(self) -> str:
        """The catalog provider id this instance serves."""
        ...

    @property
    def model(self) -> str:
        """The configured default model."""
        ...

    def generate(self, request: AIRequest) -> AIResponse:
        """Return one complete reply. Raises ``LLMError`` on any failure."""
        ...

    def generate_stream(self, request: AIRequest) -> Iterator[str]:
        """Yield the reply as text fragments as they are produced. Raises ``LLMError``."""
        ...
