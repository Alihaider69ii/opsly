"""Phase 9: a normalized, read-only view over whichever channel table actually backs a
``SupportConversation`` (``Message``/``WhatsAppMessage``/``EmailMessage``) -- the unified inbox's
message list without physically merging three differently-shaped tables (see
``app/db/models/support.py`` module docstring for why they stay separate)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    EmailMessage,
    Message,
    MessageRole,
    SenderType,
    SupportChannel,
    SupportConversation,
    WhatsAppMessage,
)


@dataclass(frozen=True)
class UnifiedMessage:
    id: uuid.UUID
    direction: str  # "INBOUND" | "OUTBOUND"
    sender_type: SenderType | None
    sender_user_id: uuid.UUID | None
    content: str | None
    status: str
    ai: dict[str, Any] | None
    policy: dict[str, Any] | None
    error_code: str | None
    created_at: datetime


def list_messages(
    db: Session, conversation: SupportConversation, *, limit: int = 500
) -> list[UnifiedMessage]:
    if conversation.channel == SupportChannel.WEBSITE:
        website_rows = db.scalars(
            select(Message)
            .where(
                Message.organization_id == conversation.organization_id,
                Message.conversation_id == conversation.channel_conversation_id,
                Message.role != MessageRole.SYSTEM,
            )
            .order_by(Message.position.asc())
            .limit(limit)
        )
        return [
            UnifiedMessage(
                id=m.id,
                direction="INBOUND" if m.role == MessageRole.USER else "OUTBOUND",
                sender_type=m.sender_type,
                sender_user_id=m.sender_user_id,
                content=m.content,
                status="RECEIVED" if m.role == MessageRole.USER else "SENT",
                ai=(m.meta or {}).get("ai") if isinstance(m.meta, dict) else None,
                policy=(m.meta or {}).get("policy") if isinstance(m.meta, dict) else None,
                error_code=None,
                created_at=m.created_at,
            )
            for m in website_rows
        ]
    if conversation.channel == SupportChannel.WHATSAPP:
        whatsapp_rows = db.scalars(
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.organization_id == conversation.organization_id,
                WhatsAppMessage.conversation_id == conversation.channel_conversation_id,
            )
            .order_by(WhatsAppMessage.created_at.asc())
            .limit(limit)
        )
        return [
            UnifiedMessage(
                id=m.id,
                direction=m.direction.value,
                sender_type=m.sender_type,
                sender_user_id=m.sender_user_id,
                content=m.content,
                status=m.status.value,
                ai=m.ai_metadata,
                policy=m.ai_policy,
                error_code=m.error_code,
                created_at=m.created_at,
            )
            for m in whatsapp_rows
        ]
    email_rows = db.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.organization_id == conversation.organization_id,
            EmailMessage.thread_id == conversation.channel_conversation_id,
        )
        .order_by(EmailMessage.created_at.asc())
        .limit(limit)
    )
    return [
        UnifiedMessage(
            id=m.id,
            direction=m.direction.value,
            sender_type=m.sender_type,
            sender_user_id=m.sender_user_id,
            content=m.body_text or m.body_html_sanitized,
            status=m.status.value,
            ai=m.ai_metadata,
            policy=(m.safe_metadata or {}).get("policy")
            if isinstance(m.safe_metadata, dict)
            else None,
            error_code=m.error_code,
            created_at=m.created_at,
        )
        for m in email_rows
    ]
