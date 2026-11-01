"""Support -> Operations bridge (brief section 16) + the operations events that surface on the
Phase 9 customer timeline (brief section 23). Reuses ``SupportConversation``/
``SupportTimelineEvent`` unchanged -- this module never creates a second customer/conversation
record.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    DetectedBy,
    IssueCategory,
    IssuePriority,
    IssueSeverity,
    OperationalIssue,
    OperationalTask,
)
from app.services.operations import issues, tasks
from app.services.support import timeline as support_timeline
from app.services.support.conversations import ConversationNotFound, get_conversation


def _issue_timeline_payload(issue: OperationalIssue) -> dict[str, Any]:
    return {
        "issue_id": str(issue.id),
        "category": issue.category.value,
        "priority": issue.priority.value,
        "title": issue.title,
    }


def record_issue_timeline(
    db: Session, issue: OperationalIssue, event_type: str, extra: dict[str, Any] | None = None
) -> None:
    """No-op if the issue has no linked customer -- not every operational issue originates from
    (or is visible to) a specific customer (e.g. an inventory alert)."""
    if issue.customer_id is None:
        return
    payload = _issue_timeline_payload(issue)
    if extra:
        payload.update(extra)
    support_timeline.record_event(
        db,
        organization_id=issue.organization_id,
        event_type=event_type,
        customer_id=issue.customer_id,
        payload=payload,
    )


def create_issue_and_task_from_support(
    db: Session,
    *,
    organization_id: uuid.UUID,
    support_conversation_id: uuid.UUID,
    category: IssueCategory,
    title: str,
    description: str | None = None,
    order_id: uuid.UUID | None = None,
    evidence: dict[str, Any] | None = None,
    recommended_action: str | None = None,
    severity: IssueSeverity = IssueSeverity.MEDIUM,
    priority: IssuePriority = IssuePriority.NORMAL,
    detected_by: DetectedBy = DetectedBy.AI,
    now: datetime | None = None,
) -> tuple[OperationalIssue, OperationalTask]:
    """The chain required by brief section 16:
    SupportConversation -> OperationalIssue -> OperationalTask -> Operations Team.

    The support conversation's own ``customer_id`` is reused verbatim -- this never creates or
    matches a customer itself (that stays Phase 9's ``services/support/identity.py`` job).
    """
    try:
        conversation = get_conversation(
            db, organization_id=organization_id, conversation_id=support_conversation_id
        )
    except ConversationNotFound:
        conversation = None
    customer_id = conversation.customer_id if conversation else None

    issue = issues.create_issue(
        db,
        organization_id=organization_id,
        category=category,
        title=title,
        description=description,
        customer_id=customer_id,
        order_id=order_id,
        priority=priority,
        severity=severity,
        detected_by=detected_by,
        recommended_action=recommended_action,
        evidence=evidence,
        now=now,
    )
    task = tasks.create_task(
        db,
        organization_id=organization_id,
        title=f"Follow up: {title}"[:300],
        category=category,
        created_by_type=detected_by,
        issue_id=issue.id,
        customer_id=customer_id,
        order_id=order_id,
        priority=issue.priority,
        now=now,
    )
    if conversation is not None:
        support_timeline.record_event(
            db,
            organization_id=organization_id,
            event_type="operational_issue_created",
            customer_id=customer_id,
            support_conversation_id=conversation.id,
            payload=_issue_timeline_payload(issue),
        )
        support_timeline.record_event(
            db,
            organization_id=organization_id,
            event_type="operational_task_created",
            customer_id=customer_id,
            support_conversation_id=conversation.id,
            payload={"task_id": str(task.id), "issue_id": str(issue.id)},
        )
    return issue, task
