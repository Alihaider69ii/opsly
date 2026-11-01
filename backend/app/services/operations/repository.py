"""Read-only, tenant-scoped query helpers backing the Operations API (brief sections 10-14, 28).
Every query starts from ``organization_id`` (mirrors ``app/db/scoping.py:scoped_select``), so a
lookup can never reach another tenant's row.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    IssueCategory,
    IssuePriority,
    IssueStatus,
    OperationalAction,
    OperationalApproval,
    OperationalIssue,
    OperationalTask,
    Order,
    ProductVariant,
    ReturnCase,
    Shipment,
)
from app.services.operations.detectors import LOW_STOCK_THRESHOLD

MAX_LIMIT = 200


def _bounded(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def get_issue(
    db: Session, *, organization_id: uuid.UUID, issue_id: uuid.UUID
) -> OperationalIssue | None:
    return db.scalar(
        select(OperationalIssue).where(
            OperationalIssue.id == issue_id, OperationalIssue.organization_id == organization_id
        )
    )


def list_issues(
    db: Session,
    *,
    organization_id: uuid.UUID,
    status: IssueStatus | None = None,
    priority: IssuePriority | None = None,
    category: IssueCategory | None = None,
    assigned_user_id: uuid.UUID | None = None,
    breached_only: bool = False,
    limit: int = 100,
) -> list[OperationalIssue]:
    query = select(OperationalIssue).where(OperationalIssue.organization_id == organization_id)
    if status is not None:
        query = query.where(OperationalIssue.status == status)
    if priority is not None:
        query = query.where(OperationalIssue.priority == priority)
    if category is not None:
        query = query.where(OperationalIssue.category == category)
    if assigned_user_id is not None:
        query = query.where(OperationalIssue.assigned_user_id == assigned_user_id)
    if breached_only:
        query = query.where(OperationalIssue.breached_at.is_not(None))
    return list(
        db.scalars(query.order_by(OperationalIssue.created_at.desc()).limit(_bounded(limit)))
    )


def get_task(
    db: Session, *, organization_id: uuid.UUID, task_id: uuid.UUID
) -> OperationalTask | None:
    return db.scalar(
        select(OperationalTask).where(
            OperationalTask.id == task_id, OperationalTask.organization_id == organization_id
        )
    )


def get_action(
    db: Session, *, organization_id: uuid.UUID, action_id: uuid.UUID
) -> OperationalAction | None:
    return db.scalar(
        select(OperationalAction).where(
            OperationalAction.id == action_id, OperationalAction.organization_id == organization_id
        )
    )


def get_approval_for_action(db: Session, action: OperationalAction) -> OperationalApproval | None:
    if action.approval_id is None:
        return None
    return db.scalar(
        select(OperationalApproval).where(
            OperationalApproval.id == action.approval_id,
            OperationalApproval.organization_id == action.organization_id,
        )
    )


def list_orders_needing_attention(
    db: Session, *, organization_id: uuid.UUID, limit: int = 100
) -> list[dict]:
    """Orders that currently have at least one OPEN operational issue, newest issue first."""
    rows = db.execute(
        select(Order, OperationalIssue)
        .join(OperationalIssue, OperationalIssue.order_id == Order.id)
        .where(
            Order.organization_id == organization_id,
            OperationalIssue.organization_id == organization_id,
            OperationalIssue.status.not_in([IssueStatus.RESOLVED, IssueStatus.DISMISSED]),
        )
        .order_by(OperationalIssue.priority.asc(), OperationalIssue.created_at.desc())
        .limit(_bounded(limit))
    ).all()
    out = []
    for order, issue in rows:
        out.append(
            {
                "order_id": str(order.id),
                "order_number": order.order_number,
                "total": str(order.total) if order.total is not None else None,
                "financial_status": order.financial_status,
                "fulfillment_status": order.fulfillment_status,
                "issue_id": str(issue.id),
                "issue_category": issue.category.value,
                "issue_priority": issue.priority.value,
                "issue_title": issue.title,
            }
        )
    return out


def list_inventory_overview(
    db: Session, *, organization_id: uuid.UUID, limit: int = 100
) -> list[dict]:
    rows = db.scalars(
        select(ProductVariant)
        .where(
            ProductVariant.organization_id == organization_id, ProductVariant.deleted_at.is_(None)
        )
        .order_by(ProductVariant.inventory_quantity.asc().nulls_last())
        .limit(_bounded(limit))
    ).all()
    out = []
    for variant in rows:
        qty = variant.inventory_quantity
        out.append(
            {
                "variant_id": str(variant.id),
                "product_id": str(variant.product_id),
                "title": variant.title,
                "sku": variant.sku,
                "inventory_quantity": qty,
                "out_of_stock": qty is not None and qty == 0,
                "low_stock": qty is not None and 0 < qty <= LOW_STOCK_THRESHOLD,
            }
        )
    return out


def list_shipments(
    db: Session, *, organization_id: uuid.UUID, status: str | None = None, limit: int = 100
) -> list[Shipment]:
    query = select(Shipment).where(Shipment.organization_id == organization_id)
    if status is not None:
        query = query.where(Shipment.status == status)
    return list(db.scalars(query.order_by(Shipment.updated_at.desc()).limit(_bounded(limit))))


def list_payment_orders(
    db: Session, *, organization_id: uuid.UUID, limit: int = 100
) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .where(
                Order.organization_id == organization_id,
                Order.financial_status.in_(
                    ["pending", "voided", "expired", "partially_refunded", "refunded"]
                ),
            )
            .order_by(Order.updated_at_shopify.desc().nulls_last())
            .limit(_bounded(limit))
        )
    )


def list_return_cases(
    db: Session, *, organization_id: uuid.UUID, status: str | None = None, limit: int = 100
) -> list[ReturnCase]:
    query = select(ReturnCase).where(ReturnCase.organization_id == organization_id)
    if status is not None:
        query = query.where(ReturnCase.status == status)
    return list(db.scalars(query.order_by(ReturnCase.updated_at.desc()).limit(_bounded(limit))))
