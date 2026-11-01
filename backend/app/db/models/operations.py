"""Phase 10: D2C Operations AI -- the normalized operational layer over existing commerce data.

This module does NOT duplicate Shopify-owned data. ``Order``/``OrderItem``/``Product``/
``ProductVariant``/``Customer`` (``app/db/models/commerce.py``) remain the system of record for
business facts; this module only adds the smallest normalized tables Shopify's own sync does not
yet populate (``Shipment`` for carrier/tracking/NDR/RTO state, ``ReturnCase`` for the return/
exchange/refund workflow) plus the operations-intelligence tables themselves (issues, tasks, SLA
definitions, actions, approvals, audit).

Payment and refund-pending signals are deliberately NOT a separate table: they are read directly
from ``Order.financial_status`` (see ``services/operations/detectors.py``) -- Shopify already
reports this, and duplicating it would risk drifting out of sync with the source of truth.
Similarly, inventory velocity is computed from existing ``OrderItem`` rows and
``ProductVariant.inventory_quantity`` rather than a new historical snapshot table (see
``docs/operations-architecture.md`` for the reuse-vs-new-table rationale on every entity).

Every table carries ``organization_id`` and every child row is tied to its parent by a composite
``(id, organization_id)`` foreign key, so a row can never reference another tenant's data -- the
same convention used throughout the rest of the schema (see ``app/db/scoping.py``).

AI is never a writer in this schema: every mutation path below is a deterministic service function
in ``app/services/operations/``; an LLM can only produce a recommendation string or request an
action, never update a row directly (Phase 10 brief sections 2, 34).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ShipmentStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    DELAYED = "DELAYED"
    NDR = "NDR"
    RTO = "RTO"
    LOST = "LOST"


class ReturnCaseType(enum.StrEnum):
    RETURN = "RETURN"
    EXCHANGE = "EXCHANGE"
    REFUND_ONLY = "REFUND_ONLY"


class ReturnCaseStatus(enum.StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    PICKUP_SCHEDULED = "PICKUP_SCHEDULED"
    PICKUP_FAILED = "PICKUP_FAILED"
    RECEIVED = "RECEIVED"
    REFUNDED = "REFUNDED"
    EXCHANGED = "EXCHANGED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class IssueCategory(enum.StrEnum):
    ORDER = "ORDER"
    SHIPPING = "SHIPPING"
    DELIVERY = "DELIVERY"
    NDR = "NDR"
    RTO = "RTO"
    RETURN = "RETURN"
    REFUND = "REFUND"
    EXCHANGE = "EXCHANGE"
    INVENTORY = "INVENTORY"
    WAREHOUSE = "WAREHOUSE"
    PAYMENT = "PAYMENT"
    FRAUD = "FRAUD"
    PRODUCT = "PRODUCT"
    PROCUREMENT = "PROCUREMENT"
    CUSTOMER = "CUSTOMER"
    SLA = "SLA"
    RECONCILIATION = "RECONCILIATION"
    OTHER = "OTHER"


class IssuePriority(enum.StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class IssueSeverity(enum.StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IssueStatus(enum.StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class DetectedBy(enum.StrEnum):
    """Who/what originated a row. Shared vocabulary across issues, tasks, actions and audit
    events so the dashboard can render provenance uniformly."""

    RULE = "RULE"
    AI = "AI"
    HUMAN = "HUMAN"
    INTEGRATION = "INTEGRATION"


class OperationsPrioritySource(enum.StrEnum):
    """Mirrors ``support.PrioritySource`` (same values, same rule: human priority is authoritative
    until another human changes it) but kept as its own enum -- Phase 9's table is untouched."""

    SYSTEM = "SYSTEM"
    AI = "AI"
    HUMAN = "HUMAN"


