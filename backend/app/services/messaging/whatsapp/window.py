"""Customer-service window tracking and the get-or-create conversation.

Eligibility is always recomputed live, from the stored timestamps, at the moment of send. The
denormalized ``window_expires_at`` column exists for cheap display and listing only; it is never
treated as a cached decision, and no separate expiry sweep is needed because nothing ever acts on
a stale cached boolean.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import WhatsAppConversation


def _aware(value: datetime) -> datetime:
    """SQLite drops timezone metadata; interpret naive stored values as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def get_or_create_conversation(
    db: Session,
    *,
    organization_id: uuid.UUID,
    contact_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> WhatsAppConversation:
    conversation = db.scalar(
        select(WhatsAppConversation)
        .where(
            WhatsAppConversation.organization_id == organization_id,
            WhatsAppConversation.contact_id == contact_id,
            WhatsAppConversation.connection_id == connection_id,
        )
        .with_for_update()
    )
    if conversation is not None:
        return conversation
    conversation = WhatsAppConversation(
        id=uuid.uuid4(),
        organization_id=organization_id,
        contact_id=contact_id,
        connection_id=connection_id,
    )
    db.add(conversation)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        conversation = db.scalar(
            select(WhatsAppConversation).where(
                WhatsAppConversation.organization_id == organization_id,
                WhatsAppConversation.contact_id == contact_id,
                WhatsAppConversation.connection_id == connection_id,
            )
        )
        if conversation is None:
            raise
    return conversation


def record_inbound(conversation: WhatsAppConversation, *, now: datetime, window_hours: int) -> None:
    """A customer message opens or resets the window and always counts toward message_count.

    Phase 8: if the previous window had already expired (or none existed yet), this inbound
    message opens a genuinely NEW window, so ``ai_replies_in_window`` -- the AI auto-reply loop/
    cost control in ``services/messaging/whatsapp/autoreply.py`` -- resets to 0. A message that
    merely refreshes an already-open window does not reset the counter.
    """
    if not is_window_open(conversation, now=now):
        conversation.ai_replies_in_window = 0
    conversation.last_inbound_at = now
    conversation.window_expires_at = now + timedelta(hours=window_hours)
    conversation.message_count += 1


def record_outbound(conversation: WhatsAppConversation, *, now: datetime) -> None:
    conversation.last_outbound_at = now
    conversation.message_count += 1


def is_window_open(conversation: WhatsAppConversation, *, now: datetime) -> bool:
    if conversation.window_expires_at is None:
        return False
    return _aware(conversation.window_expires_at) > now
