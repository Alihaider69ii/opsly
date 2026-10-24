"""Retrieval for the chat agent: question -> query embedding -> organization-scoped vector search ->
relevance filtering -> a bounded, attributed context block.

Reuses Phase 2's ``search_knowledge`` (always organization-scoped, exact cosine search) and its
``EmbeddingProvider``; nothing here queries the knowledge base any other way.
"""

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.services.ai.embeddings import EmbeddingProvider
from app.services.ai.provider import ChatMessage
from app.services.rag.search import SearchHit, search_knowledge

logger = logging.getLogger(__name__)

Searcher = Callable[..., list[SearchHit]]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_FOLLOW_UP_WORDS = 8  # a question this short probably leans on the previous turn ("that one?")
_QUERY_CONTEXT_CHARS = 300
_MIN_USEFUL_REMAINDER = 300


@dataclass(frozen=True)
class ContextSource:
    number: int
    title: str
    url: str
    content: str
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[SearchHit]
    sources: list[ContextSource]  # empty when nothing relevant was found

    @property
    def found(self) -> bool:
        return bool(self.sources)


def normalize_question(text: str, max_chars: int) -> str:
    """Strip control characters, collapse whitespace, cap the length."""
    return _WS_RE.sub(" ", _CONTROL_RE.sub("", text)).strip()[:max_chars]


def build_retrieval_query(question: str, history: Sequence[ChatMessage]) -> str:
    """The text that is embedded. A short follow-up ("What's the price of that one?") is useless on
    its own, so it is searched together with the previous customer message and the start of the
    previous reply. Long, self-contained questions are searched as written."""
    if len(question.split()) > _FOLLOW_UP_WORDS or not history:
        return question
    last_user = next((m.content for m in reversed(history) if m.role == "user"), "")
    last_reply = next((m.content for m in reversed(history) if m.role == "assistant"), "")
    parts = [last_user[:_QUERY_CONTEXT_CHARS], last_reply[:_QUERY_CONTEXT_CHARS], question]
    return "\n".join(p for p in parts if p)


def _dedupe_key(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def build_context(hits: Sequence[SearchHit], max_chars: int) -> list[ContextSource]:
    """Numbered sources, best match first, attributed to their page, within ``max_chars`` in total.

    Near-duplicates (the same text, or text contained in an already-included chunk) are skipped so
    the budget is spent on distinct information. The first source is always kept (shortened if
    needed); later ones are included whole or shortened if enough room remains, otherwise dropped.
    """
    sources: list[ContextSource] = []
    seen: list[str] = []
    used = 0
    for hit in hits:
        key = _dedupe_key(hit.content)
        if not key or any(key in earlier for earlier in seen):
            continue
        title = (hit.title or "(untitled)").strip()
        overhead = len(f"SOURCE {len(sources) + 1}\nTitle: {title}\nURL: {hit.url}\nContent:\n\n\n")
        room = max_chars - used - overhead
        content = hit.content.strip()
        if sources and room < _MIN_USEFUL_REMAINDER:
            break
        if len(content) > room:
            if room <= 0:
                break
            cut = content[:room].rsplit(" ", 1)[0] or content[:room]
            content = cut.rstrip() + " ..."
        sources.append(ContextSource(len(sources) + 1, title, hit.url, content, hit.score))
        seen.append(key)
        used += overhead + len(content)
    return sources


def retrieve(
    db: Session,
    *,
    organization_id: uuid.UUID,
    question: str,
    history: Sequence[ChatMessage],
    embedder: EmbeddingProvider,
    settings: Settings,
    search: Searcher = search_knowledge,
    conversation_id: uuid.UUID | None = None,
) -> RetrievalResult:
    """Embed only the current query, search this organization's knowledge, keep what is relevant."""
    query = build_retrieval_query(question, history)
    (query_embedding,) = embedder.embed([query])
    hits = search(
        db,
        organization_id=organization_id,
        query_embedding=query_embedding,
        embedding_model=embedder.model_id,
        top_k=settings.rag_top_k,
        min_score=settings.rag_similarity_threshold,
    )
    sources = build_context(hits, settings.rag_max_context_chars)
    log_event(
        logger,
        "rag.retrieved",
        conversation=conversation_id,
        organization=organization_id,
        hits=len(hits),
        used=len(sources),
        top_score=f"{hits[0].score:.3f}" if hits else "none",
    )
    return RetrievalResult(list(hits), sources)
