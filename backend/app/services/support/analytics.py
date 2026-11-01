"""Phase 9: support analytics foundation. Minimum metrics only (brief section 24: "do not
overbuild advanced analytics, focus on correct event/data foundations") -- every number here is a
direct aggregate over already-stored columns, no new derived tables, no background rollups.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    EmailMessage,
    EmailMessageStatus,
    SenderType,
    SupportConversation,
    SupportPriority,
    SupportStatus,
)

_CLOSED = (SupportStatus.RESOLVED, SupportStatus.CLOSED)


@dataclass(frozen=True)
class SupportAnalytics:
    total_conversations: int
    open_conversations: int
    urgent_conversations: int
    high_priority_conversations: int
    ai_handled: int
    human_handled: int
    ai_handoffs: int
    avg_first_response_seconds: float | None
    avg_resolution_seconds: float | None
    emails_sent: int
    emails_delivered: int
    emails_bounced: int
    emails_unsubscribed: int


def compute(db: Session, *, organization_id: uuid.UUID) -> SupportAnalytics:
    base = select(func.count(SupportConversation.id)).where(
        SupportConversation.organization_id == organization_id
    )
    total = db.scalar(base) or 0
    open_count = db.scalar(base.where(SupportConversation.status.notin_(_CLOSED))) or 0
    urgent = db.scalar(base.where(SupportConversation.priority == SupportPriority.URGENT)) or 0
    high = db.scalar(base.where(SupportConversation.priority == SupportPriority.HIGH)) or 0
    ai_handled = (
        db.scalar(base.where(SupportConversation.last_response_sender_type == SenderType.AI)) or 0
    )
    human_handled = (
        db.scalar(base.where(SupportConversation.last_response_sender_type == SenderType.HUMAN))
        or 0
    )
    ai_handoffs = (
        db.scalar(
            select(func.count())
            .select_from(SupportConversation)
            .where(
                SupportConversation.organization_id == organization_id,
                SupportConversation.handoff_at.isnot(None),
            )
        )
        or 0
    )

    first_response_seconds = db.scalar(
        select(
            func.avg(
                func.extract("epoch", SupportConversation.first_response_at)
                - func.extract("epoch", SupportConversation.created_at)
            )
        ).where(
            SupportConversation.organization_id == organization_id,
            SupportConversation.first_response_at.isnot(None),
        )
    )
    resolution_seconds = db.scalar(
        select(
            func.avg(
                func.extract("epoch", SupportConversation.resolved_at)
                - func.extract("epoch", SupportConversation.created_at)
            )
        ).where(
            SupportConversation.organization_id == organization_id,
            SupportConversation.resolved_at.isnot(None),
        )
    )

    email_base = select(func.count(EmailMessage.id)).where(
        EmailMessage.organization_id == organization_id
    )
    emails_sent = (
        db.scalar(
            email_base.where(
                EmailMessage.status.in_(
                    [
                        EmailMessageStatus.SENT,
                        EmailMessageStatus.DELIVERED,
                        EmailMessageStatus.BOUNCED,
                    ]
                )
            )
        )
        or 0
    )
    emails_delivered = (
        db.scalar(email_base.where(EmailMessage.status == EmailMessageStatus.DELIVERED)) or 0
    )
    emails_bounced = (
        db.scalar(email_base.where(EmailMessage.status == EmailMessageStatus.BOUNCED)) or 0
    )
    emails_unsubscribed = (
        db.scalar(email_base.where(EmailMessage.status == EmailMessageStatus.UNSUBSCRIBED)) or 0
    )

    return SupportAnalytics(
        total_conversations=total,
        open_conversations=open_count,
        urgent_conversations=urgent,
        high_priority_conversations=high,
        ai_handled=ai_handled,
        human_handled=human_handled,
        ai_handoffs=ai_handoffs,
        avg_first_response_seconds=float(first_response_seconds)
        if first_response_seconds is not None
        else None,
        avg_resolution_seconds=float(resolution_seconds)
        if resolution_seconds is not None
        else None,
        emails_sent=emails_sent,
        emails_delivered=emails_delivered,
        emails_bounced=emails_bounced,
        emails_unsubscribed=emails_unsubscribed,
    )
