"""Email connection management (Phase 9). Unlike WhatsApp/Shopify there is no external OAuth or
manual-token step: the provider credential is environment-based (see
``app/db/models/email.py:EmailConnection`` docstring), so "connecting" only sets this
organization's non-secret sending identity and its deterministic inbound routing address.
Authenticated; the organization is always the caller's verified one. Reading is open to every
member; connecting/disconnecting and AI settings need OWNER or ADMIN (mirrors
``api/integrations/whatsapp.py``).
"""

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.core.config import Settings, get_settings
from app.db.models import EmailConnection, OrganizationRole, WhatsAppConnectionStatus
from app.schemas.email import (
    EmailAISettingsIn,
    EmailAutoReplyIn,
    EmailConnectIn,
    EmailConnectionOut,
    EmailStatusOut,
)
from app.services.organizations.service import OrgContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/email", tags=["email"])

AppSettings = Annotated[Settings, Depends(get_settings)]
ManagerOrg = Annotated[
    OrgContext, Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))
]
_SLUG_SAFE = re.compile(r"[^a-z0-9-]+")


def _connection(db: DbSession, ctx: OrgContext) -> EmailConnection | None:
    return db.scalar(
        select(EmailConnection).where(EmailConnection.organization_id == ctx.organization.id)
    )


def _status_out(db: DbSession, ctx: OrgContext, settings: Settings) -> EmailStatusOut:
    connection = _connection(db, ctx)
    return EmailStatusOut(
        available=settings.email_configured or settings.email_provider == "fake",
        connection=EmailConnectionOut.model_validate(connection) if connection else None,
    )


def _inbound_address(organization_slug: str, settings: Settings) -> str:
    local = _SLUG_SAFE.sub("-", organization_slug.lower()).strip("-") or "org"
    return f"support-{local}@{settings.email_inbound_domain}"


@router.get("", response_model=EmailStatusOut)
def email_status(ctx: CurrentOrg, db: DbSession, settings: AppSettings) -> EmailStatusOut:
    return _status_out(db, ctx, settings)


@router.post("/connect", response_model=EmailConnectionOut)
def connect(
    body: EmailConnectIn, ctx: ManagerOrg, db: DbSession, settings: AppSettings
) -> EmailConnectionOut:
    now = datetime.now(UTC)
    connection = _connection(db, ctx)
    if connection is None:
        connection = EmailConnection(
            id=uuid.uuid4(),
            organization_id=ctx.organization.id,
            inbound_address=_inbound_address(ctx.organization.slug, settings),
            connected_at=now,
        )
        db.add(connection)
    connection.from_address = body.from_address.lower()
    connection.from_name = body.from_name
    connection.reply_to_address = body.reply_to_address.lower() if body.reply_to_address else None
    connection.status = WhatsAppConnectionStatus.CONNECTED
    connection.enabled = True
    connection.error_code = None
    connection.error_message = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this inbound address is already in use"
        ) from None
    logger.info("email connected org=%s from=%s", ctx.organization.id, connection.from_address)
    return EmailConnectionOut.model_validate(connection)


@router.post("/disconnect", response_model=EmailStatusOut)
def disconnect(ctx: ManagerOrg, db: DbSession, settings: AppSettings) -> EmailStatusOut:
    connection = _connection(db, ctx)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No email connection is configured")
    connection.status = WhatsAppConnectionStatus.DISCONNECTED
    connection.enabled = False
    connection.ai_auto_reply_enabled = False
    db.commit()
    logger.info("email disconnected org=%s", ctx.organization.id)
    return _status_out(db, ctx, settings)


@router.post("/auto-reply", response_model=EmailConnectionOut)
def set_auto_reply(body: EmailAutoReplyIn, ctx: ManagerOrg, db: DbSession) -> EmailConnectionOut:
    connection = _connection(db, ctx)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No email connection is configured")
    connection.ai_auto_reply_enabled = body.enabled
    db.commit()
    return EmailConnectionOut.model_validate(connection)


@router.post("/ai-settings", response_model=EmailConnectionOut)
def set_ai_settings(body: EmailAISettingsIn, ctx: ManagerOrg, db: DbSession) -> EmailConnectionOut:
    connection = _connection(db, ctx)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No email connection is configured")
    connection.ai_grounding_required = body.grounding_required
    connection.ai_min_confidence = body.min_confidence
    connection.ai_handoff_enabled = body.handoff_enabled
    connection.ai_fallback_message = body.fallback_message
    connection.ai_handoff_message = body.handoff_message
    connection.ai_max_replies_per_window = body.max_replies_per_window
    db.commit()
    return EmailConnectionOut.model_validate(connection)
