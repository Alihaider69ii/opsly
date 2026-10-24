"""durable tenant-scoped automation engine

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _id():
    return sa.Uuid()


def _status(name, values):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def upgrade() -> None:
    op.add_column(
        "customers", sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
    )
    op.create_table(
        "automation_workflows",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column(
            "status",
            _status("workflow_status", ["DRAFT", "ACTIVE", "PAUSED", "ARCHIVED"]),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_automation_workflows_id_org"),
        sa.CheckConstraint("version > 0", name="ck_automation_workflows_version_positive"),
    )
    op.create_index(
        "ix_automation_workflows_organization_id", "automation_workflows", ["organization_id"]
    )
    op.create_index(
        "ix_automation_workflows_org_status", "automation_workflows", ["organization_id", "status"]
    )

    op.create_table(
        "automation_events",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(160)),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            _status("event_status", ["PENDING", "PROCESSING", "PROCESSED", "FAILED"]),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "organization_id", "dedupe_key", name="uq_automation_events_org_dedupe"
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_automation_events_id_org"),
    )
    op.create_index(
        "ix_automation_events_organization_id", "automation_events", ["organization_id"]
    )
    op.create_index(
        "ix_automation_events_status_created", "automation_events", ["status", "created_at"]
    )
    op.create_index(
        "ix_automation_events_org_type_created",
        "automation_events",
        ["organization_id", "event_type", "created_at"],
    )

    op.create_table(
        "automation_executions",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_id", _id(), nullable=False),
        sa.Column("event_id", _id(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("workflow_snapshot", sa.JSON(), nullable=False),
        sa.Column("event_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            _status(
                "execution_status",
                ["QUEUED", "RUNNING", "WAITING", "RETRYING", "COMPLETED", "FAILED", "CANCELLED"],
            ),
            nullable=False,
        ),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("error_code", sa.String(60)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["automation_workflows.id", "automation_workflows.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_executions_workflow_org",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["automation_events.id", "automation_events.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_executions_event_org",
        ),
        sa.UniqueConstraint(
            "workflow_id", "event_id", name="uq_automation_executions_workflow_event"
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_automation_executions_id_org"),
    )
    op.create_index(
        "ix_automation_executions_event_id", "automation_executions", ["event_id"]
    )
    op.create_index(
        "ix_automation_executions_organization_id", "automation_executions", ["organization_id"]
    )
    op.create_index(
        "ix_automation_executions_org_created",
        "automation_executions",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_automation_executions_status_next", "automation_executions", ["status", "next_run_at"]
    )
    op.create_index(
        "ix_automation_executions_next_run_at", "automation_executions", ["next_run_at"]
    )

    op.create_table(
        "automation_execution_steps",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_id", _id(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("action_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            _status(
                "execution_step_status",
                ["PENDING", "RUNNING", "WAITING", "RETRYING", "COMPLETED", "FAILED", "SKIPPED"],
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(60)),
        sa.Column("safe_result", sa.JSON()),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["automation_executions.id", "automation_executions.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_steps_execution_org",
        ),
        sa.UniqueConstraint(
            "execution_id", "step_index", name="uq_automation_steps_execution_index"
        ),
    )
    op.create_index(
        "ix_automation_execution_steps_organization_id",
        "automation_execution_steps",
        ["organization_id"],
    )
    op.create_index(
        "ix_automation_steps_status_scheduled",
        "automation_execution_steps",
        ["status", "scheduled_at"],
    )

    op.create_table(
        "website_leads",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", _id(), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(50)),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("marketing_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_source", sa.String(40), nullable=False, server_default="website_chat"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_website_leads_conversation_org",
        ),
        sa.UniqueConstraint("conversation_id", name="uq_website_leads_conversation"),
        sa.UniqueConstraint("id", "organization_id", name="uq_website_leads_id_org"),
    )
    op.create_index("ix_website_leads_organization_id", "website_leads", ["organization_id"])
    op.create_index(
        "ix_website_leads_org_created", "website_leads", ["organization_id", "created_at"]
    )

    op.create_table(
        "automation_tasks",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_id", _id(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["automation_executions.id", "automation_executions.organization_id"],
            ondelete="CASCADE",
            name="fk_automation_tasks_execution_org",
        ),
        sa.UniqueConstraint(
            "execution_id", "step_index", name="uq_automation_tasks_execution_step"
        ),
    )
    op.create_index("ix_automation_tasks_organization_id", "automation_tasks", ["organization_id"])
    op.create_index(
        "ix_automation_tasks_org_status", "automation_tasks", ["organization_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("automation_tasks")
    op.drop_table("website_leads")
    op.drop_table("automation_execution_steps")
    op.drop_index("ix_automation_executions_event_id", table_name="automation_executions")
    op.drop_table("automation_executions")
    op.drop_table("automation_events")
    op.drop_table("automation_workflows")
    op.drop_column("customers", "tags")
