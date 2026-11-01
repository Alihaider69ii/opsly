"""WhatsApp (Phase 8) multi-turn, RAG-grounded conversational AI layer.

Mirrors ``app.services.ai.agent.ChatAgent``'s pipeline (normalize -> retrieve -> prompt -> LLM ->
validate -> reply), reusing the SAME retrieval (``app.services.ai.rag.retrieve``), embeddings and
``LLMProvider`` abstraction -- no second RAG system, no second provider integration. It differs
from ``ChatAgent`` in three ways WhatsApp specifically needs:

1. A deterministic CONFIDENCE bucket (never a model-reported number) derived from retrieval
   signal, returned to the caller so it can decide between sending, falling back, or triggering
   human handoff -- the AI itself makes none of those decisions, see
   ``services/messaging/whatsapp/handoff.py``.
2. A greeting/casual-message path: a short, deterministically-classified greeting or closing
   still reaches the model (with no knowledge block) instead of tripping the "no relevant
   knowledge" fallback the way a real, unanswerable product question correctly would.
3. An optional CUSTOMER CONTEXT block (read-only Shopify facts), built by
   ``services/messaging/whatsapp/customer_context.py``.

AI never decides policy -- it only returns a ``WhatsAppAIReply`` describing what was generated
and how confident/grounded it was. See ``services/messaging/whatsapp/autoreply.py`` for where
that becomes a send/fallback/handoff decision, and ``services/messaging/whatsapp/send.py`` for
the one and only place a messaging provider is actually called.
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
from app.db.models import WhatsAppAIConfidence
from app.services.ai import prompts
from app.services.ai.agent import prepare_history
from app.services.ai.embeddings import EmbeddingError, EmbeddingProvider
from app.services.ai.provider import AIRequest, ChatMessage, LLMError, LLMProvider
from app.services.ai.rag import ContextSource, Searcher, normalize_question, retrieve
from app.services.rag.search import search_knowledge

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 1000  # WhatsApp replies stay short; mirrors the Phase 6 constant it replaces
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_PREFIX_RE = re.compile(r"^\s*(assistant|ai|bot)\s*:\s*", re.IGNORECASE)

# A deterministic allowlist of greetings/closings in English and Hindi/Hinglish that should
# reach the model even when retrieval finds nothing -- see module docstring point 2.
# Anything longer, or not matched here, is treated as a real question and gets the normal
# grounding gate.
_GREETING_RE = re.compile(
    r"^\s*("
    r"(hi|hello|hey|hiya)(\s+(there|guys|everyone|folks))?|"
    r"good\s+(morning|afternoon|evening)|"
    r"thanks|thank\s*you|thx|ty|"
    r"ok|okay|k|cool|great|good|nice|"
    r"bye|goodbye|see\s+you|"
    r"yes|yeah|yep|no|nope|"
    r"namaste|namaskar|pranam|pranaam|salaam|salam|"
    r"kaise\s+ho|kya\s+haal\s+hai|"
    r"shukriya|dhanyawad|dhanyavad|alvida|"
    r"नमस्ते|नमस्कार|प्रणाम|प्रनाम|सलाम|"
    r"कैसे\s+हो|क्या\s+हाल\s+है|"
    r"शुक्रिया|धन्यवाद|अलविदा|हैलो|हाय"
    r")[\s!.,?]*$",
    re.IGNORECASE | re.UNICODE,
)

# Deterministic detector supporting English and common Indian/D2C Hindi/Hinglish patterns.
# A false negative only means the usual confidence-based handoff path still applies;
# a false positive only means an eager handoff -- neither is unsafe.
_HUMAN_REQUEST_RE = re.compile(
    r"("
    r"\b(human|agent|representative|manager|real\s+person|customer\s+(care|service|support)|support\s+(team|agent)?)\b|"
    r"\b(speak|talk)\s+to\s+(a\s+)?(someone|person|human|agent|representative|manager|executive)\b|"
    r"\b(baat\s*(karni|karna|karwao|karvaao|karein|karado|kara\s*do|karo))\b|"
    r"\b(agent|human|person|insaan|insan|aadmi|executive|support|customer\s*care)\s*(chahiye|chahie)\b|"
    r"\b(insaan|insan|aadmi|vyakti)\b|"
    r"(एजेंट|इंसान|आदमी|कस्टमर\s*केयर|सपोर्ट)\s*(से)?\s*बात|"
    r"(एजेंट|सपोर्ट|कस्टमर\s*केयर)\s*चाहिए|"
    r"बात\s*(करनी|करना|करवाओ|करवा\s*दो)|"
    r"\b(एजेंट|इंसान|आदमी)\b"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CONFIDENCE_RANK = {
    WhatsAppAIConfidence.NONE: 0,
    WhatsAppAIConfidence.LOW: 1,
    WhatsAppAIConfidence.MEDIUM: 2,
    WhatsAppAIConfidence.HIGH: 3,
}


def requests_human(text: str) -> bool:
    """A deterministic detector supporting English and Hindi/Hinglish patterns."""
    return bool(_HUMAN_REQUEST_RE.search(text))


def is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text))


def meets_minimum(confidence: WhatsAppAIConfidence, minimum: WhatsAppAIConfidence) -> bool:
    return _CONFIDENCE_RANK[confidence] >= _CONFIDENCE_RANK[minimum]


def classify_confidence(
    sources: Sequence[ContextSource], *, similarity_threshold: float
) -> WhatsAppAIConfidence:
    """Deterministic, from retrieval signal only -- never a model-reported score (Phase 8 brief
    section 12). ``sources`` are already filtered to ``score >= similarity_threshold`` by
    ``retrieve``, so ``margin`` is always >= 0 when ``sources`` is non-empty."""
    if not sources:
        return WhatsAppAIConfidence.NONE
    margin = sources[0].score - similarity_threshold
    if margin >= 0.35 and len(sources) >= 2:
        return WhatsAppAIConfidence.HIGH
    if margin >= 0.12:
        return WhatsAppAIConfidence.MEDIUM
    return WhatsAppAIConfidence.LOW


class WhatsAppAIUnavailable(Exception):
    """Nothing could be generated at all (provider/embedding/retrieval failure). Distinct from a
    successful-but-ungrounded reply, which is a normal ``WhatsAppAIReply`` with
    ``grounded=False`` -- this is reserved for "nothing was generated"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WhatsAppAIReply:
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
        """Non-sensitive audit payload stored in ``WhatsAppMessage.safe_metadata``."""
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


