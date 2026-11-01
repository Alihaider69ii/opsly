"""Phase 8: inbound WhatsApp -> AI Brain (multi-turn, RAG-grounded) -> outbound WhatsApp.

Gated first by the tenant-level ``WhatsAppConnection.ai_auto_reply_enabled`` switch (default off,
unchanged from Phase 6/7), then by a fixed, deterministic policy order (mirrors the Phase 8
brief's section 19):

    1. AI auto-reply enabled
    2. contact not suppressed
    3. SUPPORT consent opted in
    4. customer-service window open
    5. human handoff not already in effect (``ai_state == AI_ACTIVE``)
    6. explicit "talk to a human" request -> handoff, no generation
    7. per-window AI reply cap not exceeded -> otherwise handoff/silence, no generation
    8. build bounded multi-turn context (history + optional read-only customer context)
    9. retrieve + generate (``app.services.ai.whatsapp_agent``)
    10. grounding + confidence check against this connection's policy
    11. send the real reply, OR a fallback, OR trigger handoff and send its acknowledgement

AI never decides policy: every rule above is evaluated by this module and
``services/messaging/whatsapp/handoff.py`` in plain Python before anything is generated, and the
one and only place a provider is called is ``services/messaging/whatsapp/send.py``, which
re-enforces consent/suppression/window itself regardless of what this module decided (defense in
depth -- this module's checks are a cost-control short-circuit, not the enforcement boundary).
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    Organization,
    SenderType,
    WhatsAppConnection,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
)
from app.services.ai.factory import get_embedding_provider, get_llm_provider
from app.services.ai.provider import ChatMessage
from app.services.ai.whatsapp_agent import (
    WhatsAppAIUnavailable,
    WhatsAppConversationAgent,
    meets_minimum,
    requests_human,
)
from app.services.messaging.errors import MessagingError
from app.services.messaging.provider import MessagingProvider
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import customer_context
from app.services.messaging.whatsapp import handoff as handoff_service
from app.services.messaging.whatsapp import send as send_service
from app.services.messaging.whatsapp import window as window_service
from app.services.rag.search import search_knowledge

logger = logging.getLogger(__name__)

HISTORY_FETCH_FACTOR = 3  # fetch extra rows: prepare_history still collapses/trims after this


def _recent_history(
    db: Session, conversation: WhatsAppConversation, *, limit: int, exclude_message_id: uuid.UUID
) -> list[ChatMessage]:
    """The newest ``limit``-ish text messages in this conversation, oldest first, excluding the
    just-stored current inbound message (the caller passes it separately as the question) and
    any message with no plain-text content (templates) or that never actually reached the
    customer (FAILED)."""
    if limit <= 0:
        return []
    rows = db.scalars(
        select(WhatsAppMessage)
        .where(
            WhatsAppMessage.organization_id == conversation.organization_id,
            WhatsAppMessage.conversation_id == conversation.id,
            WhatsAppMessage.id != exclude_message_id,
            WhatsAppMessage.content.isnot(None),
            WhatsAppMessage.status != WhatsAppMessageStatus.FAILED,
        )
        .order_by(WhatsAppMessage.created_at.desc())
        .limit(limit * HISTORY_FETCH_FACTOR)
    )
    return [
        ChatMessage(
            "user" if m.direction == WhatsAppMessageDirection.INBOUND else "assistant",
            m.content or "",  # the query above already guarantees content is not None
        )
        for m in reversed(list(rows))
    ]


def _dispatch(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: WhatsAppConnection,
    contact: WhatsAppContact,
    conversation: WhatsAppConversation,
    content: str,
    inbound_wamid: str,
    provider: MessagingProvider,
    access_token: str,
    safe_metadata: dict,
    now: datetime,
    sent_label: str,
) -> str:
    """The only place this module calls ``send.send_text``. Counts toward
    ``ai_max_replies_per_window`` regardless of outcome (an attempted send still consumes budget,
    which is what actually bounds AI-driven outbound volume)."""
    if send_service._existing(db, organization_id, f"autoreply:{inbound_wamid}") is None:
        conversation.ai_replies_in_window += 1
        db.commit()
    try:
        row = send_service.send_text(
            db,
            organization_id=organization_id,
            connection=connection,
            contact=contact,
            outbound=send_service.OutboundText(
                content=content, consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key=f"autoreply:{inbound_wamid}",
            provider=provider,
            access_token=access_token,
            safe_metadata=safe_metadata,
            now=now,
            sender_type=SenderType.AI if sent_label == "sent" else SenderType.SYSTEM,
        )
    except MessagingError as exc:
        return f"error:{exc.code.value}"
    if row.status == WhatsAppMessageStatus.FAILED:
        return f"blocked:{row.error_code}"
    return sent_label


def maybe_auto_reply(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: WhatsAppConnection,
    contact: WhatsAppContact,
    conversation: WhatsAppConversation,
    inbound_message_id: uuid.UUID,
    inbound_text: str,
    inbound_wamid: str,
    provider: MessagingProvider,
    access_token: str,
    settings: Settings,
    now: datetime,
) -> str:
    """Returns a short outcome string for audit logging (never the message text)."""
    # 0. Crash recovery check: if an outbound reply row already exists for this inbound wamid
    existing_outbound = send_service._existing(db, organization_id, f"autoreply:{inbound_wamid}")
    if existing_outbound is not None:
        if existing_outbound.status == WhatsAppMessageStatus.QUEUED:
            # Durable AI result already exists; worker died before dispatch completed.
            # Complete the send without re-generating.
            return _dispatch(
                db,
                organization_id=organization_id,
                connection=connection,
                contact=contact,
                conversation=conversation,
                content=existing_outbound.content or "",
                inbound_wamid=inbound_wamid,
                provider=provider,
                access_token=access_token,
                safe_metadata=existing_outbound.safe_metadata or {},
                now=now,
                sent_label="sent",
            )
        if existing_outbound.status == WhatsAppMessageStatus.SENT:
            return "sent"
        if existing_outbound.status == WhatsAppMessageStatus.FAILED:
            return f"blocked:{existing_outbound.error_code}"
        # In-flight (SENDING) -- conservative provider uncertainty, do not retry
        return "in_flight"

    if not connection.ai_auto_reply_enabled:
        return "disabled"
    if consent_service.is_suppressed(db, organization_id=organization_id, contact_id=contact.id):
        return "suppressed"
    if (
        consent_service.get_consent_status(
            db,
            organization_id=organization_id,
            contact_id=contact.id,
            category=WhatsAppConsentCategory.SUPPORT,
        )
        != WhatsAppConsentStatus.OPTED_IN
    ):
        return "opted_out"
    if not window_service.is_window_open(conversation, now=now):
        return "window_closed"
    if not handoff_service.is_ai_active(conversation):
        return f"handoff_active:{conversation.ai_state.value.lower()}"

    from app.workers.queue import get_job_queue

    queue = get_job_queue()
    text = inbound_text.strip()

    if requests_human(text):
        triggered = handoff_service.trigger_handoff(
            db, queue, conversation=conversation, reason="customer_requested_human", now=now
        )
        if not triggered:
            return "handoff_already_active"
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            contact=contact,
            conversation=conversation,
            content=connection.ai_handoff_message or settings.whatsapp_ai_handoff_message,
            inbound_wamid=inbound_wamid,
            provider=provider,
            access_token=access_token,
            safe_metadata={"policy": {"outcome": "handoff", "reason": "customer_requested_human"}},
            now=now,
            sent_label="handoff_sent",
        )

    if (
        connection.ai_max_replies_per_window > 0
        and conversation.ai_replies_in_window >= connection.ai_max_replies_per_window
    ):
        if not connection.ai_handoff_enabled:
            return "max_replies_reached"
        triggered = handoff_service.trigger_handoff(
            db, queue, conversation=conversation, reason="max_replies_reached", now=now
        )
        if not triggered:
            return "handoff_already_active"
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            contact=contact,
            conversation=conversation,
            content=connection.ai_handoff_message or settings.whatsapp_ai_handoff_message,
            inbound_wamid=inbound_wamid,
            provider=provider,
            access_token=access_token,
            safe_metadata={"policy": {"outcome": "handoff", "reason": "max_replies_reached"}},
            now=now,
            sent_label="handoff_sent",
        )

    business_name = (
        db.scalar(select(Organization.name).where(Organization.id == organization_id)) or ""
    )
    history = _recent_history(
        db,
        conversation,
        limit=settings.whatsapp_ai_history_max_messages,
        exclude_message_id=inbound_message_id,
    )
    customer_facts = customer_context.build_customer_facts(
        db, organization_id=organization_id, phone_e164=contact.phone_e164
    )

    try:
        agent = WhatsAppConversationAgent(
            llm=get_llm_provider(),
            embedder=get_embedding_provider(),
            settings=settings,
            search=search_knowledge,
        )
        reply = agent.respond(
            db,
            organization_id=organization_id,
            business_name=business_name,
            question=text,
            history=history,
            customer_facts=customer_facts,
            conversation_id=conversation.id,
            grounding_required=connection.ai_grounding_required,
        )
    except WhatsAppAIUnavailable as exc:
        return f"generation_failed:{exc.reason}"

    grounding_ok = reply.grounded or reply.is_greeting
    confidence_ok = reply.is_greeting or meets_minimum(
        reply.confidence, connection.ai_min_confidence
    )
    acceptable = confidence_ok and (grounding_ok or not connection.ai_grounding_required)

    if acceptable and reply.content:
        conversation.ai_consecutive_fallbacks = 0
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            contact=contact,
            conversation=conversation,
            content=reply.content,
            inbound_wamid=inbound_wamid,
            provider=provider,
            access_token=access_token,
            safe_metadata={**reply.audit(), "policy": {"outcome": "sent"}},
            now=now,
            sent_label="sent",
        )

    fallback_reason = "no_knowledge" if not grounding_ok else "low_confidence"
    conversation.ai_consecutive_fallbacks += 1
    db.commit()
    if (
        connection.ai_handoff_enabled
        and conversation.ai_consecutive_fallbacks >= settings.whatsapp_ai_max_consecutive_fallbacks
    ):
        triggered = handoff_service.trigger_handoff(
            db, queue, conversation=conversation, reason="repeated_fallback", now=now
        )
        if not triggered:
            return "handoff_already_active"
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            contact=contact,
            conversation=conversation,
            content=connection.ai_handoff_message or settings.whatsapp_ai_handoff_message,
            inbound_wamid=inbound_wamid,
            provider=provider,
            access_token=access_token,
            safe_metadata={
                **reply.audit(),
                "policy": {"outcome": "handoff", "reason": "repeated_fallback"},
            },
            now=now,
            sent_label="handoff_sent",
        )

    return _dispatch(
        db,
        organization_id=organization_id,
        connection=connection,
        contact=contact,
        conversation=conversation,
        content=connection.ai_fallback_message or settings.whatsapp_ai_fallback_message,
        inbound_wamid=inbound_wamid,
        provider=provider,
        access_token=access_token,
        safe_metadata={
            **reply.audit(),
            "policy": {"outcome": "fallback", "reason": fallback_reason},
        },
        now=now,
        sent_label=f"fallback_sent:{fallback_reason}",
    )