class TaskStatus(enum.StrEnum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class SLACategory(enum.StrEnum):
    CUSTOMER_SUPPORT = "CUSTOMER_SUPPORT"
    ORDER = "ORDER"
    FULFILLMENT = "FULFILLMENT"
    SHIPPING = "SHIPPING"
    NDR = "NDR"
    RTO = "RTO"
    RETURN = "RETURN"
    REFUND = "REFUND"
    INVENTORY = "INVENTORY"
    PAYMENT = "PAYMENT"


class ActionCategory(enum.StrEnum):
    """The deterministic policy class of an action -- never decided by the AI (brief section 19)."""

    SAFE_AUTOMATION = "SAFE_AUTOMATION"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    FORBIDDEN = "FORBIDDEN"


class ActionStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class ApprovalStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class RiskLevel(enum.StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def _enum(cls: type[enum.Enum], name: str, length: int = 24) -> Enum:
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name)


class Shipment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Carrier/tracking state for one order. Not Shopify-synced today (no courier/NDR webhook is
    wired up yet -- see ``docs/operations-architecture.md`` "known limitations"); populated by the
    demo seed and available for a future courier-aggregator connector to write into."""

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_shipments_id_organization_id"),
        ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="CASCADE",
            name="fk_shipments_order_org",
        ),
        Index("ix_shipments_org_status", "organization_id", "status"),
        Index("ix_shipments_org_order", "organization_id", "order_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    carrier: Mapped[str | None] = mapped_column(String(120))
    tracking_number: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[ShipmentStatus] = mapped_column(
        _enum(ShipmentStatus, "shipment_status"), default=ShipmentStatus.PENDING
    )
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ndr_count: Mapped[int] = mapped_column(Integer, default=0)
    ndr_reason: Mapped[str | None] = mapped_column(String(200))
    rto_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReturnCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "return_cases"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_return_cases_id_organization_id"),
        ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="CASCADE",
            name="fk_return_cases_order_org",
        ),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_return_cases_customer_org",
        ),
        Index("ix_return_cases_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    type: Mapped[ReturnCaseType] = mapped_column(_enum(ReturnCaseType, "return_case_type"))
    # Customer-submitted free text. Untrusted: never treated as instructions (brief section 35).
    reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[ReturnCaseStatus] = mapped_column(
        _enum(ReturnCaseStatus, "return_case_status"), default=ReturnCaseStatus.REQUESTED
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationalSLA(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """SLA catalog entry. Seeded per-organization (defaults created by the demo seed / a future
    onboarding step); evaluation is purely arithmetic, never AI (brief section 7)."""

    __tablename__ = "operational_slas"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_operational_slas_id_organization_id"),
        UniqueConstraint("organization_id", "name", name="uq_operational_slas_org_name"),
        Index("ix_operational_slas_org_category", "organization_id", "category"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[SLACategory] = mapped_column(_enum(SLACategory, "sla_category"))
    target_minutes: Mapped[int] = mapped_column(Integer)
    # NULL = applies to any priority within this category; a priority-specific row wins when both
    # match (see services/operations/sla.py:select_sla).
    priority: Mapped[IssuePriority | None] = mapped_column(_enum(IssuePriority, "sla_priority"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class OperationalIssue(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operational_issues"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_operational_issues_id_organization_id"),
        # Nullable dedupe_key: detector-created issues set it (so re-running a sweep updates rather
        # than duplicates); human-created issues leave it NULL and NULLs never collide.
        UniqueConstraint(
            "organization_id", "dedupe_key", name="uq_operational_issues_org_dedupe_key"
        ),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_issues_customer_org",
        ),
        ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_issues_order_org",
        ),
        ForeignKeyConstraint(
            ["sla_id", "organization_id"],
            ["operational_slas.id", "operational_slas.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_issues_sla_org",
        ),
        Index("ix_operational_issues_org_status", "organization_id", "status"),
        Index("ix_operational_issues_org_priority", "organization_id", "priority"),
        Index("ix_operational_issues_org_category", "organization_id", "category"),
        Index("ix_operational_issues_org_assigned", "organization_id", "assigned_user_id"),
        Index("ix_operational_issues_org_created", "organization_id", "created_at"),
        Index("ix_operational_issues_org_due", "organization_id", "due_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    category: Mapped[IssueCategory] = mapped_column(_enum(IssueCategory, "issue_category"))
    subcategory: Mapped[str | None] = mapped_column(String(60))
    priority: Mapped[IssuePriority] = mapped_column(
        _enum(IssuePriority, "issue_priority"), default=IssuePriority.NORMAL
    )
    priority_source: Mapped[OperationsPrioritySource] = mapped_column(
        _enum(OperationsPrioritySource, "issue_priority_source"),
        default=OperationsPrioritySource.SYSTEM,
    )
    severity: Mapped[IssueSeverity] = mapped_column(
        _enum(IssueSeverity, "issue_severity"), default=IssueSeverity.MEDIUM
    )
    status: Mapped[IssueStatus] = mapped_column(
        _enum(IssueStatus, "issue_status"), default=IssueStatus.OPEN
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(2000))
    # Bounded, structured, safe-to-display facts the detector observed -- never a raw DB dump and
    # never secrets/PII beyond what is already on the issue (brief section 5).
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detected_by: Mapped[DetectedBy] = mapped_column(_enum(DetectedBy, "issue_detected_by"))
    detector_code: Mapped[str | None] = mapped_column(String(80))
    recommended_action: Mapped[str | None] = mapped_column(String(1000))
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    sla_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Detector-owned idempotency key (e.g. "ORDER_UNFULFILLED_SLA:<order_id>"); NULL for
    # human/AI-created issues with no single re-detectable root cause.
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationalTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operational_tasks"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_operational_tasks_id_organization_id"),
        ForeignKeyConstraint(
            ["issue_id", "organization_id"],
            ["operational_issues.id", "operational_issues.organization_id"],
            ondelete="CASCADE",
            name="fk_operational_tasks_issue_org",
        ),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_tasks_customer_org",
        ),
        ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_tasks_order_org",
        ),
        Index("ix_operational_tasks_org_status", "organization_id", "status"),
        Index("ix_operational_tasks_org_priority", "organization_id", "priority"),
        Index("ix_operational_tasks_org_assigned", "organization_id", "assigned_user_id"),
        Index("ix_operational_tasks_org_issue", "organization_id", "issue_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    issue_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(2000))
    category: Mapped[IssueCategory] = mapped_column(_enum(IssueCategory, "task_category"))
    priority: Mapped[IssuePriority] = mapped_column(
        _enum(IssuePriority, "task_priority"), default=IssuePriority.NORMAL
    )
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"), default=TaskStatus.OPEN
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_by_type: Mapped[DetectedBy] = mapped_column(_enum(DetectedBy, "task_created_by_type"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(1000))


class OperationalApproval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operational_approvals"
    __table_args__ = (
        UniqueConstraint(
            "id", "organization_id", name="uq_operational_approvals_id_organization_id"
        ),
        Index("ix_operational_approvals_org_status", "organization_id", "status"),
        Index(
            "ix_operational_approvals_org_resource",
            "organization_id",
            "resource_type",
            "resource_id",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(60))
    resource_type: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    requested_by_type: Mapped[DetectedBy] = mapped_column(
        _enum(DetectedBy, "approval_requested_by_type"), default=DetectedBy.HUMAN
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), default=ApprovalStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationalAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The idempotency + execution ledger for every externally/financially meaningful operation
    (brief sections 19-21). ``category`` is resolved once, at request time, from the static policy
    map in ``services/operations/actions.py`` -- never decided by the caller or the AI."""

    __tablename__ = "operational_actions"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_operational_actions_id_organization_id"),
        UniqueConstraint(
            "organization_id",
            "action_type",
            "resource_type",
            "resource_id",
            "idempotency_key",
            name="uq_operational_actions_org_action_resource_idempotency",
        ),
        Index("ix_operational_actions_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(60))
    category: Mapped[ActionCategory] = mapped_column(_enum(ActionCategory, "action_category"))
    resource_type: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    requested_by_type: Mapped[DetectedBy] = mapped_column(
        _enum(DetectedBy, "action_requested_by_type")
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    status: Mapped[ActionStatus] = mapped_column(
        _enum(ActionStatus, "action_status"), default=ActionStatus.PENDING
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(60))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationalAuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. No secrets, no full sensitive payloads (brief section 22)."""

    __tablename__ = "operational_audit_events"
    __table_args__ = (
        Index("ix_operational_audit_events_org_created", "organization_id", "created_at"),
        Index(
            "ix_operational_audit_events_org_resource",
            "organization_id",
            "resource_type",
            "resource_id",
        ),
        Index("ix_operational_audit_events_org_event_type", "organization_id", "event_type"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    actor_type: Mapped[DetectedBy] = mapped_column(_enum(DetectedBy, "audit_actor_type"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(60))
    resource_type: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
