"""website chat: chat_widgets, conversations, messages

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21

Additive only: no existing table is altered, so Phase 1/2 data is untouched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "chat_widgets",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("welcome_message", sa.String(length=500), nullable=True),
        sa.Column("allowed_origins", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_chat_widgets_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_widgets")),
        sa.UniqueConstraint("organization_id", name=op.f("uq_chat_widgets_organization_id")),
    )
    op.create_index(op.f("ix_chat_widgets_public_key"), "chat_widgets", ["public_key"], unique=True)

    op.create_table(
        "conversations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "WEBSITE",
                name="channel",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("visitor_id", sa.String(length=64), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN", "CLOSED", name="status", native_enum=False, create_constraint=True, length=20
            ),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_conversations_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint(
            "id", "organization_id", name=op.f("uq_conversations_id_organization_id")
        ),
    )
    op.create_index(
        op.f("ix_conversations_organization_id"), "conversations", ["organization_id"], unique=False
    )
    op.create_index(
        op.f("ix_conversations_visitor_id"), "conversations", ["visitor_id"], unique=False
    )

    op.create_table(
        "messages",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "USER",
                "ASSISTANT",
                "SYSTEM",
                name="role",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_messages_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            name=op.f("fk_messages_conversation_org"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id", "position", name=op.f("uq_messages_conversation_position")
        ),
    )
    op.create_index(op.f("ix_messages_organization_id"), "messages", ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_organization_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_conversations_visitor_id"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_organization_id"), table_name="conversations")
    op.drop_table("conversations")
    op.drop_index(op.f("ix_chat_widgets_public_key"), table_name="chat_widgets")
    op.drop_table("chat_widgets")
