"""Dashboard KPIs + analytics (brief sections 8, 27). Every number is a real aggregate query --
nothing here is estimated or faked (brief: "Do not fake numbers").
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    IssueCategory,
    IssuePriority,
    IssueStatus,
    OperationalAction,
    OperationalIssue,
    OperationalTask,
    TaskStatus,
)


def dashboard_kpis(db: Session, *, organization_id: uuid.UUID) -> dict[str, int]:
    def count_issues(*filters: Any) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(OperationalIssue)
                .where(OperationalIssue.organization_id == organization_id, *filters)
            )
            or 0
        )

    open_statuses = OperationalIssue.status.not_in([IssueStatus.RESOLVED, IssueStatus.DISMISSED])
    return {
        "open_issues": count_issues(open_statuses),
        "urgent_issues": count_issues(
            open_statuses, OperationalIssue.priority == IssuePriority.URGENT
        ),
        "high_priority_issues": count_issues(
            open_statuses, OperationalIssue.priority == IssuePriority.HIGH
        ),
        "sla_breaches": count_issues(open_statuses, OperationalIssue.breached_at.is_not(None)),
        "open_tasks": (
            db.scalar(
                select(func.count())
                .select_from(OperationalTask)
                .where(
                    OperationalTask.organization_id == organization_id,
                    OperationalTask.status.not_in([TaskStatus.RESOLVED, TaskStatus.CANCELLED]),
                )
            )
            or 0
        ),
        "orders_needing_attention": count_issues(
            open_statuses,
            OperationalIssue.category.in_([IssueCategory.ORDER, IssueCategory.WAREHOUSE]),
        ),
        "inventory_alerts": count_issues(
            open_statuses, OperationalIssue.category == IssueCategory.INVENTORY
        ),
        "payment_alerts": count_issues(
            open_statuses, OperationalIssue.category == IssueCategory.PAYMENT
        ),
        "shipping_ndr_rto_alerts": count_issues(
            open_statuses,
            OperationalIssue.category.in_(
                [IssueCategory.SHIPPING, IssueCategory.NDR, IssueCategory.RTO]
            ),
        ),
    }


def issues_by_category(db: Session, *, organization_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        select(OperationalIssue.category, func.count())
        .where(OperationalIssue.organization_id == organization_id)
        .group_by(OperationalIssue.category)
    ).all()
    return {category.value: count for category, count in rows}


def issues_by_priority(db: Session, *, organization_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        select(OperationalIssue.priority, func.count())
        .where(OperationalIssue.organization_id == organization_id)
        .group_by(OperationalIssue.priority)
    ).all()
    return {priority.value: count for priority, count in rows}


def issues_by_status(db: Session, *, organization_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        select(OperationalIssue.status, func.count())
        .where(OperationalIssue.organization_id == organization_id)
        .group_by(OperationalIssue.status)
    ).all()
    return {status.value: count for status, count in rows}


def tasks_by_status(db: Session, *, organization_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        select(OperationalTask.status, func.count())
        .where(OperationalTask.organization_id == organization_id)
        .group_by(OperationalTask.status)
    ).all()
    return {status.value: count for status, count in rows}


def ai_vs_human_handling(db: Session, *, organization_id: uuid.UUID) -> dict[str, dict[str, int]]:
    issue_rows = db.execute(
        select(OperationalIssue.detected_by, func.count())
        .where(OperationalIssue.organization_id == organization_id)
        .group_by(OperationalIssue.detected_by)
    ).all()
    task_rows = db.execute(
        select(OperationalTask.created_by_type, func.count())
        .where(OperationalTask.organization_id == organization_id)
        .group_by(OperationalTask.created_by_type)
    ).all()
    return {
        "issues_detected_by": {detected_by.value: count for detected_by, count in issue_rows},
        "tasks_created_by": {created_by.value: count for created_by, count in task_rows},
    }


def average_resolution_minutes(db: Session, *, organization_id: uuid.UUID) -> float | None:
    rows = db.execute(
        select(OperationalIssue.created_at, OperationalIssue.resolved_at).where(
            OperationalIssue.organization_id == organization_id,
            OperationalIssue.status == IssueStatus.RESOLVED,
            OperationalIssue.resolved_at.is_not(None),
        )
    ).all()
    if not rows:
        return None
    total_minutes = sum((resolved - created).total_seconds() / 60 for created, resolved in rows)
    return round(total_minutes / len(rows), 1)


def actions_summary(db: Session, *, organization_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        select(OperationalAction.status, func.count())
        .where(OperationalAction.organization_id == organization_id)
        .group_by(OperationalAction.status)
    ).all()
    return {status.value: count for status, count in rows}


def full_analytics(db: Session, *, organization_id: uuid.UUID) -> dict[str, Any]:
    return {
        "kpis": dashboard_kpis(db, organization_id=organization_id),
        "issues_by_category": issues_by_category(db, organization_id=organization_id),
        "issues_by_priority": issues_by_priority(db, organization_id=organization_id),
        "issues_by_status": issues_by_status(db, organization_id=organization_id),
        "tasks_by_status": tasks_by_status(db, organization_id=organization_id),
        "ai_vs_human": ai_vs_human_handling(db, organization_id=organization_id),
        "average_resolution_minutes": average_resolution_minutes(
            db, organization_id=organization_id
        ),
        "actions_summary": actions_summary(db, organization_id=organization_id),
    }
