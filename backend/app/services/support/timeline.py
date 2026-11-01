"""Phase 9: the append-only event ledger backing the customer timeline, the support audit trail,
and the consent/suppression change audit -- one mechanism, not three (see
``app/db/models/support.py:SupportTimelineEvent``).

``record_event`` only adds and flushes; it never commits. Every caller writes a timeline event as
part of a larger state change (a priority update, an assignment, a message being stored) and must
commit once, atomically, with that change -- a timeline event must never exist for a mutation that
itself rolled back.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SupportTimelineEvent

MAX_PAYLOAD_CHARS = 4000
MAX_LIMIT = 100
DEFAULT_LIMIT = 50

# Fixed vocabulary (brief section 9/11): keep this in sync with every ``record_event`` call site.
EVENT_TYPES = frozenset(
    {
        "whatsapp_inbound",
        "whatsapp_outbound",
        "email_inbound",
        "email_outbound",
        "website_message",
        "ai_reply",
        "human_reply",
        "human_takeover",
        "ai_handoff",
        "returned_to_ai",
        "assignment_changed",
        "priority_changed",
        "conversation_resolved",
        "conversation_reopened",
        "automation_triggered",
        "shopify_order_event",
        "consent_changed",
        "email_suppressed",
        "email_unsuppressed",
        "campaign_sent",
        "campaign_interaction",
        "email_delivered",
        "email_bounced",
        "email_unsubscribed",
        # Phase 10: operations events that surface on the customer timeline (brief section 23).
        "operational_issue_created",
        "operational_issue_assigned",
        "operational_issue_resolved",
        "operational_task_created",
    }
)


def record_event(
    db: Session,
    *,
    organization_id: uuid.UUID,
    event_type: str,
    customer_id: uuid.UUID | None = None,
    support_conversation_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> SupportTimelineEvent:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown timeline event_type: {event_type!r}")
    payload = payload or {}
    if len(str(payload)) > MAX_PAYLOAD_CHARS:
        payload = {"truncated": True}
    event = SupportTimelineEvent(
        id=uuid.uuid4(),
        organization_id=organization_id,
        customer_id=customer_id,
        support_conversation_id=support_conversation_id,
        event_type=event_type,
        payload=payload,
        created_at=now or datetime.now(UTC),
    )
    db.add(event)
    db.flush()
    return event


def list_customer_timeline(
    db: Session,
    *,
    organization_id: uuid.UUID,
    customer_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
    before: datetime | None = None,
) -> list[SupportTimelineEvent]:
    limit = max(1, min(limit, MAX_LIMIT))
    query = select(SupportTimelineEvent).where(
        SupportTimelineEvent.organization_id == organization_id,
        SupportTimelineEvent.customer_id == customer_id,
    )
    if before is not None:
        query = query.where(SupportTimelineEvent.created_at < before)
    return list(db.scalars(query.order_by(SupportTimelineEvent.created_at.desc()).limit(limit)))


def list_conversation_timeline(
    db: Session,
    *,
    organization_id: uuid.UUID,
    support_conversation_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
    before: datetime | None = None,
) -> list[SupportTimelineEvent]:
    limit = max(1, min(limit, MAX_LIMIT))
    query = select(SupportTimelineEvent).where(
        SupportTimelineEvent.organization_id == organization_id,
        SupportTimelineEvent.support_conversation_id == support_conversation_id,
    )
    if before is not None:
        query = query.where(SupportTimelineEvent.created_at < before)
    return list(db.scalars(query.order_by(SupportTimelineEvent.created_at.desc()).limit(limit)))
