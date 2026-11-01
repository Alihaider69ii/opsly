"""platform admin + brand environment (platform_audit_events, users.is_platform_admin,
organizations.environment/is_active)

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _id():
    return sa.Uuid()


def _enum(name, values, length=24):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "environment",
            _enum("organization_environment", ["DEMO", "PRODUCTION"], length=20),
            nullable=False,
            server_default="PRODUCTION",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "platform_audit_events",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "actor_user_id",
            _id(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_platform_audit_events_actor_user_id", "platform_audit_events", ["actor_user_id"]
    )
    op.create_index("ix_platform_audit_events_event_type", "platform_audit_events", ["event_type"])
    op.create_index(
        "ix_platform_audit_events_organization_id", "platform_audit_events", ["organization_id"]
    )
    op.create_index(
        "ix_platform_audit_events_org_created",
        "platform_audit_events",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("platform_audit_events")
    op.drop_column("organizations", "is_active")
    op.drop_column("organizations", "environment")
    op.drop_column("users", "is_platform_admin")
