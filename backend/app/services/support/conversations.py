"""Phase 9: the unified cross-channel support conversation layer.

``SupportConversation`` is a thin index row kept in sync by each channel's own intake code (see
``app/db/models/support.py`` module docstring for why this is not a rewrite of the channel-
specific tables). This module owns:

* ``sync_conversation`` -- the upsert every channel's intake code calls after storing a message
* unified inbox listing/filtering (``list_inbox``)
* priority, assignment, and AI/human-switching actions, dispatched per channel:
    - WhatsApp: delegates to the Phase 8 state machine (``messaging/whatsapp/handoff.py``), which
      stays authoritative on ``WhatsAppConversation.ai_state`` -- never decided independently here
    - Email: delegates to its own mirror state machine (``services/email/handoff.py``), same
      reason -- ``EmailThread.ai_state`` is authoritative
    - Website: has no prior per-channel handoff concept, so this row's ``ai_state`` IS the
      source of truth for that channel only

Every mutating function here only adds/flushes/updates; it never calls ``db.commit()`` (except
where it must delegate into WhatsApp's own commit-per-transition functions, documented below) --
callers commit once, atomically, with whatever else the action implies.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AIHandoffState,
    EmailThread,
    OrganizationMember,
    PrioritySource,
    SenderType,
    SupportChannel,
    SupportConversation,
    SupportPriority,
    SupportStatus,
    WhatsAppConversation,
)
from app.services.email import handoff as email_handoff
from app.services.messaging.whatsapp import handoff as whatsapp_handoff
from app.services.support import timeline
from app.workers.queue import JobQueue

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
_CLOSED_OUT = (SupportStatus.RESOLVED, SupportStatus.CLOSED)


class NotOrganizationMember(Exception):
    pass


class ConversationNotFound(Exception):
    pass


def _derive_status(
    ai_state: AIHandoffState, *, direction: Literal["inbound", "outbound"]
) -> SupportStatus:
    if ai_state == AIHandoffState.HUMAN_ACTIVE and direction == "outbound":
        return SupportStatus.WAITING
    return SupportStatus(ai_state.value)


def sync_conversation(
    db: Session,
    *,
    organization_id: uuid.UUID,
    channel: SupportChannel,
    channel_conversation_id: uuid.UUID,
    ai_state: AIHandoffState,
    direction: Literal["inbound", "outbound"],
    sender_type: SenderType,
    event_type: str,
    customer_id: uuid.UUID | None = None,
    message_preview: str | None = None,
    now: datetime | None = None,
) -> SupportConversation:
    """Upsert the index row for one channel conversation. Called after every stored message, by
    every channel's intake code (WhatsApp webhook processing, website chat service, email
    inbound/outbound). Idempotent to call repeatedly with the channel's current state."""
    now = now or datetime.now(UTC)
    row = db.scalar(
        select(SupportConversation)
        .where(
            SupportConversation.organization_id == organization_id,
            SupportConversation.channel == channel,
            SupportConversation.channel_conversation_id == channel_conversation_id,
        )
        .with_for_update()
    )
    if row is None:
        row = SupportConversation(
            id=uuid.uuid4(),
            organization_id=organization_id,
            channel=channel,
            channel_conversation_id=channel_conversation_id,
            customer_id=customer_id,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return sync_conversation(
                db,
                organization_id=organization_id,
                channel=channel,
                channel_conversation_id=channel_conversation_id,
                ai_state=ai_state,
                direction=direction,
                sender_type=sender_type,
                event_type=event_type,
                customer_id=customer_id,
                message_preview=message_preview,
                now=now,
            )
    elif customer_id is not None and row.customer_id is None:
        row.customer_id = customer_id

    was_closed_out = row.status in _CLOSED_OUT
    row.ai_state = ai_state
    if direction == "inbound":
        row.last_customer_activity_at = now
        row.unread = True
        if was_closed_out or row.status not in _CLOSED_OUT:
            row.status = SupportStatus(ai_state.value)
    else:
        row.last_response_at = now
        row.last_response_sender_type = sender_type
        if row.first_response_at is None:
            row.first_response_at = now
        row.unread = False
        if row.status not in _CLOSED_OUT:
            row.status = _derive_status(ai_state, direction=direction)
    if message_preview is not None:
        row.last_message_preview = message_preview[:300]
    db.flush()

    timeline.record_event(
        db,
        organization_id=organization_id,
        event_type=event_type,
        customer_id=row.customer_id,
        support_conversation_id=row.id,
        payload={
            "channel": channel.value,
            "direction": direction,
            "sender_type": sender_type.value,
            "preview": (message_preview or "")[:200],
        },
        now=now,
    )
    return row


# --- unified inbox ---------------------------------------------------------------------------


def get_conversation(
    db: Session, *, organization_id: uuid.UUID, conversation_id: uuid.UUID
) -> SupportConversation:
    row = db.scalar(
        select(SupportConversation).where(
            SupportConversation.id == conversation_id,
            SupportConversation.organization_id == organization_id,
        )
    )
    if row is None:
        raise ConversationNotFound
    return row


def list_inbox(
    db: Session,
    *,
    organization_id: uuid.UUID,
    channel: SupportChannel | None = None,
    priority: SupportPriority | None = None,
    status: SupportStatus | None = None,
    assignment: Literal["all", "me", "unassigned"] | uuid.UUID | None = None,
    viewer_user_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[SupportConversation]:
    limit = max(1, min(limit, MAX_LIMIT))
    query = select(SupportConversation).where(
        SupportConversation.organization_id == organization_id
    )
    if channel is not None:
        query = query.where(SupportConversation.channel == channel)
    if priority is not None:
        query = query.where(SupportConversation.priority == priority)
    if status is not None:
        query = query.where(SupportConversation.status == status)
    if assignment == "unassigned":
        query = query.where(SupportConversation.assigned_agent_user_id.is_(None))
    elif assignment == "me":
        query = query.where(SupportConversation.assigned_agent_user_id == viewer_user_id)
    elif isinstance(assignment, uuid.UUID):
        query = query.where(SupportConversation.assigned_agent_user_id == assignment)
    query = (
        query.order_by(SupportConversation.updated_at.desc()).limit(limit).offset(max(0, offset))
    )
    return list(db.scalars(query))


# --- priority ----------------------------------------------------------------------------------


def set_priority(
    db: Session,
    *,
    conversation: SupportConversation,
    priority: SupportPriority,
    source: PrioritySource,
    reason: str | None = None,
    changed_by: uuid.UUID | None = None,
    now: datetime | None = None,
) -> bool:
    """Returns False (a no-op) if a human already set the priority and this call is not itself
    HUMAN -- a human-set priority must never be silently overwritten by AI/system (brief section
    5). A human call always wins, including overriding another human's choice."""
    now = now or datetime.now(UTC)
    if conversation.priority_source == PrioritySource.HUMAN and source != PrioritySource.HUMAN:
        return False
    conversation.priority = priority
    conversation.priority_source = source
    conversation.priority_changed_by = changed_by
    conversation.priority_changed_at = now
    conversation.priority_reason = (reason or "")[:200] or None
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="priority_changed",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"priority": priority.value, "source": source.value, "reason": reason},
        now=now,
    )
    return True


