"""Shopify dashboard API. Authenticated; the organization is always the caller's verified one
(``current_org``) and is never accepted from the client. Reading is open to every member;
connecting, syncing and disconnecting spend Shopify API budget or remove data, so they need OWNER or
ADMIN.

The OAuth callback is also served here (and not as an anonymous public endpoint) because it must
know *who* completes the authorization: the browser lands on the Next.js app, which holds the user's
session and forwards the callback query string, unchanged, with that user's token.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.core.config import Settings, get_settings
from app.core.rate_limit import rate_limit
from app.core.secrets import SecretError, SecretManager
from app.db.database import get_session_factory
from app.db.models import OrganizationRole
from app.schemas.shopify import (
    AuthorizationOut,
    CallbackOut,
    ConnectionOut,
    CountsOut,
    ShopifyConnectIn,
    ShopifyStatusOut,
    SyncStateOut,
)
from app.services.integrations.shopify import service
from app.services.integrations.shopify.auth import SessionFactory
from app.services.integrations.shopify.errors import (
    InvalidShopDomain,
    OAuthError,
    user_message,
)
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.validation import parse_query_string
from app.services.organizations.service import OrgContext
from app.workers.queue import JobQueue, get_job_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/shopify", tags=["shopify"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Queue = Annotated[JobQueue, Depends(get_job_queue)]
Factory = Annotated[SessionFactory, Depends(get_session_factory)]
ManagerOrg = Annotated[
    OrgContext,
    Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
]

_oauth_limit = rate_limit(
    "shopify-oauth",
    limit=lambda s: s.shopify_oauth_rate_limit,
    window=lambda s: 60,
    detail="Too many Shopify connection attempts. Please wait a moment.",
)


def get_shopify_runtime(settings: AppSettings) -> ShopifyRuntime | None:
    """The runtime for this request, or None when the server cannot encrypt secrets (no usable
    SECRET_ENCRYPTION_KEY). Tests override this dependency to inject a mocked Shopify."""
    try:
        return ShopifyRuntime(settings=settings, secrets=SecretManager.from_settings(settings))
    except SecretError as exc:
        logger.error("shopify unavailable: secret manager not usable code=%s", exc.code)
        return None


Runtime = Annotated[ShopifyRuntime | None, Depends(get_shopify_runtime)]

_OAUTH_STATUS = {
    "not_configured": status.HTTP_503_SERVICE_UNAVAILABLE,
    "secrets_not_configured": status.HTTP_503_SERVICE_UNAVAILABLE,
    "already_connected": status.HTTP_409_CONFLICT,
    "different_store": status.HTTP_409_CONFLICT,
    "exchange_failed": status.HTTP_502_BAD_GATEWAY,
}


def _oauth_error(exc: OAuthError) -> HTTPException:
    return HTTPException(
        _OAUTH_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": user_message(exc.code)},
    )


def _status_out(view: service.StatusView) -> ShopifyStatusOut:
    connection = view.connection
    return ShopifyStatusOut(
        available=view.available,
        secure_storage=view.secure_storage,
        webhooks_configured=view.webhooks_configured,
        connection=ConnectionOut.model_validate(connection) if connection is not None else None,
        scopes=view.scopes,
        sync=[SyncStateOut.model_validate(s) for s in view.sync_states],
        counts=CountsOut(
            products=view.counts.products,
            variants=view.counts.variants,
            customers=view.counts.customers,
            orders=view.counts.orders,
        ),
    )


def _view(db: DbSession, ctx: OrgContext, settings: Settings, rt: ShopifyRuntime | None):
    return _status_out(service.get_status(db, ctx, settings, secure_storage=rt is not None))


@router.get("", response_model=ShopifyStatusOut)
def shopify_status(
    ctx: CurrentOrg, db: DbSession, settings: AppSettings, rt: Runtime
) -> ShopifyStatusOut:
    return _view(db, ctx, settings, rt)


@router.post("/connect", response_model=AuthorizationOut, dependencies=[Depends(_oauth_limit)])
def connect(
    body: ShopifyConnectIn, ctx: ManagerOrg, db: DbSession, settings: AppSettings, rt: Runtime
) -> AuthorizationOut:
    try:
        url = service.start_connect(db, ctx, shop=body.shop, settings=settings, rt=rt)
    except InvalidShopDomain:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, user_message("invalid_shop")
        ) from None
    except OAuthError as exc:
        raise _oauth_error(exc) from None
    return AuthorizationOut(authorization_url=url)


@router.get("/callback", response_model=CallbackOut, dependencies=[Depends(_oauth_limit)])
def callback(
    request: Request, ctx: ManagerOrg, db: DbSession, queue: Queue, rt: Runtime
) -> CallbackOut:
    """Complete the authorization. Takes the callback's raw query string (its HMAC covers every
    parameter, so it must reach us exactly as Shopify sent it)."""
    try:
        query = parse_query_string(request.url.query)
        connection = service.finish_connect(db, ctx, query=query, rt=rt, queue=queue)
    except InvalidShopDomain:  # duplicate parameters
        raise _oauth_error(OAuthError("malformed callback", code="invalid_signature")) from None
    except OAuthError as exc:
        db.rollback()
        logger.info("shopify callback rejected org=%s code=%s", ctx.organization.id, exc.code)
        raise _oauth_error(exc) from None
    return CallbackOut(status="connected", shop_domain=connection.shop_domain)


@router.post("/sync", response_model=ShopifyStatusOut, status_code=status.HTTP_202_ACCEPTED)
def sync_now(
    ctx: ManagerOrg, db: DbSession, settings: AppSettings, queue: Queue, rt: Runtime
) -> ShopifyStatusOut:
    try:
        service.request_sync(db, ctx, settings=settings, queue=queue)
    except service.NotConnected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Shopify store is connected") from None
    except service.ReauthorizationRequired:
        raise HTTPException(
            status.HTTP_409_CONFLICT, user_message("reauthorization_required")
        ) from None
    except service.SyncInProgress:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A sync is already in progress for this store"
        ) from None
    except service.SyncTooSoon as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "This store was synced very recently. Please wait a moment before syncing again.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    return _view(db, ctx, settings, rt)


@router.post("/disconnect", response_model=ShopifyStatusOut)
def disconnect(
    ctx: ManagerOrg, db: DbSession, settings: AppSettings, rt: Runtime, factory: Factory
) -> ShopifyStatusOut:
    try:
        service.disconnect(db, ctx, rt=rt, session_factory=factory)
    except service.NotConnected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Shopify store is connected") from None
    db.expire_all()
    return _view(db, ctx, settings, rt)
