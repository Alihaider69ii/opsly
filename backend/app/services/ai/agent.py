"""The website chat agent: one customer question in, one grounded reply out.

    normalize -> retrieve (organization-scoped) -> nothing relevant? fixed fallback, NO LLM call
              -> build structured prompt -> LLM -> validate -> reply

Grounding is enforced twice: by the prompt, and by code. When retrieval finds nothing above the
similarity threshold the model is never called, so it cannot invent business facts for a question
the knowledge base cannot answer.
"""

import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.services.ai import prompts
from app.services.ai.embeddings import EmbeddingError, EmbeddingProvider
from app.services.ai.provider import AIRequest, ChatMessage, LLMError, LLMProvider
from app.services.ai.rag import Searcher, normalize_question, retrieve
from app.services.rag.search import search_knowledge

logger = logging.getLogger(__name__)

HISTORY_MESSAGE_CHARS = 1500
HISTORY_TOTAL_CHARS = 8000
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_PREFIX_RE = re.compile(r"^\s*(assistant|ai|bot)\s*:\s*", re.IGNORECASE)


class AssistantUnavailable(Exception):
    """The reply could not be produced (provider, embedding or retrieval failure).

    ``reason`` is a short internal label for logs; callers show users a generic message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AgentReply:
    content: str
    grounded: bool  # True when the answer was generated from retrieved business knowledge
    sources: list[dict[str, Any]] = field(default_factory=list)  # {url, title, score}
    model: str | None = None
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def audit(self) -> dict[str, Any]:
        """What is stored alongside the assistant message (never sent to the public API)."""
        return {
            "grounded": self.grounded,
            "sources": self.sources,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "prompt_version": prompts.PROMPT_VERSION,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def prepare_history(
    messages: Sequence[ChatMessage],
    limit: int,
    *,
    max_total_chars: int = HISTORY_TOTAL_CHARS,
    max_message_chars: int = HISTORY_MESSAGE_CHARS,
) -> list[ChatMessage]:
    """The recent turns to show the model: oldest first, at most ``limit`` messages and a bounded
    number of characters. Consecutive messages from the same side collapse to the newest (an
    earlier customer message that never got a reply, such as one sent during an outage, is
    superseded by the retry), and the history never starts with an assistant message.

    ``max_total_chars``/``max_message_chars`` default to the website chat's budget; a caller with
    a smaller bound (e.g. WhatsApp, see ``app/services/ai/whatsapp_agent.py``) passes its own.
    """
    if limit <= 0:
        return []
    collapsed: list[ChatMessage] = []
    for message in messages:
        if message.role not in ("user", "assistant"):
            continue
        if collapsed and collapsed[-1].role == message.role:
            collapsed[-1] = message
        else:
            collapsed.append(message)
    recent = collapsed[-limit:]
    while recent and recent[0].role == "assistant":
        recent = recent[1:]

    trimmed: list[ChatMessage] = []
    total = 0
    for message in reversed(recent):  # keep the newest when the character budget runs out
        text = message.content[:max_message_chars]
        if total + len(text) > max_total_chars:
            break
        trimmed.append(ChatMessage(message.role, text))
        total += len(text)
    trimmed.reverse()
    while trimmed and trimmed[0].role == "assistant":
        trimmed = trimmed[1:]
    return trimmed


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("\n"))
    if boundary > max_chars // 2:
        return cut[: boundary + 1].rstrip()
    return cut.rsplit(" ", 1)[0].rstrip() + "..."


class ChatAgent:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        embedder: EmbeddingProvider,
        settings: Settings,
        search: Searcher = search_knowledge,
    ) -> None:
        self._llm = llm
        self._embedder = embedder
        self._settings = settings
        self._search = search

    def validate_reply(self, text: str) -> str | None:
        """Clean the model's output. Returns None when it must not be shown (empty, or it leaks the
        instructions), in which case the caller uses the fallback message."""
        text = _CONTROL_RE.sub("", text).strip()
        text = _ROLE_PREFIX_RE.sub("", text)
        lowered = text.lower()
        if not text:
            return None
        if (
            prompts.PROMPT_CANARY in lowered
            or f"<{prompts.KNOWLEDGE_TAG}" in lowered
            or "business information\n" in lowered
        ):
            return None
        return _truncate(text, self._settings.chat_max_response_chars)

    def respond(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        business_name: str,
        question: str,
        history: Sequence[ChatMessage],
        conversation_id: uuid.UUID | None = None,
    ) -> AgentReply:
        started = time.monotonic()
        settings = self._settings
        question = normalize_question(question, settings.chat_max_message_chars)
        recent = prepare_history(history, settings.chat_history_limit)
        # A retry after an outage leaves the same customer message unanswered at the end of the
        # history; the current question supersedes it, so the model must not see it twice.
        if recent and recent[-1].role == "user" and _same_text(recent[-1].content, question):
            recent = recent[:-1]

        try:
            retrieval = retrieve(
                db,
                organization_id=organization_id,
                question=question,
                history=recent,
                embedder=self._embedder,
                settings=settings,
                search=self._search,
                conversation_id=conversation_id,
            )
        except EmbeddingError as exc:
            log_event(
                logger, "agent.failed", level=logging.WARNING, stage="embedding", code=exc.code
            )
            raise AssistantUnavailable("embedding") from None
        except SQLAlchemyError:
            logger.exception("retrieval failed")
            raise AssistantUnavailable("retrieval") from None

        if not retrieval.found:
            log_event(logger, "agent.no_context", conversation=conversation_id)
            return self._fallback(started)

        messages = prompts.build_messages(
            business_name=business_name,
            history=recent,
            sources=retrieval.sources,
            question=question,
        )
        log_event(
            logger,
            "llm.started",
            conversation=conversation_id,
            provider=self._llm.name,
            model=self._llm.model,
            messages=len(messages),
            sources=len(retrieval.sources),
        )
        request = AIRequest(
            messages=messages,
            max_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
            metadata={"conversation": str(conversation_id)} if conversation_id else {},
        )
        try:
            response = self._llm.generate(request)
        except LLMError as exc:
            log_event(
                logger,
                "llm.failed",
                level=logging.WARNING,
                conversation=conversation_id,
                provider=self._llm.name,
                code=exc.code,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            raise AssistantUnavailable(f"llm_{exc.code}") from None

        latency = int((time.monotonic() - started) * 1000)
        log_event(
            logger,
            "llm.completed",
            conversation=conversation_id,
            provider=self._llm.name,
            model=response.model,
            elapsed_ms=latency,
            finish=response.finish_reason,
            prompt_tokens=response.usage.prompt_tokens if response.usage else None,
            completion_tokens=response.usage.completion_tokens if response.usage else None,
            request_id=response.request_id,
        )

        reply = self.validate_reply(response.text)
        if reply is None:
            log_event(
                logger, "agent.reply_rejected", level=logging.WARNING, conversation=conversation_id
            )
            return self._fallback(started)
        return AgentReply(
            content=reply,
            grounded=True,
            sources=[
                {"url": s.url, "title": s.title, "score": round(s.score, 4)}
                for s in retrieval.sources
            ],
            model=response.model,
            latency_ms=latency,
            prompt_tokens=response.usage.prompt_tokens if response.usage else None,
            completion_tokens=response.usage.completion_tokens if response.usage else None,
        )

    def _fallback(self, started: float) -> AgentReply:
        return AgentReply(
            content=self._settings.chat_fallback_message,
            grounded=False,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