# --- assignment ---------------------------------------------------------------------------------


def _require_member(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
    member = db.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
    )
    if member is None:
        raise NotOrganizationMember


def assign(
    db: Session,
    *,
    conversation: SupportConversation,
    agent_user_id: uuid.UUID,
    assigned_by: uuid.UUID,
    now: datetime | None = None,
) -> SupportConversation:
    """Manager action: assign or reassign unconditionally (no precondition on current
    assignment). Raises ``NotOrganizationMember`` if ``agent_user_id`` does not belong to this
    organization."""
    now = now or datetime.now(UTC)
    _require_member(db, organization_id=conversation.organization_id, user_id=agent_user_id)
    conversation.assigned_agent_user_id = agent_user_id
    conversation.assigned_at = now
    conversation.assigned_by = assigned_by
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="assignment_changed",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"assigned_agent_user_id": str(agent_user_id), "action": "assign"},
        now=now,
    )
    return conversation


def claim(
    db: Session,
    *,
    conversation: SupportConversation,
    agent_user_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Self-claim, atomic and race-safe: two agents clicking "claim" on the same unassigned
    conversation can never both win (brief section 30)."""
    now = now or datetime.now(UTC)
    _require_member(db, organization_id=conversation.organization_id, user_id=agent_user_id)
    result = db.execute(
        update(SupportConversation)
        .where(
            SupportConversation.id == conversation.id,
            SupportConversation.organization_id == conversation.organization_id,
            SupportConversation.assigned_agent_user_id.is_(None),
        )
        .values(assigned_agent_user_id=agent_user_id, assigned_at=now, assigned_by=agent_user_id)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return False
    db.refresh(conversation)
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="assignment_changed",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"assigned_agent_user_id": str(agent_user_id), "action": "claim"},
        now=now,
    )
    return True


def release(
    db: Session, *, conversation: SupportConversation, now: datetime | None = None
) -> SupportConversation:
    now = now or datetime.now(UTC)
    conversation.assigned_agent_user_id = None
    conversation.assigned_at = None
    conversation.assigned_by = None
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="assignment_changed",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"assigned_agent_user_id": None, "action": "release"},
        now=now,
    )
    return conversation


# --- AI / human switching ----------------------------------------------------------------------


def _whatsapp_conversation(
    db: Session, conversation: SupportConversation
) -> WhatsAppConversation | None:
    return db.scalar(
        select(WhatsAppConversation).where(
            WhatsAppConversation.id == conversation.channel_conversation_id,
            WhatsAppConversation.organization_id == conversation.organization_id,
        )
    )


def _email_thread(db: Session, conversation: SupportConversation) -> EmailThread | None:
    return db.scalar(
        select(EmailThread).where(
            EmailThread.id == conversation.channel_conversation_id,
            EmailThread.organization_id == conversation.organization_id,
        )
    )


def take_over(
    db: Session,
    queue: JobQueue,
    *,
    conversation: SupportConversation,
    by_user_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """A human takes over immediately, from any non-HUMAN_ACTIVE state. For WhatsApp/Email this
    delegates to (and commits via) that channel's own ``take_over`` -- the two writes (the
    channel table, then this row) are not one atomic transaction; a crash between them leaves a
    transient mismatch that self-heals on the next inbound/outbound sync (documented known
    limitation, matching Phase 8's own commit-per-transition pattern)."""
    now = now or datetime.now(UTC)
    _require_member(db, organization_id=conversation.organization_id, user_id=by_user_id)
    if conversation.channel == SupportChannel.WHATSAPP:
        wa_conversation = _whatsapp_conversation(db, conversation)
        if wa_conversation is None or not whatsapp_handoff.take_over(
            db, conversation=wa_conversation, now=now
        ):
            return False
    elif conversation.channel == SupportChannel.EMAIL:
        email_thread = _email_thread(db, conversation)
        if email_thread is None or not email_handoff.take_over(db, thread=email_thread, now=now):
            return False
    else:
        result = db.execute(
            update(SupportConversation)
            .where(
                SupportConversation.id == conversation.id,
                SupportConversation.organization_id == conversation.organization_id,
                SupportConversation.ai_state != AIHandoffState.HUMAN_ACTIVE,
            )
            .values(ai_state=AIHandoffState.HUMAN_ACTIVE, handoff_at=now)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            return False
    db.refresh(conversation)
    conversation.ai_state = AIHandoffState.HUMAN_ACTIVE
    conversation.status = SupportStatus.HUMAN_ACTIVE
    conversation.handoff_at = now
    if conversation.assigned_agent_user_id is None:
        conversation.assigned_agent_user_id = by_user_id
        conversation.assigned_at = now
        conversation.assigned_by = by_user_id
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="human_takeover",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"by": str(by_user_id)},
        now=now,
    )
    return True


