"""phase 10: D2C operations AI (issues, tasks, SLA, approvals, actions, audit, shipments, returns)

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _id():
    return sa.Uuid()


def _enum(name, values, length=24):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _user_fk():
    return sa.ForeignKey("users.id", ondelete="SET NULL")


def _org_fk():
    return sa.ForeignKey("organizations.id", ondelete="CASCADE")


ISSUE_CATEGORY = [
    "ORDER",
    "SHIPPING",
    "DELIVERY",
    "NDR",
    "RTO",
    "RETURN",
    "REFUND",
    "EXCHANGE",
    "INVENTORY",
    "WAREHOUSE",
    "PAYMENT",
    "FRAUD",
    "PRODUCT",
    "PROCUREMENT",
    "CUSTOMER",
    "SLA",
    "RECONCILIATION",
    "OTHER",
]
ISSUE_PRIORITY = ["URGENT", "HIGH", "NORMAL", "LOW"]
DETECTED_BY = ["RULE", "AI", "HUMAN", "INTEGRATION"]


def upgrade() -> None:
    # --- shipments ---------------------------------------------------------------------------
    op.create_table(
        "shipments",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("order_id", _id(), nullable=False),
        sa.Column("carrier", sa.String(120)),
        sa.Column("tracking_number", sa.String(120)),
        sa.Column(
            "status",
            _enum(
                "shipment_status",
                [
                    "PENDING",
                    "IN_TRANSIT",
                    "OUT_FOR_DELIVERY",
                    "DELIVERED",
                    "DELAYED",
                    "NDR",
                    "RTO",
                    "LOST",
                ],
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("shipped_at", sa.DateTime(timezone=True)),
        sa.Column("estimated_delivery_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_status_at", sa.DateTime(timezone=True)),
        sa.Column("ndr_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ndr_reason", sa.String(200)),
        sa.Column("rto_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="CASCADE",
            name="fk_shipments_order_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_shipments_id_organization_id"),
    )
    op.create_index("ix_shipments_organization_id", "shipments", ["organization_id"])
    op.create_index("ix_shipments_order_id", "shipments", ["order_id"])
    op.create_index("ix_shipments_org_status", "shipments", ["organization_id", "status"])
    op.create_index("ix_shipments_org_order", "shipments", ["organization_id", "order_id"])

    # --- return_cases --------------------------------------------------------------------------
    op.create_table(
        "return_cases",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("order_id", _id(), nullable=False),
        sa.Column("customer_id", _id()),
        sa.Column(
            "type", _enum("return_case_type", ["RETURN", "EXCHANGE", "REFUND_ONLY"]), nullable=False
        ),
        sa.Column("reason", sa.String(500)),
        sa.Column(
            "status",
            _enum(
                "return_case_status",
                [
                    "REQUESTED",
                    "APPROVED",
                    "PICKUP_SCHEDULED",
                    "PICKUP_FAILED",
                    "RECEIVED",
                    "REFUNDED",
                    "EXCHANGED",
                    "REJECTED",
                    "CANCELLED",
                ],
            ),
            nullable=False,
            server_default="REQUESTED",
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="CASCADE",
            name="fk_return_cases_order_org",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_return_cases_customer_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_return_cases_id_organization_id"),
    )
    op.create_index("ix_return_cases_organization_id", "return_cases", ["organization_id"])
    op.create_index("ix_return_cases_order_id", "return_cases", ["order_id"])
    op.create_index("ix_return_cases_customer_id", "return_cases", ["customer_id"])
    op.create_index("ix_return_cases_org_status", "return_cases", ["organization_id", "status"])

    # --- operational_slas ------------------------------------------------------------------------
    op.create_table(
        "operational_slas",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "category",
            _enum(
                "sla_category",
                [
                    "CUSTOMER_SUPPORT",
                    "ORDER",
                    "FULFILLMENT",
                    "SHIPPING",
                    "NDR",
                    "RTO",
                    "RETURN",
                    "REFUND",
                    "INVENTORY",
                    "PAYMENT",
                ],
            ),
            nullable=False,
        ),
        sa.Column("target_minutes", sa.Integer(), nullable=False),
        sa.Column("priority", _enum("sla_priority", ISSUE_PRIORITY)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_operational_slas_id_organization_id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_operational_slas_org_name"),
    )
    op.create_index("ix_operational_slas_organization_id", "operational_slas", ["organization_id"])
    op.create_index(
        "ix_operational_slas_org_category", "operational_slas", ["organization_id", "category"]
    )

    # --- operational_issues ---------------------------------------------------------------------
    op.create_table(
        "operational_issues",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("customer_id", _id()),
        sa.Column("order_id", _id()),
        sa.Column("category", _enum("issue_category", ISSUE_CATEGORY), nullable=False),
        sa.Column("subcategory", sa.String(60)),
        sa.Column(
            "priority",
            _enum("issue_priority", ISSUE_PRIORITY),
            nullable=False,
            server_default="NORMAL",
        ),
        sa.Column(
            "priority_source",
            _enum("issue_priority_source", ["SYSTEM", "AI", "HUMAN"]),
            nullable=False,
            server_default="SYSTEM",
        ),
        sa.Column(
            "severity",
            _enum("issue_severity", ["CRITICAL", "HIGH", "MEDIUM", "LOW"]),
            nullable=False,
            server_default="MEDIUM",
        ),
        sa.Column(
            "status",
            _enum(
                "issue_status",
                [
                    "OPEN",
                    "ACKNOWLEDGED",
                    "IN_PROGRESS",
                    "WAITING",
                    "ESCALATED",
                    "RESOLVED",
                    "DISMISSED",
                ],
            ),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("detected_by", _enum("issue_detected_by", DETECTED_BY), nullable=False),
        sa.Column("detector_code", sa.String(80)),
        sa.Column("recommended_action", sa.String(1000)),
        sa.Column("assigned_user_id", _id(), _user_fk()),
        sa.Column("sla_id", _id()),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("breached_at", sa.DateTime(timezone=True)),
        sa.Column("dedupe_key", sa.String(300)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_issues_customer_org",
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_issues_order_org",
        ),
        sa.ForeignKeyConstraint(
            ["sla_id", "organization_id"],
            ["operational_slas.id", "operational_slas.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_issues_sla_org",
        ),
        sa.UniqueConstraint(
            "id", "organization_id", name="uq_operational_issues_id_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id", "dedupe_key", name="uq_operational_issues_org_dedupe_key"
        ),
    )
    op.create_index(
        "ix_operational_issues_organization_id", "operational_issues", ["organization_id"]
    )
    op.create_index("ix_operational_issues_customer_id", "operational_issues", ["customer_id"])
    op.create_index("ix_operational_issues_order_id", "operational_issues", ["order_id"])
    op.create_index(
        "ix_operational_issues_assigned_user_id", "operational_issues", ["assigned_user_id"]
    )
    op.create_index("ix_operational_issues_sla_id", "operational_issues", ["sla_id"])
    op.create_index(
        "ix_operational_issues_org_status", "operational_issues", ["organization_id", "status"]
    )
    op.create_index(
        "ix_operational_issues_org_priority", "operational_issues", ["organization_id", "priority"]
    )
    op.create_index(
        "ix_operational_issues_org_category", "operational_issues", ["organization_id", "category"]
    )
    op.create_index(
        "ix_operational_issues_org_assigned",
        "operational_issues",
        ["organization_id", "assigned_user_id"],
    )
    op.create_index(
        "ix_operational_issues_org_created", "operational_issues", ["organization_id", "created_at"]
    )
    op.create_index(
        "ix_operational_issues_org_due", "operational_issues", ["organization_id", "due_at"]
    )

    # --- operational_tasks -----------------------------------------------------------------------
    op.create_table(
        "operational_tasks",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("issue_id", _id()),
        sa.Column("customer_id", _id()),
        sa.Column("order_id", _id()),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("category", _enum("task_category", ISSUE_CATEGORY), nullable=False),
        sa.Column(
            "priority",
            _enum("task_priority", ISSUE_PRIORITY),
            nullable=False,
            server_default="NORMAL",
        ),
        sa.Column(
            "status",
            _enum(
                "task_status",
                [
                    "OPEN",
                    "ASSIGNED",
                    "IN_PROGRESS",
                    "WAITING",
                    "ESCALATED",
                    "RESOLVED",
                    "CANCELLED",
                ],
            ),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("assigned_user_id", _id(), _user_fk()),
        sa.Column("created_by_type", _enum("task_created_by_type", DETECTED_BY), nullable=False),
        sa.Column("created_by_user_id", _id(), _user_fk()),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["issue_id", "organization_id"],
            ["operational_issues.id", "operational_issues.organization_id"],
            ondelete="CASCADE",
            name="fk_operational_tasks_issue_org",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_tasks_customer_org",
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="SET NULL",
            name="fk_operational_tasks_order_org",
        ),
        sa.UniqueConstraint(
            "id", "organization_id", name="uq_operational_tasks_id_organization_id"
        ),
    )
    op.create_index(
        "ix_operational_tasks_organization_id", "operational_tasks", ["organization_id"]
    )
    op.create_index("ix_operational_tasks_issue_id", "operational_tasks", ["issue_id"])
    op.create_index("ix_operational_tasks_customer_id", "operational_tasks", ["customer_id"])
    op.create_index("ix_operational_tasks_order_id", "operational_tasks", ["order_id"])
    op.create_index(
        "ix_operational_tasks_assigned_user_id", "operational_tasks", ["assigned_user_id"]
    )
    op.create_index(
        "ix_operational_tasks_created_by_user_id", "operational_tasks", ["created_by_user_id"]
    )
    op.create_index(
        "ix_operational_tasks_org_status", "operational_tasks", ["organization_id", "status"]
    )
    op.create_index(
        "ix_operational_tasks_org_priority", "operational_tasks", ["organization_id", "priority"]
    )
    op.create_index(
        "ix_operational_tasks_org_assigned",
        "operational_tasks",
        ["organization_id", "assigned_user_id"],
    )
    op.create_index(
        "ix_operational_tasks_org_issue", "operational_tasks", ["organization_id", "issue_id"]
    )

    # --- operational_approvals -------------------------------------------------------------------
    op.create_table(
        "operational_approvals",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("action_type", sa.String(60), nullable=False),
        sa.Column("resource_type", sa.String(60), nullable=False),
        sa.Column("resource_id", _id()),
        sa.Column(
            "requested_by_type",
            _enum("approval_requested_by_type", DETECTED_BY),
            nullable=False,
            server_default="HUMAN",
        ),
        sa.Column("requested_by", _id(), _user_fk()),
        sa.Column(
            "status",
            _enum("approval_status", ["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("reason", sa.String(500)),
        sa.Column("payload", sa.JSON()),
        sa.Column("approved_by", _id(), _user_fk()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_by", _id(), _user_fk()),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "id", "organization_id", name="uq_operational_approvals_id_organization_id"
        ),
    )
    op.create_index(
        "ix_operational_approvals_organization_id", "operational_approvals", ["organization_id"]
    )
    op.create_index(
        "ix_operational_approvals_resource_id", "operational_approvals", ["resource_id"]
    )
    op.create_index(
        "ix_operational_approvals_requested_by", "operational_approvals", ["requested_by"]
    )
    op.create_index(
        "ix_operational_approvals_approved_by", "operational_approvals", ["approved_by"]
    )
    op.create_index(
        "ix_operational_approvals_rejected_by", "operational_approvals", ["rejected_by"]
    )
    op.create_index(
        "ix_operational_approvals_org_status",
        "operational_approvals",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_operational_approvals_org_resource",
        "operational_approvals",
        ["organization_id", "resource_type", "resource_id"],
    )

    # --- operational_actions ---------------------------------------------------------------------
    op.create_table(
        "operational_actions",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("action_type", sa.String(60), nullable=False),
        sa.Column(
            "category",
            _enum("action_category", ["SAFE_AUTOMATION", "REQUIRES_APPROVAL", "FORBIDDEN"]),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(60), nullable=False),
        sa.Column("resource_id", _id()),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column(
            "requested_by_type", _enum("action_requested_by_type", DETECTED_BY), nullable=False
        ),
        sa.Column("requested_by_user_id", _id(), _user_fk()),
        sa.Column("approval_id", _id()),
        sa.Column(
            "status",
            _enum("action_status", ["PENDING", "APPROVED", "REJECTED", "EXECUTED", "FAILED"]),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("payload", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("error_code", sa.String(60)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "id", "organization_id", name="uq_operational_actions_id_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "action_type",
            "resource_type",
            "resource_id",
            "idempotency_key",
            name="uq_operational_actions_org_action_resource_idempotency",
        ),
    )
    op.create_index(
        "ix_operational_actions_organization_id", "operational_actions", ["organization_id"]
    )
    op.create_index("ix_operational_actions_resource_id", "operational_actions", ["resource_id"])
    op.create_index("ix_operational_actions_approval_id", "operational_actions", ["approval_id"])
    op.create_index(
        "ix_operational_actions_requested_by_user_id",
        "operational_actions",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_operational_actions_org_status", "operational_actions", ["organization_id", "status"]
    )

    # --- operational_audit_events ----------------------------------------------------------------
    op.create_table(
        "operational_audit_events",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("organization_id", _id(), _org_fk(), nullable=False),
        sa.Column("actor_type", _enum("audit_actor_type", DETECTED_BY), nullable=False),
        sa.Column("actor_id", _id()),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("resource_type", sa.String(60), nullable=False),
        sa.Column("resource_id", _id()),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_operational_audit_events_organization_id",
        "operational_audit_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_operational_audit_events_resource_id", "operational_audit_events", ["resource_id"]
    )
    op.create_index(
        "ix_operational_audit_events_org_created",
        "operational_audit_events",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_operational_audit_events_org_resource",
        "operational_audit_events",
        ["organization_id", "resource_type", "resource_id"],
    )
    op.create_index(
        "ix_operational_audit_events_org_event_type",
        "operational_audit_events",
        ["organization_id", "event_type"],
    )


def downgrade() -> None:
    op.drop_table("operational_audit_events")
    op.drop_table("operational_actions")
    op.drop_table("operational_approvals")
    op.drop_table("operational_tasks")
    op.drop_table("operational_issues")
    op.drop_table("operational_slas")
    op.drop_table("return_cases")
    op.drop_table("shipments")
