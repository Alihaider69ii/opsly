"""Phase 9 email AI conversation layer.

Mirrors ``app.services.ai.whatsapp_agent.WhatsAppConversationAgent``'s pipeline (normalize ->
retrieve -> prompt -> LLM -> validate -> reply), reusing the SAME retrieval
(``app.services.ai.rag.retrieve``), embeddings, ``LLMProvider`` abstraction -- no second RAG
system, no second provider integration, per the Phase 9 brief ("Do NOT build a separate email
AI"). It also reuses the WhatsApp module's channel-agnostic detectors and confidence classifier
directly (``requests_human``, ``is_greeting``, ``classify_confidence``, ``meets_minimum``,
``validate_reply``): none of them reference anything WhatsApp-specific, and email's confidence
bucket is the same deterministic, retrieval-signal-derived ``WhatsAppAIConfidence`` enum reused
on ``EmailConnection.ai_min_confidence`` (see ``app/db/models/email.py``). Only the system prompt
and the reply-length budget differ (email tolerates a full, multi-sentence reply; WhatsApp does
not) -- see ``app/services/ai/prompts.py:build_email_messages``.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.db.models import WhatsAppAIConfidence
from app.services.ai import prompts
from app.services.ai.agent import prepare_history
from app.services.ai.embeddings import EmbeddingError, EmbeddingProvider
from app.services.ai.provider import AIRequest, ChatMessage, LLMError, LLMProvider
from app.services.ai.rag import Searcher, normalize_question, retrieve
from app.services.ai.whatsapp_agent import classify_confidence, is_greeting, validate_reply
from app.services.rag.search import search_knowledge

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 4000  # email tolerates a full reply, unlike WhatsApp's MAX_REPLY_CHARS = 1000


class EmailAIUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EmailAIReply:
    content: str
    grounded: bool
    confidence: WhatsAppAIConfidence
    is_greeting: bool
    sources: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def audit(self) -> dict[str, Any]:
        return {
            "ai": {
                "grounded": self.grounded,
                "confidence": self.confidence.value,
                "retrieval_used": not self.is_greeting,
                "sources_count": len(self.sources),
                "sources": self.sources,
                "model": self.model,
                "latency_ms": self.latency_ms,
                "prompt_version": prompts.PROMPT_VERSION,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }
        }


class EmailConversationAgent:
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

    def respond(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        business_name: str,
        question: str,
        history: Sequence[ChatMessage],
        customer_facts: Sequence[str] = (),
        conversation_id: uuid.UUID | None = None,
        grounding_required: bool = True,
    ) -> EmailAIReply:
        started = time.monotonic()
        settings = self._settings
        question = normalize_question(question, settings.chat_max_message_chars)
        recent = prepare_history(
            history,
            settings.email_ai_history_max_messages,
            max_total_chars=settings.email_ai_history_max_chars,
        )

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
                logger, "email_ai.failed", level=logging.WARNING, stage="embedding", code=exc.code
            )
            raise EmailAIUnavailable("embedding") from None
        except SQLAlchemyError:
            logger.exception("email ai retrieval failed")
            raise EmailAIUnavailable("retrieval") from None

        greeting = not retrieval.found and is_greeting(question)
        if not retrieval.found and not greeting and grounding_required:
            log_event(logger, "email_ai.no_context", conversation=conversation_id)
            return EmailAIReply(
                content="",
                grounded=False,
                confidence=WhatsAppAIConfidence.NONE,
                is_greeting=False,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        confidence = classify_confidence(
            retrieval.sources, similarity_threshold=settings.rag_similarity_threshold
        )
        messages = prompts.build_email_messages(
            business_name=business_name,
            history=recent,
            sources=retrieval.sources,
            customer_facts=customer_facts,
            question=question,
        )
        log_event(
            logger,
            "email_ai.llm_started",
            conversation=conversation_id,
            provider=self._llm.name,
            model=self._llm.model,
            messages=len(messages),
            sources=len(retrieval.sources),
            greeting=greeting,
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
                "email_ai.llm_failed",
                level=logging.WARNING,
                conversation=conversation_id,
                provider=self._llm.name,
                code=exc.code,
            )
            raise EmailAIUnavailable(f"llm_{exc.code}") from None

        latency = int((time.monotonic() - started) * 1000)
        reply_text = validate_reply(response.text, MAX_REPLY_CHARS)
        if reply_text is None:
            log_event(
                logger,
                "email_ai.reply_rejected",
                level=logging.WARNING,
                conversation=conversation_id,
            )
            return EmailAIReply(
                content="",
                grounded=False,
                confidence=WhatsAppAIConfidence.NONE,
                is_greeting=greeting,
                latency_ms=latency,
            )
        return EmailAIReply(
            content=reply_text,
            grounded=retrieval.found,
            confidence=confidence,
            is_greeting=greeting,
            sources=[
                {"url": s.url, "title": s.title, "score": round(s.score, 4)}
                for s in retrieval.sources
            ],
            model=response.model,
            latency_ms=latency,
            prompt_tokens=response.usage.prompt_tokens if response.usage else None,
            completion_tokens=response.usage.completion_tokens if response.usage else None,
        )