def return_to_ai(
    db: Session, queue: JobQueue, *, conversation: SupportConversation, now: datetime | None = None
) -> bool:
    """Control returns to AI. For WhatsApp, Phase 8's AI rules (RAG grounding, confidence,
    suppression, customer-service window, handoff logic) resume unchanged -- this call only
    flips the state, nothing here weakens or bypasses any of them."""
    now = now or datetime.now(UTC)
    if conversation.channel == SupportChannel.WHATSAPP:
        wa_conversation = _whatsapp_conversation(db, conversation)
        if wa_conversation is None or not whatsapp_handoff.resolve(
            db, conversation=wa_conversation, now=now
        ):
            return False
    elif conversation.channel == SupportChannel.EMAIL:
        email_thread = _email_thread(db, conversation)
        if email_thread is None or not email_handoff.resolve(db, thread=email_thread, now=now):
            return False
    else:
        if conversation.ai_state == AIHandoffState.AI_ACTIVE:
            return False
    db.refresh(conversation)
    conversation.ai_state = AIHandoffState.AI_ACTIVE
    conversation.status = SupportStatus.AI_ACTIVE
    conversation.handoff_reason = None
    conversation.handoff_at = None
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="returned_to_ai",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={},
        now=now,
    )
    return True


def request_human(
    db: Session,
    queue: JobQueue,
    *,
    conversation: SupportConversation,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Flags a conversation for human attention without assigning anyone (HUMAN_REQUIRED) --
    used both by a dashboard "request human" action and internally by the email/website AI
    auto-reply policy layer, mirroring how WhatsApp's autoreply.py already triggers this."""
    now = now or datetime.now(UTC)
    if conversation.channel == SupportChannel.WHATSAPP:
        wa_conversation = _whatsapp_conversation(db, conversation)
        if wa_conversation is None or not whatsapp_handoff.trigger_handoff(
            db, queue, conversation=wa_conversation, reason=reason, now=now
        ):
            return False
    elif conversation.channel == SupportChannel.EMAIL:
        email_thread = _email_thread(db, conversation)
        if email_thread is None or not email_handoff.trigger_handoff(
            db, queue, thread=email_thread, reason=reason, now=now
        ):
            return False
    else:
        result = db.execute(
            update(SupportConversation)
            .where(
                SupportConversation.id == conversation.id,
                SupportConversation.organization_id == conversation.organization_id,
                SupportConversation.ai_state == AIHandoffState.AI_ACTIVE,
            )
            .values(
                ai_state=AIHandoffState.HUMAN_REQUIRED, handoff_reason=reason[:60], handoff_at=now
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            return False
    db.refresh(conversation)
    conversation.ai_state = AIHandoffState.HUMAN_REQUIRED
    conversation.status = SupportStatus.HUMAN_REQUIRED
    conversation.handoff_reason = reason[:60]
    conversation.handoff_at = now
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="ai_handoff",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"reason": reason},
        now=now,
    )
    return True


# --- resolve / reopen -----------------------------------------------------------------------


def resolve_conversation(
    db: Session,
    queue: JobQueue,
    *,
    conversation: SupportConversation,
    by_user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> SupportConversation:
    now = now or datetime.now(UTC)
    conversation.status = SupportStatus.RESOLVED
    conversation.resolved_at = now
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="conversation_resolved",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"by": str(by_user_id) if by_user_id else None},
        now=now,
    )
    if conversation.customer_id is not None:
        # Cross-channel automation foundation (brief section 23): a workflow may react to any
        # channel's conversation resolving, e.g. "WhatsApp resolves -> email follow-up".
        from app.services.automation import ingest_event

        ingest_event(
            db,
            queue,
            organization_id=conversation.organization_id,
            event_type="support_conversation_resolved",
            source=conversation.channel.value.lower(),
            source_id=str(conversation.id),
            dedupe_key=f"support:resolved:{conversation.id}:{int(now.timestamp())}",
            payload={
                "event": {"source": conversation.channel.value.lower()},
                "customer": {"exists": True, "id": str(conversation.customer_id)},
                "support": {
                    "conversation_id": str(conversation.id),
                    "channel": conversation.channel.value,
                },
            },
        )
    return conversation


