"""phase 9: unified support layer + email support/automation/campaigns

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _id():
    return sa.Uuid()


def _status(name, values, length=20):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _user_fk():
    return sa.ForeignKey("users.id", ondelete="SET NULL")


SENDER_TYPES = ["CUSTOMER", "AI", "HUMAN", "SYSTEM"]


def upgrade() -> None:
    # --- customers: identity + email marketing consent/suppression --------------------------
    op.add_column("customers", sa.Column("normalized_email", sa.String(320)))
    op.add_column("customers", sa.Column("normalized_phone", sa.String(20)))
    op.add_column(
        "customers",
        sa.Column(
            "status",
            _status("customer_status", ["ACTIVE", "ARCHIVED"]),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.add_column("customers", sa.Column("email_marketing_consent", sa.Boolean()))
    op.add_column(
        "customers",
        sa.Column("email_suppressed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("customers", sa.Column("email_suppressed_reason", sa.String(60)))
    op.add_column("customers", sa.Column("email_suppressed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_customers_org_normalized_email", "customers", ["organization_id", "normalized_email"]
    )
    op.create_index(
        "ix_customers_org_normalized_phone", "customers", ["organization_id", "normalized_phone"]
    )

    # --- conversations (website chat): resolved customer identity ----------------------------
    op.create_index("ix_conversations_customer_id", "conversations", ["customer_id"])
    op.create_foreign_key(
        "fk_conversations_customer_org",
        "conversations",
        "customers",
        ["customer_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="SET NULL",
    )

    # --- messages (website chat): explicit sender attribution ---------------------------------
    op.add_column("messages", sa.Column("sender_type", _status("sender_type", SENDER_TYPES)))
    op.add_column("messages", sa.Column("sender_user_id", _id(), _user_fk()))
    op.create_index("ix_messages_sender_user_id", "messages", ["sender_user_id"])

    # --- whatsapp_messages: explicit sender attribution ----------------------------------------
    op.add_column(
        "whatsapp_messages", sa.Column("sender_type", _status("sender_type", SENDER_TYPES))
    )
    op.add_column("whatsapp_messages", sa.Column("sender_user_id", _id(), _user_fk()))
    op.create_index(
        "ix_whatsapp_messages_sender_user_id", "whatsapp_messages", ["sender_user_id"]
    )

    # --- support_conversations: the unified cross-channel index row --------------------------
    op.create_table(
        "support_conversations",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", _id()),
        sa.Column(
            "channel",
            _status("support_channel", ["WEBSITE", "WHATSAPP", "EMAIL"]),
            nullable=False,
        ),
        sa.Column("channel_conversation_id", _id(), nullable=False),
        sa.Column(
            "status",
            _status(
                "support_status",
                [
                    "OPEN",
                    "AI_ACTIVE",
                    "HUMAN_REQUIRED",
                    "HUMAN_ACTIVE",
                    "WAITING",
                    "RESOLVED",
                    "CLOSED",
                ],
            ),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column(
            "priority",
            _status("support_priority", ["URGENT", "HIGH", "NORMAL", "LOW"]),
            nullable=False,
            server_default="NORMAL",
        ),
        sa.Column(
            "priority_source",
            _status("support_priority_source", ["SYSTEM", "AI", "HUMAN"]),
            nullable=False,
            server_default="SYSTEM",
        ),
        sa.Column("priority_changed_by", _id(), _user_fk()),
        sa.Column("priority_changed_at", sa.DateTime(timezone=True)),
        sa.Column("priority_reason", sa.String(200)),
        sa.Column("assigned_agent_user_id", _id(), _user_fk()),
        sa.Column("assigned_at", sa.DateTime(timezone=True)),
        sa.Column("assigned_by", _id(), _user_fk()),
        sa.Column(
            "ai_state",
            _status(
                "support_ai_state", ["AI_ACTIVE", "HUMAN_REQUIRED", "HUMAN_ACTIVE", "RESOLVED"]
            ),
            nullable=False,
            server_default="AI_ACTIVE",
        ),
        sa.Column("handoff_reason", sa.String(60)),
        sa.Column("handoff_at", sa.DateTime(timezone=True)),
        sa.Column("last_customer_activity_at", sa.DateTime(timezone=True)),
        sa.Column("first_response_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("last_response_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_response_sender_type", _status("support_last_sender_type", SENDER_TYPES)
        ),
        sa.Column("last_message_preview", sa.String(300)),
        sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_support_conversations_customer_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_support_conversations_id_org"),
        sa.UniqueConstraint(
            "organization_id",
            "channel",
            "channel_conversation_id",
            name="uq_support_conversations_org_channel_conversation",
        ),
    )
    op.create_index(
        "ix_support_conversations_organization_id",
        "support_conversations",
        ["organization_id"],
    )
    op.create_index(
        "ix_support_conversations_customer_id", "support_conversations", ["customer_id"]
    )
    op.create_index(
        "ix_support_conversations_channel_conversation_id",
        "support_conversations",
        ["channel_conversation_id"],
    )
    op.create_index(
        "ix_support_conversations_priority_changed_by",
        "support_conversations",
        ["priority_changed_by"],
    )
    op.create_index(
        "ix_support_conversations_assigned_agent_user_id",
        "support_conversations",
        ["assigned_agent_user_id"],
    )
    op.create_index(
        "ix_support_conversations_assigned_by", "support_conversations", ["assigned_by"]
    )
    op.create_index(
        "ix_support_conversations_org_status", "support_conversations", ["organization_id", "status"]
    )
    op.create_index(
        "ix_support_conversations_org_priority",
        "support_conversations",
        ["organization_id", "priority"],
    )
    op.create_index(
        "ix_support_conversations_org_channel",
        "support_conversations",
        ["organization_id", "channel"],
    )
    op.create_index(
        "ix_support_conversations_org_assigned",
        "support_conversations",
        ["organization_id", "assigned_agent_user_id"],
    )
    op.create_index(
        "ix_support_conversations_org_updated",
        "support_conversations",
        ["organization_id", "updated_at"],
    )

    # --- support_timeline_events: customer timeline + audit trail + consent audit ------------
    op.create_table(
        "support_timeline_events",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", _id()),
        sa.Column("support_conversation_id", _id()),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="CASCADE",
            name="fk_support_timeline_events_customer_org",
        ),
        sa.ForeignKeyConstraint(
            ["support_conversation_id", "organization_id"],
            ["support_conversations.id", "support_conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_support_timeline_events_conversation_org",
        ),
    )
    op.create_index(
        "ix_support_timeline_events_organization_id",
        "support_timeline_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_support_timeline_events_customer_id", "support_timeline_events", ["customer_id"]
    )
    op.create_index(
        "ix_support_timeline_events_support_conversation_id",
        "support_timeline_events",
        ["support_conversation_id"],
    )
    op.create_index(
        "ix_support_timeline_events_org_customer_created",
        "support_timeline_events",
        ["organization_id", "customer_id", "created_at"],
    )
    op.create_index(
        "ix_support_timeline_events_org_conversation_created",
        "support_timeline_events",
        ["organization_id", "support_conversation_id", "created_at"],
    )

    # --- email_connections -----------------------------------------------------------------
    op.create_table(
        "email_connections",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _status("email_connection_status", ["CONNECTED", "ERROR", "DISCONNECTED"]),
            nullable=False,
        ),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("from_name", sa.String(255)),
        sa.Column("reply_to_address", sa.String(320)),
        sa.Column("inbound_address", sa.String(320), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_auto_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_grounding_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "ai_min_confidence",
            _status("email_ai_confidence", ["HIGH", "MEDIUM", "LOW", "NONE"]),
            nullable=False,
            server_default="MEDIUM",
        ),
        sa.Column("ai_handoff_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ai_fallback_message", sa.String(1000)),
        sa.Column("ai_handoff_message", sa.String(1000)),
        sa.Column(
            "ai_max_replies_per_window", sa.Integer(), nullable=False, server_default="5"
        ),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_email_connections_id_org"),
        sa.UniqueConstraint("organization_id", name="uq_email_connections_organization_id"),
        sa.UniqueConstraint("inbound_address", name="uq_email_connections_inbound_address"),
    )
    op.create_index(
        "ix_email_connections_organization_id", "email_connections", ["organization_id"]
    )
    op.create_index("ix_email_connections_status", "email_connections", ["status"])

    # --- email_templates (must precede email_campaigns/email_messages) ----------------------
    op.create_table(
        "email_templates",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column(
            "type",
            _status("email_template_type", ["TRANSACTIONAL", "LIFECYCLE", "MARKETING"]),
            nullable=False,
        ),
        sa.Column("subject", sa.String(998), nullable=False),
        sa.Column("preheader", sa.String(200)),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text()),
        sa.Column("variables", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "status",
            _status("email_template_status", ["DRAFT", "ACTIVE", "ARCHIVED"]),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", _id(), _user_fk()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_email_templates_id_org"),
    )
    op.create_index("ix_email_templates_organization_id", "email_templates", ["organization_id"])
    op.create_index("ix_email_templates_created_by", "email_templates", ["created_by"])
    op.create_index(
        "ix_email_templates_org_status", "email_templates", ["organization_id", "status"]
    )

    # --- email_campaigns (must precede email_messages/email_campaign_recipients) -------------
    op.create_table(
        "email_campaigns",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("template_id", _id(), nullable=False),
        sa.Column(
            "audience_definition", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "status",
            _status(
                "email_campaign_status",
                ["DRAFT", "SCHEDULED", "RUNNING", "PAUSED", "COMPLETED", "CANCELLED"],
            ),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", _id(), _user_fk()),
        sa.Column("total_recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "organization_id"],
            ["email_templates.id", "email_templates.organization_id"],
            name="fk_email_campaigns_template_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_email_campaigns_id_org"),
    )
    op.create_index("ix_email_campaigns_organization_id", "email_campaigns", ["organization_id"])
    op.create_index("ix_email_campaigns_template_id", "email_campaigns", ["template_id"])
    op.create_index("ix_email_campaigns_created_by", "email_campaigns", ["created_by"])
    op.create_index(
        "ix_email_campaigns_org_status", "email_campaigns", ["organization_id", "status"]
    )
    op.create_index(
        "ix_email_campaigns_status_scheduled", "email_campaigns", ["status", "scheduled_at"]
    )

    # --- email_threads (must precede email_messages) ------------------------------------------
    op.create_table(
        "email_threads",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", _id()),
        sa.Column("customer_email", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(998)),
        sa.Column("provider_thread_id", sa.String(128)),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
        sa.Column("ai_replies_in_window", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_consecutive_fallbacks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ai_state",
            _status(
                "email_ai_state", ["AI_ACTIVE", "HUMAN_REQUIRED", "HUMAN_ACTIVE", "RESOLVED"]
            ),
            nullable=False,
            server_default="AI_ACTIVE",
        ),
        sa.Column("handoff_reason", sa.String(60)),
        sa.Column("handoff_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_email_threads_customer_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_email_threads_id_org"),
    )
    op.create_index("ix_email_threads_organization_id", "email_threads", ["organization_id"])
    op.create_index("ix_email_threads_customer_id", "email_threads", ["customer_id"])
    op.create_index(
        "ix_email_threads_org_customer", "email_threads", ["organization_id", "customer_id"]
    )
    op.create_index(
        "ix_email_threads_org_ai_state", "email_threads", ["organization_id", "ai_state"]
    )

    # --- email_messages ----------------------------------------------------------------------
    op.create_table(
        "email_messages",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", _id(), nullable=False),
        sa.Column(
            "direction", _status("email_direction", ["INBOUND", "OUTBOUND"]), nullable=False
        ),
        sa.Column(
            "status",
            _status(
                "email_message_status",
                [
                    "QUEUED",
                    "SENDING",
                    "SENT",
                    "DELIVERED",
                    "BOUNCED",
                    "FAILED",
                    "COMPLAINT",
                    "UNSUBSCRIBED",
                    "RECEIVED",
                ],
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("message_id_header", sa.String(998)),
        sa.Column("in_reply_to_header", sa.String(998)),
        sa.Column("references_header", sa.Text()),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("to_addresses", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("cc_addresses", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("subject", sa.String(998)),
        sa.Column("body_text", sa.Text()),
        sa.Column("body_html_sanitized", sa.Text()),
        sa.Column(
            "classification",
            _status("email_classification", ["TRANSACTIONAL", "LIFECYCLE", "MARKETING"]),
        ),
        sa.Column("template_id", _id()),
        sa.Column("campaign_id", _id()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sender_type", _status("sender_type", SENDER_TYPES)),
        sa.Column("sender_user_id", _id(), _user_fk()),
        sa.Column("safe_metadata", sa.JSON()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("bounced_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("complained_at", sa.DateTime(timezone=True)),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id", "organization_id"],
            ["email_threads.id", "email_threads.organization_id"],
            ondelete="CASCADE",
            name="fk_email_messages_thread_org",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "organization_id"],
            ["email_templates.id", "email_templates.organization_id"],
            ondelete="SET NULL",
            name="fk_email_messages_template_org",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "organization_id"],
            ["email_campaigns.id", "email_campaigns.organization_id"],
            ondelete="SET NULL",
            name="fk_email_messages_campaign_org",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_email_messages_id_org"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_email_messages_org_idempotency"
        ),
        sa.UniqueConstraint(
            "organization_id", "provider_message_id", name="uq_email_messages_org_provider_id"
        ),
    )
    op.create_index("ix_email_messages_organization_id", "email_messages", ["organization_id"])
    op.create_index("ix_email_messages_thread_id", "email_messages", ["thread_id"])
    op.create_index("ix_email_messages_template_id", "email_messages", ["template_id"])
    op.create_index("ix_email_messages_campaign_id", "email_messages", ["campaign_id"])
    op.create_index("ix_email_messages_sender_user_id", "email_messages", ["sender_user_id"])
    op.create_index(
        "ix_email_messages_org_thread_created",
        "email_messages",
        ["organization_id", "thread_id"],
    )
    op.create_index(
        "ix_email_messages_org_message_id_header",
        "email_messages",
        ["organization_id", "message_id_header"],
    )
    op.create_index("ix_email_messages_status", "email_messages", ["status"])

    # --- email_campaign_recipients -------------------------------------------------------------
    op.create_table(
        "email_campaign_recipients",
        sa.Column("id", _id(), primary_key=True),
        sa.Column(
            "organization_id",
            _id(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("campaign_id", _id(), nullable=False),
        sa.Column("customer_id", _id(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column(
            "status",
            _status(
                "email_campaign_recipient_status",
                ["PENDING", "QUEUED", "SENT", "FAILED", "SKIPPED_SUPPRESSED"],
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("email_message_id", _id()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id", "organization_id"],
            ["email_campaigns.id", "email_campaigns.organization_id"],
            ondelete="CASCADE",
            name="fk_email_campaign_recipients_campaign_org",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "campaign_id",
            "customer_id",
            name="uq_email_campaign_recipients_org_campaign_customer",
        ),
    )
    op.create_index(
        "ix_email_campaign_recipients_organization_id",
        "email_campaign_recipients",
        ["organization_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_campaign_id",
        "email_campaign_recipients",
        ["campaign_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_customer_id",
        "email_campaign_recipients",
        ["customer_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_status", "email_campaign_recipients", ["status"]
    )

    # --- email_webhook_events ------------------------------------------------------------------
    op.create_table(
        "email_webhook_events",
        sa.Column("id", _id(), primary_key=True),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("organization_id", _id()),
        sa.Column("connection_id", _id()),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "status",
            _status(
                "email_webhook_event_status",
                ["PENDING", "PROCESSING", "PROCESSED", "FAILED", "IGNORED"],
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(50)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_email_webhook_events_dedupe"),
    )
    op.create_index(
        "ix_email_webhook_events_organization_id", "email_webhook_events", ["organization_id"]
    )
    op.create_index(
        "ix_email_webhook_events_connection_id", "email_webhook_events", ["connection_id"]
    )


def downgrade() -> None:
    op.drop_table("email_webhook_events")
    op.drop_table("email_campaign_recipients")
    op.drop_table("email_messages")
    op.drop_table("email_threads")
    op.drop_table("email_campaigns")
    op.drop_table("email_templates")
    op.drop_table("email_connections")
    op.drop_table("support_timeline_events")
    op.drop_table("support_conversations")

    op.drop_index("ix_whatsapp_messages_sender_user_id", table_name="whatsapp_messages")
    op.drop_column("whatsapp_messages", "sender_user_id")
    op.drop_column("whatsapp_messages", "sender_type")
    op.drop_index("ix_messages_sender_user_id", table_name="messages")
    op.drop_column("messages", "sender_user_id")
    op.drop_column("messages", "sender_type")

    op.drop_constraint("fk_conversations_customer_org", "conversations", type_="foreignkey")
    op.drop_index("ix_conversations_customer_id", table_name="conversations")

    op.drop_index("ix_customers_org_normalized_phone", table_name="customers")
    op.drop_index("ix_customers_org_normalized_email", table_name="customers")
    op.drop_column("customers", "email_suppressed_at")
    op.drop_column("customers", "email_suppressed_reason")
    op.drop_column("customers", "email_suppressed")
    op.drop_column("customers", "email_marketing_consent")
    op.drop_column("customers", "status")
    op.drop_column("customers", "normalized_phone")
    op.drop_column("customers", "normalized_email")
