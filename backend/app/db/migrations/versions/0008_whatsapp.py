"""whatsapp business messaging engine

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _id():
    return sa.Uuid()


def _status(name, values, length=20):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def upgrade() -> None:
    op.create_table(
        "whatsapp_connections",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _status("whatsapp_connection_status", ["CONNECTED", "ERROR", "DISCONNECTED"]),
            nullable=False,
        ),
        sa.Column("phone_number_id", sa.String(64), nullable=False),
        sa.Column("waba_id", sa.String(64), nullable=False),
        sa.Column("business_id", sa.String(64)),
        sa.Column("display_phone_number", sa.String(32)),
        sa.Column("verified_name", sa.String(255)),
        sa.Column("quality_rating", sa.String(20)),
        sa.Column("access_token_encrypted", sa.Text()),
        sa.Column("token_set_at", sa.DateTime(timezone=True)),
        sa.Column("api_version", sa.String(20), nullable=False),
        sa.Column("default_country", sa.String(2)),
        sa.Column("ai_auto_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_whatsapp_connections_id_org"),
        sa.UniqueConstraint("organization_id", name="uq_whatsapp_connections_organization_id"),
    )
    op.create_index(
        "ix_whatsapp_connections_organization_id", "whatsapp_connections", ["organization_id"]
    )
    op.create_index("ix_whatsapp_connections_status", "whatsapp_connections", ["status"])
    op.create_index(
        "uq_whatsapp_connections_active_phone",
        "whatsapp_connections",
        ["phone_number_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'DISCONNECTED'"),
        sqlite_where=sa.text("status <> 'DISCONNECTED'"),
    )

    op.create_table(
        "whatsapp_contacts",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone_e164", sa.String(20), nullable=False),
        sa.Column("wa_id", sa.String(32)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_whatsapp_contacts_id_org"),
        sa.UniqueConstraint("organization_id", "phone_e164", name="uq_whatsapp_contacts_org_phone"),
    )
    op.create_index(
        "ix_whatsapp_contacts_organization_id", "whatsapp_contacts", ["organization_id"]
    )

    op.create_table(
        "whatsapp_suppressions",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_id", _id(), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("lifted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["contact_id", "organization_id"],
            ["whatsapp_contacts.id", "whatsapp_contacts.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_suppressions_contact_org",
        ),
        sa.UniqueConstraint(
            "organization_id", "contact_id", name="uq_whatsapp_suppressions_org_contact"
        ),
    )
    op.create_index(
        "ix_whatsapp_suppressions_organization_id", "whatsapp_suppressions", ["organization_id"]
    )
    op.create_index(
        "ix_whatsapp_suppressions_contact_id", "whatsapp_suppressions", ["contact_id"]
    )

    op.create_table(
        "whatsapp_consents",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_id", _id(), nullable=False),
        sa.Column(
            "category",
            _status("whatsapp_consent_category", ["TRANSACTIONAL", "MARKETING", "SUPPORT"]),
            nullable=False,
        ),
        sa.Column(
            "status",
            _status("whatsapp_consent_status", ["OPTED_IN", "OPTED_OUT"]),
            nullable=False,
        ),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("safe_metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "organization_id"],
            ["whatsapp_contacts.id", "whatsapp_contacts.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_consents_contact_org",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "contact_id",
            "category",
            name="uq_whatsapp_consents_org_contact_category",
        ),
    )
    op.create_index(
        "ix_whatsapp_consents_organization_id", "whatsapp_consents", ["organization_id"]
    )
    op.create_index("ix_whatsapp_consents_contact_id", "whatsapp_consents", ["contact_id"])

    op.create_table(
        "whatsapp_conversations",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_id", _id(), nullable=False),
        sa.Column("connection_id", _id(), nullable=False),
        sa.Column(
            "status",
            _status("whatsapp_conversation_status", ["OPEN", "CLOSED"]),
            nullable=False,
        ),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
        sa.Column("window_expires_at", sa.DateTime(timezone=True)),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "organization_id"],
            ["whatsapp_contacts.id", "whatsapp_contacts.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_conversations_contact_org",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "organization_id"],
            ["whatsapp_connections.id", "whatsapp_connections.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_conversations_connection_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_whatsapp_conversations_id_org"),
        sa.UniqueConstraint(
            "organization_id",
            "contact_id",
            "connection_id",
            name="uq_whatsapp_conversations_org_contact_connection",
        ),
    )
    op.create_index(
        "ix_whatsapp_conversations_organization_id", "whatsapp_conversations", ["organization_id"]
    )
    op.create_index(
        "ix_whatsapp_conversations_contact_id", "whatsapp_conversations", ["contact_id"]
    )
    op.create_index(
        "ix_whatsapp_conversations_connection_id", "whatsapp_conversations", ["connection_id"]
    )
    op.create_index(
        "ix_whatsapp_conversations_window_expires_at",
        "whatsapp_conversations",
        ["window_expires_at"],
    )

    op.create_table(
        "whatsapp_messages",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", _id(), nullable=False),
        sa.Column(
            "direction",
            _status("whatsapp_message_direction", ["INBOUND", "OUTBOUND"]),
            nullable=False,
        ),
        sa.Column(
            "message_type",
            _status("whatsapp_message_type", ["TEXT", "TEMPLATE", "MEDIA"]),
            nullable=False,
        ),
        sa.Column(
            "status",
            _status(
                "whatsapp_message_status",
                [
                    "QUEUED",
                    "SENDING",
                    "SENT",
                    "DELIVERED",
                    "READ",
                    "FAILED",
                    "CANCELLED",
                    "RECEIVED",
                ],
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("content", sa.Text()),
        sa.Column("template_name", sa.String(512)),
        sa.Column("template_language", sa.String(20)),
        sa.Column("error_code", sa.String(50)),
        sa.Column("provider_error_detail", sa.String(300)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_metadata", sa.JSON()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["whatsapp_conversations.id", "whatsapp_conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_messages_conversation_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_whatsapp_messages_id_org"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_whatsapp_messages_org_idempotency"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "provider_message_id",
            name="uq_whatsapp_messages_org_provider_id",
        ),
    )
    op.create_index(
        "ix_whatsapp_messages_organization_id", "whatsapp_messages", ["organization_id"]
    )
    op.create_index(
        "ix_whatsapp_messages_conversation_id", "whatsapp_messages", ["conversation_id"]
    )
    op.create_index(
        "ix_whatsapp_messages_org_conversation_created",
        "whatsapp_messages",
        ["organization_id", "conversation_id"],
    )
    op.create_index("ix_whatsapp_messages_status", "whatsapp_messages", ["status"])

    op.create_table(
        "whatsapp_templates",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connection_id", _id(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("provider_template_id", sa.String(64)),
        sa.Column("components", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "organization_id"],
            ["whatsapp_connections.id", "whatsapp_connections.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_templates_connection_org",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "connection_id",
            "name",
            "language",
            name="uq_whatsapp_templates_org_connection_name_language",
        ),
    )
    op.create_index(
        "ix_whatsapp_templates_organization_id", "whatsapp_templates", ["organization_id"]
    )
    op.create_index(
        "ix_whatsapp_templates_connection_id", "whatsapp_templates", ["connection_id"]
    )

    op.create_table(
        "whatsapp_webhook_events",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("phone_number_id", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("organization_id", _id()),
        sa.Column("connection_id", _id()),
        sa.Column("resource_wamid", sa.String(128)),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "status",
            _status(
                "whatsapp_webhook_event_status",
                ["PENDING", "PROCESSING", "PROCESSED", "FAILED", "IGNORED"],
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(50)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "phone_number_id", "dedupe_key", name="uq_whatsapp_webhook_events_phone_dedupe"
        ),
    )
    op.create_index(
        "ix_whatsapp_webhook_events_organization_id",
        "whatsapp_webhook_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_whatsapp_webhook_events_connection_id", "whatsapp_webhook_events", ["connection_id"]
    )
    op.create_index(
        "ix_whatsapp_webhook_events_status_received",
        "whatsapp_webhook_events",
        ["status", "received_at"],
    )


def downgrade() -> None:
    op.drop_table("whatsapp_webhook_events")
    op.drop_table("whatsapp_templates")
    op.drop_table("whatsapp_messages")
    op.drop_table("whatsapp_conversations")
    op.drop_table("whatsapp_consents")
    op.drop_table("whatsapp_suppressions")
    op.drop_table("whatsapp_contacts")
    op.drop_table("whatsapp_connections")
