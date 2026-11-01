"""Operational task lifecycle (brief section 3). ``claim`` is the one operation that must be
race-safe under concurrent agents: it uses the same atomic ``UPDATE ... WHERE status=OPEN``
pattern as the rest of the codebase (e.g. ``services/integrations/shopify/sync.py:queue_sync``),
never a read-then-write check, so two concurrent claims can never both succeed.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import DetectedBy, IssueCategory, IssuePriority, OperationalTask, TaskStatus
from app.services.operations import audit


def create_task(
    db: Session,
    *,
    organization_id: uuid.UUID,
    title: str,
    category: IssueCategory,
    created_by_type: DetectedBy,
    description: str | None = None,
    issue_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    priority: IssuePriority = IssuePriority.NORMAL,
    assigned_user_id: uuid.UUID | None = None,
    created_by_user_id: uuid.UUID | None = None,
    due_at: datetime | None = None,
    now: datetime | None = None,
) -> OperationalTask:
    now = now or datetime.now(UTC)
    task = OperationalTask(
        id=uuid.uuid4(),
        organization_id=organization_id,
        issue_id=issue_id,
        customer_id=customer_id,
        order_id=order_id,
        title=title[:300],
        description=description,
        category=category,
        priority=priority,
        status=TaskStatus.ASSIGNED if assigned_user_id else TaskStatus.OPEN,
        assigned_user_id=assigned_user_id,
        created_by_type=created_by_type,
        created_by_user_id=created_by_user_id,
        due_at=due_at,
        created_at=now,
    )
    db.add(task)
    db.flush()
    audit.record(
        db,
        organization_id=organization_id,
        actor_type=created_by_type,
        actor_id=created_by_user_id,
        event_type="TASK_CREATED",
        resource_type="operational_task",
        resource_id=task.id,
        metadata={"category": category.value, "issue_id": str(issue_id) if issue_id else None},
    )
    return task


def assign_task(
    db: Session,
    task: OperationalTask,
    *,
    assigned_user_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> OperationalTask:
    task.assigned_user_id = assigned_user_id
    if task.status == TaskStatus.OPEN:
        task.status = TaskStatus.ASSIGNED
    audit.record(
        db,
        organization_id=task.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="TASK_ASSIGNED",
        resource_type="operational_task",
        resource_id=task.id,
        metadata={"assigned_user_id": str(assigned_user_id)},
    )
    return task


def claim_task(
    db: Session, *, organization_id: uuid.UUID, task_id: uuid.UUID, user_id: uuid.UUID
) -> OperationalTask | None:
    """Atomic claim: only a task that is OPEN and unassigned can be claimed. Returns None if it
    was already claimed, assigned, or does not exist/belong to this organization -- the caller
    cannot distinguish those cases from the return value alone, which is intentional (no extra
    information leaks about another tenant's rows)."""
    now = datetime.now(UTC)
    result = db.execute(
        update(OperationalTask)
        .where(
            OperationalTask.id == task_id,
            OperationalTask.organization_id == organization_id,
            OperationalTask.status == TaskStatus.OPEN,
            OperationalTask.assigned_user_id.is_(None),
        )
        .values(assigned_user_id=user_id, status=TaskStatus.ASSIGNED, updated_at=now)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return None
    task = db.get(OperationalTask, task_id)
    assert task is not None
    db.refresh(task)
    audit.record(
        db,
        organization_id=organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=user_id,
        event_type="TASK_CLAIMED",
        resource_type="operational_task",
        resource_id=task.id,
    )
    return task


def release_task(
    db: Session, task: OperationalTask, *, actor_id: uuid.UUID | None = None
) -> OperationalTask:
    task.assigned_user_id = None
    task.status = TaskStatus.OPEN
    audit.record(
        db,
        organization_id=task.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="TASK_RELEASED",
        resource_type="operational_task",
        resource_id=task.id,
    )
    return task


def escalate_task(
    db: Session,
    task: OperationalTask,
    *,
    actor_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> OperationalTask:
    task.status = TaskStatus.ESCALATED
    task.priority = IssuePriority.URGENT
    audit.record(
        db,
        organization_id=task.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="TASK_ESCALATED",
        resource_type="operational_task",
        resource_id=task.id,
        metadata={"reason": reason},
    )
    return task


def resolve_task(
    db: Session,
    task: OperationalTask,
    *,
    resolution: str | None = None,
    actor_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> OperationalTask:
    now = now or datetime.now(UTC)
    task.status = TaskStatus.RESOLVED
    task.resolution = resolution
    task.completed_at = now
    audit.record(
        db,
        organization_id=task.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="TASK_RESOLVED",
        resource_type="operational_task",
        resource_id=task.id,
    )
    return task


def reopen_task(
    db: Session, task: OperationalTask, *, actor_id: uuid.UUID | None = None
) -> OperationalTask:
    task.status = TaskStatus.OPEN if task.assigned_user_id is None else TaskStatus.ASSIGNED
    task.completed_at = None
    audit.record(
        db,
        organization_id=task.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="TASK_REOPENED",
        resource_type="operational_task",
        resource_id=task.id,
    )
    return task


def cancel_task(
    db: Session,
    task: OperationalTask,
    *,
    actor_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> OperationalTask:
    task.status = TaskStatus.CANCELLED
    audit.record(
        db,
        organization_id=task.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="TASK_CANCELLED",
        resource_type="operational_task",
        resource_id=task.id,
        metadata={"reason": reason},
    )
    return task


def list_tasks(
    db: Session,
    *,
    organization_id: uuid.UUID,
    status: TaskStatus | None = None,
    priority: IssuePriority | None = None,
    assigned_user_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[OperationalTask]:
    query = select(OperationalTask).where(OperationalTask.organization_id == organization_id)
    if status is not None:
        query = query.where(OperationalTask.status == status)
    if priority is not None:
        query = query.where(OperationalTask.priority == priority)
    if assigned_user_id is not None:
        query = query.where(OperationalTask.assigned_user_id == assigned_user_id)
    return list(
        db.scalars(query.order_by(OperationalTask.created_at.desc()).limit(max(1, min(limit, 200))))
    )