class ChannelNotConfigured(Exception):
    pass


def send_human_reply(
    db: Session,
    queue: JobQueue,
    *,
    conversation: SupportConversation,
    by_user_id: uuid.UUID,
    content: str,
    settings,
    now: datetime | None = None,
):
    """A human replies from the unified inbox, regardless of channel. Implicitly takes over the
    conversation first if a human has not already (brief: "humans should be able to take over at
    any time" -- replying IS taking over, in the same way a support agent picking up a ticket
    and answering it are the same action). Returns the channel's own message row. Raises
    ``ChannelNotConfigured``, or the channel's own error type (``MessagingError``/``EmailError``)
    on a policy/provider failure."""
    now = now or datetime.now(UTC)
    _require_member(db, organization_id=conversation.organization_id, user_id=by_user_id)
    if conversation.ai_state != AIHandoffState.HUMAN_ACTIVE:
        take_over(db, queue, conversation=conversation, by_user_id=by_user_id, now=now)
        db.refresh(conversation)

    if conversation.channel == SupportChannel.WEBSITE:
        from app.db.models import Conversation, Message, MessageRole

        website_conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation.channel_conversation_id,
                Conversation.organization_id == conversation.organization_id,
            )
        )
        if website_conversation is None:
            raise ChannelNotConfigured("website conversation not found")
        db.refresh(website_conversation)
        website_conversation.message_count += 1
        website_conversation.updated_at = now
        message = Message(
            organization_id=conversation.organization_id,
            conversation_id=website_conversation.id,
            position=website_conversation.message_count,
            role=MessageRole.ASSISTANT,
            content=content,
            sender_type=SenderType.HUMAN,
            sender_user_id=by_user_id,
        )
        db.add(message)
        db.commit()
        sync_conversation(
            db,
            organization_id=conversation.organization_id,
            channel=SupportChannel.WEBSITE,
            channel_conversation_id=website_conversation.id,
            ai_state=AIHandoffState.HUMAN_ACTIVE,
            direction="outbound",
            sender_type=SenderType.HUMAN,
            event_type="human_reply",
            customer_id=conversation.customer_id,
            now=now,
        )
        db.commit()
        return message

    if conversation.channel == SupportChannel.WHATSAPP:
        from app.core.config import get_settings as _get_settings
        from app.core.secrets import SecretManager
        from app.db.models import (
            WhatsAppConnection,
            WhatsAppConnectionStatus,
            WhatsAppConsentCategory,
            WhatsAppContact,
        )
        from app.services.messaging.factory import get_messaging_provider
        from app.services.messaging.whatsapp import send as whatsapp_send

        wa_conversation = _whatsapp_conversation(db, conversation)
        if wa_conversation is None:
            raise ChannelNotConfigured("whatsapp conversation not found")
        connection = db.get(WhatsAppConnection, wa_conversation.connection_id)
        contact = db.get(WhatsAppContact, wa_conversation.contact_id)
        if (
            connection is None
            or contact is None
            or connection.status != WhatsAppConnectionStatus.CONNECTED
        ):
            raise ChannelNotConfigured("whatsapp connection not available")
        secrets = SecretManager.from_settings(settings or _get_settings())
        access_token = whatsapp_send.decrypt_access_token(connection, secrets)
        provider = get_messaging_provider()
        idem_key = (
            f"human:{conversation.organization_id}:{wa_conversation.id}:"
            f"{int(now.timestamp() * 1000)}:{uuid.uuid4().hex[:8]}"
        )
        result = whatsapp_send.send_text(
            db,
            organization_id=conversation.organization_id,
            connection=connection,
            contact=contact,
            outbound=whatsapp_send.OutboundText(
                content=content, consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key=idem_key,
            provider=provider,
            access_token=access_token,
            sender_type=SenderType.HUMAN,
            sender_user_id=by_user_id,
            now=now,
        )
        sync_conversation(
            db,
            organization_id=conversation.organization_id,
            channel=SupportChannel.WHATSAPP,
            channel_conversation_id=wa_conversation.id,
            ai_state=AIHandoffState.HUMAN_ACTIVE,
            direction="outbound",
            sender_type=SenderType.HUMAN,
            event_type="human_reply",
            customer_id=conversation.customer_id,
            now=now,
        )
        db.commit()
        return result

    from app.db.models import Customer, EmailConnection
    from app.services.email import send as email_send
    from app.services.email.factory import get_email_provider

    email_thread = _email_thread(db, conversation)
    if email_thread is None:
        raise ChannelNotConfigured("email thread not found")
    email_connection = db.scalar(
        select(EmailConnection).where(
            EmailConnection.organization_id == conversation.organization_id
        )
    )
    customer = db.get(Customer, conversation.customer_id) if conversation.customer_id else None
    if email_connection is None or customer is None or not customer.email:
        raise ChannelNotConfigured("email connection or customer not available")
    email_provider = get_email_provider()
    idem_key = (
        f"human:{conversation.organization_id}:{email_thread.id}:"
        f"{int(now.timestamp() * 1000)}:{uuid.uuid4().hex[:8]}"
    )
    from app.db.models import EmailClassification

    email_result = email_send.send_email(
        db,
        organization_id=conversation.organization_id,
        connection=email_connection,
        customer=customer,
        thread=email_thread,
        outbound=email_send.OutboundEmail(
            subject=f"Re: {email_thread.subject}" if email_thread.subject else "Re: your message",
            text_body=content,
            html_body="",
            classification=EmailClassification.TRANSACTIONAL,
        ),
        idempotency_key=idem_key,
        provider=email_provider,
        sender_type=SenderType.HUMAN,
        sender_user_id=by_user_id,
        now=now,
    )
    sync_conversation(
        db,
        organization_id=conversation.organization_id,
        channel=SupportChannel.EMAIL,
        channel_conversation_id=email_thread.id,
        ai_state=AIHandoffState.HUMAN_ACTIVE,
        direction="outbound",
        sender_type=SenderType.HUMAN,
        event_type="human_reply",
        customer_id=conversation.customer_id,
        now=now,
    )
    db.commit()
    return email_result


def reopen_conversation(
    db: Session,
    *,
    conversation: SupportConversation,
    by_user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    if conversation.status not in _CLOSED_OUT:
        return False
    conversation.status = SupportStatus(conversation.ai_state.value)
    timeline.record_event(
        db,
        organization_id=conversation.organization_id,
        event_type="conversation_reopened",
        customer_id=conversation.customer_id,
        support_conversation_id=conversation.id,
        payload={"by": str(by_user_id) if by_user_id else None},
        now=now,
    )
    return True
