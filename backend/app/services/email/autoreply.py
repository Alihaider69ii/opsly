"""Phase 9: inbound email -> AI Brain (multi-turn, RAG-grounded) -> outbound email. Mirrors
``app/services/messaging/whatsapp/autoreply.py``'s policy order exactly, minus the WhatsApp-only
customer-service window (email has none):

    1. AI auto-reply enabled (tenant-level EmailConnection.ai_auto_reply_enabled switch)
    2. customer not suppressed (bounce/complaint/unsubscribe)
    3. human handoff not already in effect (``ai_state == AI_ACTIVE``)
    4. explicit "talk to a human" request -> handoff, no generation
    5. per-thread AI reply cap not exceeded -> otherwise handoff/silence, no generation
    6. build bounded multi-turn context (history + optional read-only customer context)
    7. retrieve + generate (``app.services.ai.email_agent``)
    8. grounding + confidence check against this connection's policy
    9. send the real reply, OR a fallback, OR trigger handoff and send its acknowledgement

AI never decides policy: every rule above is evaluated by this module and
``services/email/handoff.py`` in plain Python before anything is generated, and the one and only
place a provider is called is ``services/email/send.py``.
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    Customer,
    EmailClassification,
    EmailConnection,
    EmailDirection,
    EmailMessage,
    EmailThread,
    Organization,
    SenderType,
)
from app.services.ai.email_agent import EmailAIUnavailable, EmailConversationAgent
from app.services.ai.factory import get_embedding_provider, get_llm_provider
from app.services.ai.provider import ChatMessage
from app.services.ai.whatsapp_agent import meets_minimum, requests_human
from app.services.email import handoff as handoff_service
from app.services.email import send as send_service
from app.services.email.errors import EmailError
from app.services.email.provider import EmailProvider
from app.services.messaging.whatsapp.customer_context import build_customer_facts_for
from app.services.rag.search import search_knowledge

logger = logging.getLogger(__name__)

HISTORY_FETCH_FACTOR = 3


def _recent_history(
    db: Session, thread: EmailThread, *, limit: int, exclude_message_id: uuid.UUID
) -> list[ChatMessage]:
    if limit <= 0:
        return []
    rows = db.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.organization_id == thread.organization_id,
            EmailMessage.thread_id == thread.id,
            EmailMessage.id != exclude_message_id,
            EmailMessage.body_text.isnot(None),
        )
        .order_by(EmailMessage.created_at.desc())
        .limit(limit * HISTORY_FETCH_FACTOR)
    )
    return [
        ChatMessage(
            "user" if m.direction == EmailDirection.INBOUND else "assistant", m.body_text or ""
        )
        for m in reversed(list(rows))
    ]


def _references_for_reply(inbound: EmailMessage) -> str | None:
    existing = (inbound.references_header or "").strip()
    parent = inbound.message_id_header or ""
    chain = f"{existing} {parent}".strip()
    return chain or None


def _dispatch(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: EmailConnection,
    customer: Customer,
    thread: EmailThread,
    subject: str,
    content: str,
    inbound: EmailMessage,
    provider: EmailProvider,
    safe_metadata: dict,
    now: datetime,
    sent_label: str,
) -> str:
    idempotency_key = f"autoreply:{inbound.id}"
    if send_service._existing(db, organization_id, idempotency_key) is None:
        thread.ai_replies_in_window += 1
        db.commit()
    try:
        row = send_service.send_email(
            db,
            organization_id=organization_id,
            connection=connection,
            customer=customer,
            thread=thread,
            outbound=send_service.OutboundEmail(
                subject=subject,
                text_body=content,
                html_body="",
                classification=EmailClassification.TRANSACTIONAL,
                in_reply_to_header=inbound.message_id_header,
                references_header=_references_for_reply(inbound),
            ),
            idempotency_key=idempotency_key,
            provider=provider,
            sender_type=SenderType.AI if sent_label == "sent" else SenderType.SYSTEM,
            safe_metadata=safe_metadata,
            now=now,
        )
    except EmailError as exc:
        return f"error:{exc.code.value}"
    from app.db.models import EmailMessageStatus

    if row.status == EmailMessageStatus.FAILED:
        return f"blocked:{row.error_code}"
    return sent_label


def maybe_auto_reply(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: EmailConnection,
    customer: Customer,
    thread: EmailThread,
    inbound_message_id: uuid.UUID,
    inbound_text: str,
    provider: EmailProvider,
    settings: Settings,
    now: datetime,
) -> str:
    """Returns a short outcome string for audit logging (never the message text)."""
    inbound = db.get(EmailMessage, inbound_message_id)
    if inbound is None:
        return "inbound_missing"

    existing_outbound = send_service._existing(db, organization_id, f"autoreply:{inbound.id}")
    if existing_outbound is not None:
        from app.db.models import EmailMessageStatus

        if existing_outbound.status == EmailMessageStatus.QUEUED:
            return _dispatch(
                db,
                organization_id=organization_id,
                connection=connection,
                customer=customer,
                thread=thread,
                subject=existing_outbound.subject or "",
                content=existing_outbound.body_text or "",
                inbound=inbound,
                provider=provider,
                safe_metadata=existing_outbound.safe_metadata or {},
                now=now,
                sent_label="sent",
            )
        if existing_outbound.status == EmailMessageStatus.SENT:
            return "sent"
        if existing_outbound.status == EmailMessageStatus.FAILED:
            return f"blocked:{existing_outbound.error_code}"
        return "in_flight"

    if not connection.ai_auto_reply_enabled:
        return "disabled"
    from app.services.email import consent as consent_service

    if consent_service.is_suppressed(customer):
        return "suppressed"
    if not handoff_service.is_ai_active(thread):
        return f"handoff_active:{thread.ai_state.value.lower()}"

    from app.workers.queue import get_job_queue

    queue = get_job_queue()
    text = inbound_text.strip()
    reply_subject = f"Re: {thread.subject}" if thread.subject else "Re: your message"

    if requests_human(text):
        triggered = handoff_service.trigger_handoff(
            db, queue, thread=thread, reason="customer_requested_human", now=now
        )
        if not triggered:
            return "handoff_already_active"
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            customer=customer,
            thread=thread,
            subject=reply_subject,
            content=connection.ai_handoff_message or settings.email_ai_handoff_message,
            inbound=inbound,
            provider=provider,
            safe_metadata={"policy": {"outcome": "handoff", "reason": "customer_requested_human"}},
            now=now,
            sent_label="handoff_sent",
        )

    if (
        connection.ai_max_replies_per_window > 0
        and thread.ai_replies_in_window >= connection.ai_max_replies_per_window
    ):
        if not connection.ai_handoff_enabled:
            return "max_replies_reached"
        triggered = handoff_service.trigger_handoff(
            db, queue, thread=thread, reason="max_replies_reached", now=now
        )
        if not triggered:
            return "handoff_already_active"
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            customer=customer,
            thread=thread,
            subject=reply_subject,
            content=connection.ai_handoff_message or settings.email_ai_handoff_message,
            inbound=inbound,
            provider=provider,
            safe_metadata={"policy": {"outcome": "handoff", "reason": "max_replies_reached"}},
            now=now,
            sent_label="handoff_sent",
        )

    business_name = (
        db.scalar(select(Organization.name).where(Organization.id == organization_id)) or ""
    )
    history = _recent_history(
        db, thread, limit=settings.email_ai_history_max_messages, exclude_message_id=inbound.id
    )
    customer_facts = build_customer_facts_for(
        db, organization_id=organization_id, customer=customer
    )

    try:
        agent = EmailConversationAgent(
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
            conversation_id=thread.id,
            grounding_required=connection.ai_grounding_required,
        )
    except EmailAIUnavailable as exc:
        return f"generation_failed:{exc.reason}"

    grounding_ok = reply.grounded or reply.is_greeting
    confidence_ok = reply.is_greeting or meets_minimum(
        reply.confidence, connection.ai_min_confidence
    )
    acceptable = confidence_ok and (grounding_ok or not connection.ai_grounding_required)

    if acceptable and reply.content:
        thread.ai_consecutive_fallbacks = 0
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            customer=customer,
            thread=thread,
            subject=reply_subject,
            content=reply.content,
            inbound=inbound,
            provider=provider,
            safe_metadata={**reply.audit(), "policy": {"outcome": "sent"}},
            now=now,
            sent_label="sent",
        )

    fallback_reason = "no_knowledge" if not grounding_ok else "low_confidence"
    thread.ai_consecutive_fallbacks += 1
    db.commit()
    if (
        connection.ai_handoff_enabled
        and thread.ai_consecutive_fallbacks >= settings.email_ai_max_consecutive_fallbacks
    ):
        triggered = handoff_service.trigger_handoff(
            db, queue, thread=thread, reason="repeated_fallback", now=now
        )
        if not triggered:
            return "handoff_already_active"
        return _dispatch(
            db,
            organization_id=organization_id,
            connection=connection,
            customer=customer,
            thread=thread,
            subject=reply_subject,
            content=connection.ai_handoff_message or settings.email_ai_handoff_message,
            inbound=inbound,
            provider=provider,
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
        customer=customer,
        thread=thread,
        subject=reply_subject,
        content=connection.ai_fallback_message or settings.email_ai_fallback_message,
        inbound=inbound,
        provider=provider,
        safe_metadata={
            **reply.audit(),
            "policy": {"outcome": "fallback", "reason": fallback_reason},
        },
        now=now,
        sent_label=f"fallback_sent:{fallback_reason}",
    )
