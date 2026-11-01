"""WhatsApp connection management: manual credential entry (phone_number_id / WABA id / a
permanent access token the org admin obtains from their own Meta Business Manager), not OAuth
embedded signup -- see docs/phase-6-progress.md "Architecture Decisions" for why. Authenticated;
the organization is always the caller's verified one and is never accepted from the client.
Reading is open to every member; connecting and disconnecting need OWNER or ADMIN.

Disconnect does NOT purge WhatsApp data (unlike Shopify's disconnect, which purges re-importable
commerce data): consent/suppression records and conversation history have no external source of
truth to re-import from, and a suppressed contact must stay suppressed across a disconnect/
reconnect cycle. Disconnect only clears the stored token and marks the connection released.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.core.config import Settings, get_settings
from app.core.rate_limit import rate_limit
from app.core.secrets import SecretError, SecretManager
from app.db.models import OrganizationRole, WhatsAppConnection, WhatsAppConnectionStatus
from app.schemas.whatsapp import (
    AISettingsIn,
    AutoReplyIn,
    ConnectionOut,
    WhatsAppConnectIn,
    WhatsAppStatusOut,
)
from app.services.messaging.errors import MessagingError
from app.services.messaging.phone import is_known_region
from app.services.messaging.providers.whatsapp_cloud import fetch_phone_info
from app.services.messaging.whatsapp.send import SECRET_CONTEXT
from app.services.organizations.service import OrgContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/whatsapp", tags=["whatsapp"])

AppSettings = Annotated[Settings, Depends(get_settings)]
ManagerOrg = Annotated[
    OrgContext,
    Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
]

_connect_limit = rate_limit(
    "whatsapp-connect",
    limit=lambda s: s.whatsapp_connect_rate_limit,
    window=lambda s: 60,
    detail="Too many WhatsApp connection attempts. Please wait a moment.",
)


def _connection(db: DbSession, ctx: OrgContext) -> WhatsAppConnection | None:
    return db.scalar(
        select(WhatsAppConnection).where(
            WhatsAppConnection.organization_id == ctx.organization.id,
            WhatsAppConnection.status != WhatsAppConnectionStatus.DISCONNECTED,
        )
    )


def _status_out(db: DbSession, ctx: OrgContext, settings: Settings) -> WhatsAppStatusOut:
    connection = _connection(db, ctx)
    secure = True
    try:
        SecretManager.from_settings(settings)
    except SecretError:
        secure = False
    return WhatsAppStatusOut(
        available=settings.whatsapp_configured or settings.whatsapp_messaging_provider == "fake",
        secure_storage=secure,
        connection=ConnectionOut.model_validate(connection) if connection is not None else None,
    )


@router.get("", response_model=WhatsAppStatusOut)
def whatsapp_status(ctx: CurrentOrg, db: DbSession, settings: AppSettings) -> WhatsAppStatusOut:
    return _status_out(db, ctx, settings)


@router.post("/connect", response_model=ConnectionOut, dependencies=[Depends(_connect_limit)])
def connect(
    body: WhatsAppConnectIn, ctx: ManagerOrg, db: DbSession, settings: AppSettings
) -> ConnectionOut:
    if body.default_country and not is_known_region(body.default_country):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "default_country is invalid")
    try:
        secrets = SecretManager.from_settings(settings)
    except SecretError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"secure storage unavailable ({exc.code})"
        ) from None

    existing = db.scalar(
        select(WhatsAppConnection).where(WhatsAppConnection.organization_id == ctx.organization.id)
    )
    if (
        existing is not None
        and existing.status != WhatsAppConnectionStatus.DISCONNECTED
        and existing.phone_number_id != body.phone_number_id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "disconnect the current WhatsApp number first"
        )
    other = db.scalar(
        select(WhatsAppConnection.id).where(
            WhatsAppConnection.phone_number_id == body.phone_number_id,
            WhatsAppConnection.organization_id != ctx.organization.id,
            WhatsAppConnection.status != WhatsAppConnectionStatus.DISCONNECTED,
        )
    )
    if other is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this WhatsApp number is already connected to another organization",
        )

    now = datetime.now(UTC)
    info = None
    if settings.whatsapp_messaging_provider != "fake":
        try:
            info = fetch_phone_info(
                phone_number_id=body.phone_number_id,
                access_token=body.access_token,
                api_version=settings.whatsapp_api_version,
                base_url=settings.whatsapp_graph_base_url,
                timeout=settings.whatsapp_request_timeout_seconds,
            )
        except MessagingError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"could not verify WhatsApp credentials ({exc.code.value})",
            ) from None

    connection = existing or WhatsAppConnection(
        id=uuid.uuid4(), organization_id=ctx.organization.id, connected_at=now
    )
    if existing is not None and existing.status == WhatsAppConnectionStatus.DISCONNECTED:
        connection.connected_at = now
    connection.phone_number_id = body.phone_number_id
    connection.waba_id = body.waba_id
    connection.business_id = body.business_id
    connection.default_country = body.default_country.upper() if body.default_country else None
    connection.api_version = settings.whatsapp_api_version
    connection.status = WhatsAppConnectionStatus.CONNECTED
    connection.access_token_encrypted = secrets.encrypt(
        body.access_token, context=SECRET_CONTEXT.format(connection_id=connection.id)
    )
    connection.token_set_at = now
    connection.error_code = None
    connection.error_message = None
    if info is not None:
        connection.display_phone_number = info.display_phone_number
        connection.verified_name = info.verified_name
        connection.quality_rating = info.quality_rating
        connection.last_verified_at = now
    if existing is None:
        db.add(connection)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this WhatsApp number is already connected to another organization",
        ) from None
    logger.info(
        "whatsapp connected org=%s phone_number_id=%s", ctx.organization.id, body.phone_number_id
    )
    return ConnectionOut.model_validate(connection)


@router.post("/disconnect", response_model=WhatsAppStatusOut)
def disconnect(ctx: ManagerOrg, db: DbSession, settings: AppSettings) -> WhatsAppStatusOut:
    connection = _connection(db, ctx)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No WhatsApp number is connected")
    connection.status = WhatsAppConnectionStatus.DISCONNECTED
    connection.access_token_encrypted = None
    connection.error_code = None
    connection.error_message = None
    db.commit()
    logger.info("whatsapp disconnected org=%s", ctx.organization.id)
    return _status_out(db, ctx, settings)


@router.post("/auto-reply", response_model=ConnectionOut)
def set_auto_reply(body: AutoReplyIn, ctx: ManagerOrg, db: DbSession) -> ConnectionOut:
    connection = _connection(db, ctx)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No WhatsApp number is connected")
    connection.ai_auto_reply_enabled = body.enabled
    db.commit()
    return ConnectionOut.model_validate(connection)


@router.post("/ai-settings", response_model=ConnectionOut)
def set_ai_settings(body: AISettingsIn, ctx: ManagerOrg, db: DbSession) -> ConnectionOut:
    """Phase 8 AI conversation policy (grounding/confidence/handoff/fallback/reply cap). Does not
    touch ``ai_auto_reply_enabled`` -- that stays a separate, deliberately single-purpose toggle
    (see ``/auto-reply`` above)."""
    connection = _connection(db, ctx)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No WhatsApp number is connected")
    connection.ai_grounding_required = body.grounding_required
    connection.ai_min_confidence = body.min_confidence
    connection.ai_handoff_enabled = body.handoff_enabled
    connection.ai_fallback_message = body.fallback_message
    connection.ai_handoff_message = body.handoff_message
    connection.ai_max_replies_per_window = body.max_replies_per_window
    db.commit()
    return ConnectionOut.model_validate(connection)
