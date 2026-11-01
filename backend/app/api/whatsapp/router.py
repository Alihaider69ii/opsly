"""WhatsApp dashboard API: conversations, message history, templates and contact consent.

Read-only for conversations and messages: Phase 6 has no dashboard compose-and-send -- every
outbound WhatsApp message is created by the automation engine (see
docs/phase-6-progress.md "Architecture Decisions"). Authenticated; the organization is always the
caller's verified one. Reading is open to every member; template sync and consent/suppression
writes need OWNER or ADMIN.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.core.config import Settings, get_settings
from app.core.rate_limit import rate_limit
from app.core.secrets import SecretError, SecretManager
from app.db.models import (
    AutomationWorkflow,
    OrganizationRole,
    WhatsAppConnection,
    WhatsAppConnectionStatus,
    WhatsAppConsentCategory,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppTemplate,
)
from app.schemas.whatsapp import (
    AutomationUsageOut,
    ConsentIn,
    ConsentListOut,
    ConsentOut,
    ConversationOut,
    MessageOut,
    OptOutIn,
    TemplateOut,
    TemplateSyncOut,
)
from app.services.messaging.errors import MessagingError
from app.services.messaging.providers.whatsapp_cloud import fetch_templates
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import handoff as handoff_service
from app.services.messaging.whatsapp import templates as template_service
from app.services.messaging.whatsapp.send import SECRET_CONTEXT
from app.services.organizations.service import OrgContext

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

AppSettings = Annotated[Settings, Depends(get_settings)]
ManagerOrg = Annotated[
    OrgContext,
    Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
]

_template_sync_limit = rate_limit(
    "whatsapp-template-sync",
    limit=lambda s: s.whatsapp_template_sync_rate_limit,
    window=lambda s: 60,
    detail="Too many template sync attempts. Please wait a moment.",
)

ACTION_TYPES_SHOWN = ("send_whatsapp_text", "send_whatsapp_template")


# --- conversations / messages --------------------------------------------------------------------


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(ctx: CurrentOrg, db: DbSession) -> list[WhatsAppConversation]:
    return list(
        db.scalars(
            select(WhatsAppConversation)
            .where(WhatsAppConversation.organization_id == ctx.organization.id)
            .order_by(WhatsAppConversation.updated_at.desc())
            .limit(200)
        )
    )


def _conversation(
    db: DbSession, ctx: OrgContext, conversation_id: uuid.UUID
) -> WhatsAppConversation:
    row = db.scalar(
        select(WhatsAppConversation).where(
            WhatsAppConversation.id == conversation_id,
            WhatsAppConversation.organization_id == ctx.organization.id,
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return row


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession
) -> WhatsAppConversation:
    return _conversation(db, ctx, conversation_id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    conversation_id: uuid.UUID, ctx: CurrentOrg, db: DbSession
) -> list[WhatsAppMessage]:
    _conversation(db, ctx, conversation_id)  # 404s before a cross-tenant id reveals anything
    return list(
        db.scalars(
            select(WhatsAppMessage)
            .where(
                WhatsAppMessage.organization_id == ctx.organization.id,
                WhatsAppMessage.conversation_id == conversation_id,
            )
            .order_by(WhatsAppMessage.created_at.asc())
            .limit(500)
        )
    )


# --- human handoff (Phase 8) -----------------------------------------------------------------


@router.post("/conversations/{conversation_id}/handoff/claim", response_model=ConversationOut)
def claim_handoff(
    conversation_id: uuid.UUID, ctx: ManagerOrg, db: DbSession
) -> WhatsAppConversation:
    """A human claims a HUMAN_REQUIRED conversation. Optional -- a human may ``resolve`` directly
    without claiming first; this just lets the dashboard show who is working on what."""
    conversation = _conversation(db, ctx, conversation_id)
    handoff_service.claim(db, conversation=conversation)
    db.refresh(conversation)
    return conversation


@router.post("/conversations/{conversation_id}/handoff/resolve", response_model=ConversationOut)
def resolve_handoff(
    conversation_id: uuid.UUID, ctx: ManagerOrg, db: DbSession
) -> WhatsAppConversation:
    """A human is done; control returns to AI auto-reply (if still enabled for this connection).
    Never sends any message on its own -- see ``services/messaging/whatsapp/handoff.py``."""
    conversation = _conversation(db, ctx, conversation_id)
    handoff_service.resolve(db, conversation=conversation, now=datetime.now(UTC))
    db.refresh(conversation)
    return conversation


# --- templates -------------------------------------------------------------------------------


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(ctx: CurrentOrg, db: DbSession) -> list[WhatsAppTemplate]:
    return list(
        db.scalars(
            select(WhatsAppTemplate)
            .where(WhatsAppTemplate.organization_id == ctx.organization.id)
            .order_by(WhatsAppTemplate.name, WhatsAppTemplate.language)
            .limit(500)
        )
    )


@router.post(
    "/templates/sync", response_model=TemplateSyncOut, dependencies=[Depends(_template_sync_limit)]
)
def sync_templates_endpoint(
    ctx: ManagerOrg, db: DbSession, settings: AppSettings
) -> TemplateSyncOut:
    connection = db.scalar(
        select(WhatsAppConnection).where(
            WhatsAppConnection.organization_id == ctx.organization.id,
            WhatsAppConnection.status == WhatsAppConnectionStatus.CONNECTED,
        )
    )
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No WhatsApp number is connected")
    if settings.whatsapp_messaging_provider == "fake":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Template sync requires a real WhatsApp connection; the offline/test provider is "
            "active",
        )
    if not connection.access_token_encrypted:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No WhatsApp access token stored")
    try:
        secrets = SecretManager.from_settings(settings)
        access_token = secrets.decrypt(
            connection.access_token_encrypted,
            context=SECRET_CONTEXT.format(connection_id=connection.id),
        )
    except SecretError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"secure storage unavailable ({exc.code})"
        ) from None
    try:
        remote = fetch_templates(
            waba_id=connection.waba_id,
            access_token=access_token,
            api_version=connection.api_version,
            base_url=settings.whatsapp_graph_base_url,
            timeout=settings.whatsapp_request_timeout_seconds,
        )
    except MessagingError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not sync templates ({exc.code.value})"
        ) from None
    count = template_service.sync_templates(
        db,
        organization_id=ctx.organization.id,
        connection_id=connection.id,
        remote=remote,
        now=datetime.now(UTC),
    )
    db.commit()
    return TemplateSyncOut(synced=count)


# --- consent / suppression -----------------------------------------------------------------------


def _contact(db: DbSession, ctx: OrgContext, contact_id: uuid.UUID) -> WhatsAppContact:
    row = db.scalar(
        select(WhatsAppContact).where(
            WhatsAppContact.id == contact_id, WhatsAppContact.organization_id == ctx.organization.id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return row


def _consent_view(db: DbSession, ctx: OrgContext, contact: WhatsAppContact) -> ConsentListOut:
    consents = [
        ConsentOut(
            category=category,
            status=consent_service.get_consent_status(
                db, organization_id=ctx.organization.id, contact_id=contact.id, category=category
            ),
        )
        for category in WhatsAppConsentCategory
    ]
    suppressed = consent_service.is_suppressed(
        db, organization_id=ctx.organization.id, contact_id=contact.id
    )
    return ConsentListOut(contact_id=contact.id, consents=consents, suppressed=suppressed)


@router.get("/contacts/{contact_id}/consent", response_model=ConsentListOut)
def get_consent(contact_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> ConsentListOut:
    contact = _contact(db, ctx, contact_id)
    return _consent_view(db, ctx, contact)


@router.post("/contacts/{contact_id}/consent", response_model=ConsentListOut)
def set_consent(
    contact_id: uuid.UUID, body: ConsentIn, ctx: ManagerOrg, db: DbSession
) -> ConsentListOut:
    contact = _contact(db, ctx, contact_id)
    consent_service.record_consent(
        db,
        organization_id=ctx.organization.id,
        contact_id=contact.id,
        category=body.category,
        status=body.status,
        source=body.source,
        now=datetime.now(UTC),
    )
    db.commit()
    return _consent_view(db, ctx, contact)


@router.post("/contacts/{contact_id}/opt-out", response_model=ConsentListOut)
def opt_out(
    contact_id: uuid.UUID, body: OptOutIn, ctx: ManagerOrg, db: DbSession
) -> ConsentListOut:
    contact = _contact(db, ctx, contact_id)
    consent_service.suppress(
        db,
        organization_id=ctx.organization.id,
        contact_id=contact.id,
        reason=body.reason,
        source="dashboard_manual",
        now=datetime.now(UTC),
    )
    db.commit()
    return _consent_view(db, ctx, contact)


# --- automation usage (read-only summary) --------------------------------------------------------


@router.get("/automation-usage", response_model=list[AutomationUsageOut])
def automation_usage(ctx: CurrentOrg, db: DbSession) -> list[AutomationUsageOut]:
    rows = list(
        db.scalars(
            select(AutomationWorkflow).where(
                AutomationWorkflow.organization_id == ctx.organization.id
            )
        )
    )
    usage = []
    for row in rows:
        actions = [
            action.get("type")
            for action in row.definition.get("actions", [])
            if action.get("type") in ACTION_TYPES_SHOWN
        ]
        if actions:
            usage.append(
                AutomationUsageOut(
                    workflow_id=row.id,
                    name=row.name,
                    status=row.status.value,
                    whatsapp_actions=actions,
                )
            )
    return usage
