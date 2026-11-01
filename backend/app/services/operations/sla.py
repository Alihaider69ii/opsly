"""Deterministic SLA catalog + evaluator (brief section 7). No LLM involved anywhere here.

Selection rule when more than one SLA could apply to a category: an SLA row scoped to the issue's
exact priority wins over a category-wide (``priority IS NULL``) row -- see ``select_sla``.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IssueCategory, IssuePriority, OperationalSLA, SLACategory

# Every IssueCategory maps to exactly one SLACategory bucket (brief sections 2 and 7 keep these as
# two separate, smaller vocabularies rather than one combined enum). Categories with no dedicated
# SLA bucket fall back to the closest operational analog.
ISSUE_CATEGORY_TO_SLA_CATEGORY: dict[IssueCategory, SLACategory] = {
    IssueCategory.ORDER: SLACategory.ORDER,
    IssueCategory.SHIPPING: SLACategory.SHIPPING,
    IssueCategory.DELIVERY: SLACategory.SHIPPING,
    IssueCategory.NDR: SLACategory.NDR,
    IssueCategory.RTO: SLACategory.RTO,
    IssueCategory.RETURN: SLACategory.RETURN,
    IssueCategory.REFUND: SLACategory.REFUND,
    IssueCategory.EXCHANGE: SLACategory.RETURN,
    IssueCategory.INVENTORY: SLACategory.INVENTORY,
    IssueCategory.WAREHOUSE: SLACategory.FULFILLMENT,
    IssueCategory.PAYMENT: SLACategory.PAYMENT,
    IssueCategory.FRAUD: SLACategory.ORDER,
    IssueCategory.PRODUCT: SLACategory.INVENTORY,
    IssueCategory.PROCUREMENT: SLACategory.INVENTORY,
    IssueCategory.CUSTOMER: SLACategory.CUSTOMER_SUPPORT,
    IssueCategory.SLA: SLACategory.ORDER,
    IssueCategory.RECONCILIATION: SLACategory.PAYMENT,
    IssueCategory.OTHER: SLACategory.ORDER,
}

# Seeded once per organization (idempotent -- see ensure_default_slas). Minutes.
DEFAULT_SLA_DEFINITIONS: tuple[tuple[str, SLACategory, int, IssuePriority | None], ...] = (
    ("Customer support first response", SLACategory.CUSTOMER_SUPPORT, 60, None),
    ("Order fulfillment (urgent)", SLACategory.FULFILLMENT, 24 * 60, IssuePriority.URGENT),
    ("Order fulfillment (standard)", SLACategory.FULFILLMENT, 48 * 60, None),
    ("Order attention window", SLACategory.ORDER, 24 * 60, None),
    ("Shipping delay response", SLACategory.SHIPPING, 24 * 60, None),
    ("NDR follow-up", SLACategory.NDR, 12 * 60, None),
    ("RTO review", SLACategory.RTO, 24 * 60, None),
    ("Return pickup window", SLACategory.RETURN, 48 * 60, None),
    ("Refund processing", SLACategory.REFUND, 72 * 60, None),
    ("Inventory alert review", SLACategory.INVENTORY, 24 * 60, None),
    ("Payment issue follow-up", SLACategory.PAYMENT, 12 * 60, None),
)


def ensure_default_slas(db: Session, organization_id: uuid.UUID) -> list[OperationalSLA]:
    """Create the default SLA catalog for an organization if it has none yet. Idempotent and
    race-safe (a unique constraint on (organization_id, name) backs it)."""
    existing = list(
        db.scalars(select(OperationalSLA).where(OperationalSLA.organization_id == organization_id))
    )
    if existing:
        return existing
    created = []
    for name, category, target_minutes, priority in DEFAULT_SLA_DEFINITIONS:
        row = OperationalSLA(
            id=uuid.uuid4(),
            organization_id=organization_id,
            name=name,
            category=category,
            target_minutes=target_minutes,
            priority=priority,
            enabled=True,
        )
        db.add(row)
        created.append(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return list(
            db.scalars(
                select(OperationalSLA).where(OperationalSLA.organization_id == organization_id)
            )
        )
    return created


def select_sla(
    db: Session, *, organization_id: uuid.UUID, category: SLACategory, priority: IssuePriority
) -> OperationalSLA | None:
    rows = list(
        db.scalars(
            select(OperationalSLA).where(
                OperationalSLA.organization_id == organization_id,
                OperationalSLA.category == category,
                OperationalSLA.enabled.is_(True),
            )
        )
    )
    exact = next((row for row in rows if row.priority == priority), None)
    if exact is not None:
        return exact
    return next((row for row in rows if row.priority is None), None)


@dataclass(frozen=True)
class SLAEvaluation:
    due_at: datetime | None
    breached: bool
    remaining_minutes: float | None


def evaluate(
    sla: OperationalSLA | None, *, started_at: datetime, now: datetime | None = None
) -> SLAEvaluation:
    now = now or datetime.now(UTC)
    if sla is None:
        return SLAEvaluation(due_at=None, breached=False, remaining_minutes=None)
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    due_at = started + timedelta(minutes=sla.target_minutes)
    remaining = (due_at - now).total_seconds() / 60
    return SLAEvaluation(due_at=due_at, breached=remaining < 0, remaining_minutes=remaining)
