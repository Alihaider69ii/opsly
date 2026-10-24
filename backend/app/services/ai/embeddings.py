"""Embedding capability: the ``EmbeddingProvider`` interface.

The knowledge base only ever talks to ``EmbeddingProvider``. A provider turns texts into vectors
and names the model that produced them (``model_id``); the id is stored on every chunk so search
never compares vectors from different models. It is independent of the LLM provider: answers can
come from one vendor while embeddings come from another.

Concrete adapters live in ``providers/`` and are built by the registry (``registry.py``) from
``EMBEDDING_*`` settings. Errors are ``EmbeddingError`` (an ``AIError`` with a normalized code) and
never carry response bodies, request payloads or credentials.
"""

from collections.abc import Sequence
from typing import Protocol

from app.services.ai.errors import EmbeddingError

__all__ = ["EmbeddingError", "EmbeddingProvider", "embedding_input"]


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str:
        """Identifies the model and endpoint that produced a vector (stored on every chunk)."""
        ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in order. Texts must be non-empty."""
        ...


def embedding_input(title: str | None, content: str) -> str:
    """The text that is actually embedded for a chunk: the page title gives each chunk the context
    a bare 'Yes, within 30 days.' lacks. The stored chunk content stays untouched."""
    return f"{title}\n\n{content}" if title else content
