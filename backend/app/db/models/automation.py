"""Tenant-owned workflow definitions and durable automation ledgers.

Redis carries wakeups only. Events, snapshots, claims, delayed steps and outcomes live here.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
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

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class EventStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class ExecutionStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


def _enum(cls: type[enum.Enum], name: str, length: int = 20) -> Enum:
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name)


class AutomationWorkflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "automation_workflows"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_automation_workflows_id_org"),
        Index("ix_automation_workflows_org_status", "organization_id", "status"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[WorkflowStatus] = mapped_column(
        _enum(WorkflowStatus, "workflow_status"), default=WorkflowStatus.DRAFT, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Definition is schema-validated at every write and again before execution.
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WebsiteLead(UUIDPrimaryKeyMixin, Base):
    """A contact explicitly supplied during a website conversation."""

    __tablename__ = "website_leads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_website_leads_conversation_org",
        ),
        UniqueConstraint("conversation_id", name="uq_website_leads_conversation"),
        UniqueConstraint("id", "organization_id", name="uq_website_leads_id_org"),
        Index("ix_website_leads_org_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    marketing_consent: Mapped[bool] = mapped_column(nullable=False, default=False)
    consent_source: Mapped[str] = mapped_column(String(40), nullable=False, default="website_chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AutomationTask(UUIDPrimaryKeyMixin, Base):
    """Persisted internal follow-up task created by an automation step."""

    __tablename__ = "automation_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["automation_executions.id", "automation_executions.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_tasks_execution_org",
        ),
        UniqueConstraint("execution_id", "step_index", name="uq_automation_tasks_execution_step"),
        Index("ix_automation_tasks_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AutomationEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "automation_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "dedupe_key", name="uq_automation_events_org_dedupe"),
        UniqueConstraint("id", "organization_id", name="uq_automation_events_id_org"),
        Index("ix_automation_events_status_created", "status", "created_at"),
        Index(
            "ix_automation_events_org_type_created", "organization_id", "event_type", "created_at"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(160))
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[EventStatus] = mapped_column(
        _enum(EventStatus, "event_status"), default=EventStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationExecution(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "automation_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["automation_workflows.id", "automation_workflows.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_executions_workflow_org",
        ),
        ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["automation_events.id", "automation_events.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_executions_event_org",
        ),
        UniqueConstraint("workflow_id", "event_id", name="uq_automation_executions_workflow_event"),
        UniqueConstraint("id", "organization_id", name="uq_automation_executions_id_org"),
        Index("ix_automation_executions_org_created", "organization_id", "created_at"),
        Index("ix_automation_executions_status_next", "status", "next_run_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workflow_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    event_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus, "execution_status"), default=ExecutionStatus.QUEUED, nullable=False
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AutomationExecutionStep(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "automation_execution_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["automation_executions.id", "automation_executions.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_steps_execution_org",
        ),
        UniqueConstraint("execution_id", "step_index", name="uq_automation_steps_execution_index"),
        Index("ix_automation_steps_status_scheduled", "status", "scheduled_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    action_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        _enum(StepStatus, "execution_step_status"), default=StepStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60))
    safe_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
