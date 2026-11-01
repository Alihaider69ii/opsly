"""Append-only operational audit log (brief section 22). ``record`` only adds and flushes -- like
``services/support/timeline.py:record_event`` it never commits; callers commit once, atomically,
with the state change it describes.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DetectedBy, OperationalAuditEvent

MAX_METADATA_CHARS = 4000
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Fixed vocabulary (brief section 22) -- keep in sync with every record() call site.
EVENT_TYPES = frozenset(
    {
        "ISSUE_CREATED",
        "ISSUE_UPDATED",
        "ISSUE_PRIORITY_CHANGED",
        "ISSUE_ASSIGNED",
        "ISSUE_RESOLVED",
        "ISSUE_REOPENED",
        "ISSUE_DISMISSED",
        "TASK_CREATED",
        "TASK_ASSIGNED",
        "TASK_CLAIMED",
        "TASK_RELEASED",
        "TASK_ESCALATED",
        "TASK_RESOLVED",
        "TASK_REOPENED",
        "TASK_CANCELLED",
        "SLA_BREACHED",
        "AI_RECOMMENDATION_CREATED",
        "AI_QUERY_ANSWERED",
        "ACTION_REQUESTED",
        "ACTION_APPROVED",
        "ACTION_REJECTED",
        "ACTION_EXECUTED",
        "ACTION_FAILED",
        "SUPPORT_LINKED",
    }
)


def record(
    db: Session,
    *,
    organization_id: uuid.UUID,
    actor_type: DetectedBy,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> OperationalAuditEvent:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown operational audit event_type: {event_type!r}")
    metadata = metadata or {}
    if len(str(metadata)) > MAX_METADATA_CHARS:
        metadata = {"truncated": True}
    event = OperationalAuditEvent(
        id=uuid.uuid4(),
        organization_id=organization_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata_=metadata,
        created_at=now or datetime.now(UTC),
    )
    db.add(event)
    db.flush()
    return event


def list_for_resource(
    db: Session,
    *,
    organization_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
) -> list[OperationalAuditEvent]:
    limit = max(1, min(limit, MAX_LIMIT))
    return list(
        db.scalars(
            select(OperationalAuditEvent)
            .where(
                OperationalAuditEvent.organization_id == organization_id,
                OperationalAuditEvent.resource_type == resource_type,
                OperationalAuditEvent.resource_id == resource_id,
            )
            .order_by(OperationalAuditEvent.created_at.desc())
            .limit(limit)
        )
    )


def list_recent(
    db: Session, *, organization_id: uuid.UUID, limit: int = DEFAULT_LIMIT
) -> list[OperationalAuditEvent]:
    limit = max(1, min(limit, MAX_LIMIT))
    return list(
        db.scalars(
            select(OperationalAuditEvent)
            .where(OperationalAuditEvent.organization_id == organization_id)
            .order_by(OperationalAuditEvent.created_at.desc())
            .limit(limit)
        )
    )