def validate_reply(text: str, max_chars: int) -> str | None:
    """Clean the model's output. ``None`` means it must not be shown (empty, or it leaks the
    instructions/tags), in which case the caller treats it like any other generation failure."""
    text = _CONTROL_RE.sub("", text).strip()
    text = _ROLE_PREFIX_RE.sub("", text)
    lowered = text.lower()
    if not text:
        return None
    if (
        prompts.PROMPT_CANARY in lowered
        or f"<{prompts.KNOWLEDGE_TAG}" in lowered
        or f"<{prompts.CUSTOMER_CONTEXT_TAG}" in lowered
    ):
        return None
    return text[:max_chars]


class WhatsAppConversationAgent:
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
    ) -> WhatsAppAIReply:
        started = time.monotonic()
        settings = self._settings
        question = normalize_question(question, settings.chat_max_message_chars)
        recent = prepare_history(
            history,
            settings.whatsapp_ai_history_max_messages,
            max_total_chars=settings.whatsapp_ai_history_max_chars,
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
                logger,
                "whatsapp_ai.failed",
                level=logging.WARNING,
                stage="embedding",
                code=exc.code,
            )
            raise WhatsAppAIUnavailable("embedding") from None
        except SQLAlchemyError:
            logger.exception("whatsapp ai retrieval failed")
            raise WhatsAppAIUnavailable("retrieval") from None

        greeting = not retrieval.found and is_greeting(question)
        # Grounding required (the default, and the only option when the organization hasn't
        # explicitly relaxed it) means no LLM call at all without retrieved knowledge -- the
        # same hard guarantee the website chat agent makes. An organization that has explicitly
        # turned grounding off still gets the system prompt's "never state a business fact you
        # cannot see" rule, but may let the model answer a general/conversational question with
        # no knowledge block at all (same no-knowledge-block mechanics as a greeting).
        if not retrieval.found and not greeting and grounding_required:
            log_event(logger, "whatsapp_ai.no_context", conversation=conversation_id)
            return WhatsAppAIReply(
                content="",
                grounded=False,
                confidence=WhatsAppAIConfidence.NONE,
                is_greeting=False,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        confidence = classify_confidence(
            retrieval.sources, similarity_threshold=settings.rag_similarity_threshold
        )
        messages = prompts.build_whatsapp_messages(
            business_name=business_name,
            history=recent,
            sources=retrieval.sources,
            customer_facts=customer_facts,
            question=question,
        )
        log_event(
            logger,
            "whatsapp_ai.llm_started",
            conversation=conversation_id,
            provider=self._llm.name,
            model=self._llm.model,
            messages=len(messages),
            sources=len(retrieval.sources),
            greeting=greeting,
        )
        request = AIRequest(
            messages=messages,
            max_tokens=min(300, settings.llm_max_output_tokens),
            temperature=settings.llm_temperature,
            metadata={"conversation": str(conversation_id)} if conversation_id else {},
        )
        try:
            response = self._llm.generate(request)
        except LLMError as exc:
            log_event(
                logger,
                "whatsapp_ai.llm_failed",
                level=logging.WARNING,
                conversation=conversation_id,
                provider=self._llm.name,
                code=exc.code,
            )
            raise WhatsAppAIUnavailable(f"llm_{exc.code}") from None

        latency = int((time.monotonic() - started) * 1000)
        reply_text = validate_reply(response.text, MAX_REPLY_CHARS)
        if reply_text is None:
            log_event(
                logger,
                "whatsapp_ai.reply_rejected",
                level=logging.WARNING,
                conversation=conversation_id,
            )
            return WhatsAppAIReply(
                content="",
                grounded=False,
                confidence=WhatsAppAIConfidence.NONE,
                is_greeting=greeting,
                latency_ms=latency,
            )
        return WhatsAppAIReply(
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
