"""whatsapp ai conversation layer (phase 8): grounding/confidence/handoff policy

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _status(name, values, length=20):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def upgrade() -> None:
    op.add_column(
        "whatsapp_connections",
        sa.Column("ai_grounding_required", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column(
            "ai_min_confidence",
            _status("whatsapp_ai_confidence", ["HIGH", "MEDIUM", "LOW", "NONE"]),
            nullable=False,
            server_default="MEDIUM",
        ),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("ai_handoff_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("whatsapp_connections", sa.Column("ai_fallback_message", sa.String(1000)))
    op.add_column("whatsapp_connections", sa.Column("ai_handoff_message", sa.String(1000)))
    op.add_column(
        "whatsapp_connections",
        sa.Column("ai_max_replies_per_window", sa.Integer(), nullable=False, server_default="5"),
    )

    op.add_column(
        "whatsapp_conversations",
        sa.Column(
            "ai_state",
            _status(
                "whatsapp_ai_state", ["AI_ACTIVE", "HUMAN_REQUIRED", "HUMAN_ACTIVE", "RESOLVED"]
            ),
            nullable=False,
            server_default="AI_ACTIVE",
        ),
    )
    op.add_column("whatsapp_conversations", sa.Column("handoff_reason", sa.String(60)))
    op.add_column("whatsapp_conversations", sa.Column("handoff_at", sa.DateTime(timezone=True)))
    op.add_column(
        "whatsapp_conversations",
        sa.Column("ai_replies_in_window", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "whatsapp_conversations",
        sa.Column("ai_consecutive_fallbacks", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_whatsapp_conversations_org_ai_state",
        "whatsapp_conversations",
        ["organization_id", "ai_state"],
    )


def downgrade() -> None:
    op.drop_index("ix_whatsapp_conversations_org_ai_state", table_name="whatsapp_conversations")
    op.drop_column("whatsapp_conversations", "ai_consecutive_fallbacks")
    op.drop_column("whatsapp_conversations", "ai_replies_in_window")
    op.drop_column("whatsapp_conversations", "handoff_at")
    op.drop_column("whatsapp_conversations", "handoff_reason")
    op.drop_column("whatsapp_conversations", "ai_state")

    op.drop_column("whatsapp_connections", "ai_max_replies_per_window")
    op.drop_column("whatsapp_connections", "ai_handoff_message")
    op.drop_column("whatsapp_connections", "ai_fallback_message")
    op.drop_column("whatsapp_connections", "ai_handoff_enabled")
    op.drop_column("whatsapp_connections", "ai_min_confidence")
    op.drop_column("whatsapp_connections", "ai_grounding_required")
