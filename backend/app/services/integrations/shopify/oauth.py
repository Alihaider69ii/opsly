"""Shopify OAuth (authorization code grant) with expiring offline tokens.

    merchant -> BizFlow dashboard: "Connect"  (start_authorization)
             -> Shopify consent screen (redirect to https://<shop>/admin/oauth/authorize?...)
             -> the frontend callback route, which holds the user's session (complete_authorization)

What stops each attack:

* **Forged callback**: the query string carries an HMAC-SHA256 over every parameter, keyed with the
  app's client secret; it is verified (constant time) before anything else happens.
* **State forgery / CSRF / login CSRF**: ``state`` is 256 random bits, only its hash is stored,
  it expires after minutes, works once, and is bound to the organization, the user and the shop that
  started it. The callback is authenticated, so a state started by user A cannot be completed by
  user B, and an attacker cannot feed the victim an authorization the attacker started.
* **Replay**: consuming the state is one atomic UPDATE; the HMAC's timestamp must also be recent.
* **Arbitrary shop / SSRF**: the shop is validated against ``*.myshopify.com`` twice (when the flow
  starts and in the callback) and must equal the shop the state was issued for.
* **Organization injection**: the organization is never read from the callback; it comes from the
  authenticated session and the stored state.
* **Wrong grants**: the granted scopes must include the required ones.
* **Two organizations, one store**: enforced by a partial unique index and checked up front.
"""

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.secrets import SecretManager
from app.db.models import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifyOAuthState,
    WebhookRegistration,
)
from app.services.integrations.shopify import scopes as scope_rules
from app.services.integrations.shopify.auth import store_grant
from app.services.integrations.shopify.client import ShopifyClient, StaticToken
from app.services.integrations.shopify.errors import (
    InvalidShopDomain,
    OAuthError,
    ShopifyError,
)
from app.services.integrations.shopify.validation import (
    normalize_shop_domain,
    verify_oauth_hmac,
)

logger = logging.getLogger(__name__)

CALLBACK_MAX_AGE = timedelta(minutes=10)
CALLBACK_MAX_FUTURE_SKEW = timedelta(minutes=2)
_MAX_PARAM_CHARS = 2048
_EXPIRED_STATE_RETENTION = timedelta(days=1)

ClientFactory = Callable[[str], ShopifyClient]


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _client_secret(settings: Settings) -> str:
    return (
        settings.shopify_client_secret.get_secret_value() if settings.shopify_client_secret else ""
    )


def authorization_base(settings: Settings, shop_domain: str) -> str:
    """Where the merchant is sent. The dev override points at a local fake Shopify."""
    return settings.shopify_dev_base_url.rstrip("/") or f"https://{shop_domain}"


def _claim_conflict(
    db: Session, *, organization_id: uuid.UUID, shop_domain: str
) -> ShopifyConnection | None:
    """The organization's own connection row, after checking this store is not owned elsewhere or
    that the organization is not already linked to a different store."""
    own = db.scalar(
        select(ShopifyConnection).where(ShopifyConnection.organization_id == organization_id)
    )
    if (
        own is not None
        and own.status != ShopifyConnectionStatus.DISCONNECTED
        and own.shop_domain != shop_domain
    ):
        raise OAuthError("disconnect the current store first", code="different_store")
    other = db.scalar(
        select(ShopifyConnection.id).where(
            ShopifyConnection.shop_domain == shop_domain,
            ShopifyConnection.organization_id != organization_id,
            ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,
        )
    )
    if other is not None:
        raise OAuthError("store is linked to another organization", code="already_connected")
    return own


def start_authorization(
    db: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    shop_input: str,
    settings: Settings,
    now: datetime,
) -> str:
    """Create a state and return the Shopify authorization URL for the merchant to visit."""
    if not settings.shopify_configured:
        raise OAuthError("Shopify is not configured", code="not_configured")
    shop = normalize_shop_domain(shop_input)  # InvalidShopDomain
    _claim_conflict(db, organization_id=organization_id, shop_domain=shop)

    # Housekeeping: stale states, and this user's earlier unfinished attempts, are discarded.
    db.execute(
        delete(ShopifyOAuthState)
        .where(ShopifyOAuthState.expires_at < now - _EXPIRED_STATE_RETENTION)
        .execution_options(synchronize_session=False)
    )
    db.execute(
        delete(ShopifyOAuthState).where(
            ShopifyOAuthState.organization_id == organization_id,
            ShopifyOAuthState.user_id == user_id,
            ShopifyOAuthState.consumed_at.is_(None),
        )
    )
    state = secrets.token_urlsafe(32)
    db.add(
        ShopifyOAuthState(
            organization_id=organization_id,
            user_id=user_id,
            shop_domain=shop,
            state_hash=hash_state(state),
            expires_at=now + timedelta(seconds=settings.shopify_oauth_state_ttl_seconds),
        )
    )
    db.commit()
    query = urlencode(
        {
            "client_id": settings.shopify_client_id,
            "scope": settings.shopify_scopes,
            "redirect_uri": settings.shopify_redirect_uri,
            "state": state,
        }
    )
    logger.info("shopify authorization started org=%s shop=%s", organization_id, shop)
    return f"{authorization_base(settings, shop)}/admin/oauth/authorize?{query}"


def _validate_callback(query: Mapping[str, str], settings: Settings, now: datetime) -> str:
    """Signature, parameter sanity and freshness. Returns the normalized shop."""
    if not settings.shopify_configured:
        raise OAuthError("Shopify is not configured", code="not_configured")
    if any(len(k) > 64 or len(v) > _MAX_PARAM_CHARS for k, v in query.items()):
        raise OAuthError("oversized callback parameter", code="invalid_signature")
    if not verify_oauth_hmac(query, _client_secret(settings)):
        raise OAuthError("callback signature mismatch", code="invalid_signature")
    try:
        shop = normalize_shop_domain(query.get("shop", ""))
    except InvalidShopDomain:
        raise OAuthError(
            "callback shop is not a myshopify.com domain", code="invalid_signature"
        ) from None
    if not query.get("code") or not query.get("state"):
        raise OAuthError("callback is missing code or state", code="invalid_state")
    try:
        issued = datetime.fromtimestamp(int(query.get("timestamp", "")), tz=UTC)
    except (ValueError, OverflowError, OSError):
        raise OAuthError("callback timestamp is invalid", code="state_expired") from None
    if now - issued > CALLBACK_MAX_AGE or issued - now > CALLBACK_MAX_FUTURE_SKEW:
        raise OAuthError("callback is too old", code="state_expired")
    return shop


def _consume_state(
    db: Session,
    *,
    state: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    shop: str,
    now: datetime,
) -> None:
    row = db.scalar(
        select(ShopifyOAuthState).where(ShopifyOAuthState.state_hash == hash_state(state))
    )
    if row is None or row.consumed_at is not None:
        raise OAuthError("unknown or already used state", code="invalid_state")
    if _aware(row.expires_at) <= now:
        raise OAuthError("state expired", code="state_expired")
    # Bound to who started it: another user or organization cannot complete someone else's flow.
    if row.organization_id != organization_id or row.user_id != user_id:
        raise OAuthError("state belongs to another session", code="invalid_state")
    if row.shop_domain != shop:
        raise OAuthError("state was issued for another store", code="shop_mismatch")
    result = db.execute(
        update(ShopifyOAuthState)
        .where(
            ShopifyOAuthState.id == row.id,
            ShopifyOAuthState.consumed_at.is_(None),
            ShopifyOAuthState.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]  # lost a race with a second use
        raise OAuthError("state already used", code="invalid_state")


def complete_authorization(
    db: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    query: Mapping[str, str],
    settings: Settings,
    secrets_manager: SecretManager,
    client_for: ClientFactory,
    now: datetime,
) -> ShopifyConnection:
    """Verify the callback, exchange the code and store the (encrypted) connection.

    The caller queues the initial sync and commits nothing further; this commits the connection.
    """
    shop = _validate_callback(query, settings, now)
    _consume_state(
        db,
        state=query["state"],
        organization_id=organization_id,
        user_id=user_id,
        shop=shop,
        now=now,
    )
    own = _claim_conflict(db, organization_id=organization_id, shop_domain=shop)

    client = client_for(shop)
    try:
        grant = client.exchange_code(
            client_id=settings.shopify_client_id,
            client_secret=_client_secret(settings),
            code=query["code"],
        )
    finally:
        client.close()
    granted = scope_rules.parse_scopes(grant.scope)
    if not all(scope_rules.covers(granted, s) for s in scope_rules.REQUIRED_SCOPES):
        raise OAuthError("required scopes were not granted", code="scope_missing")

    info_client = client_for(shop).authorize(StaticToken(grant.access_token))
    try:
        info = info_client.shop_info()
    except ShopifyError as exc:
        raise OAuthError(
            f"could not read shop information ({exc.code})", code="exchange_failed"
        ) from None
    finally:
        info_client.close()
    if info.myshopify_domain and info.myshopify_domain.lower() != shop:
        raise OAuthError("Shopify reported a different store", code="shop_mismatch")

    connection = own or ShopifyConnection(
        id=uuid.uuid4(), organization_id=organization_id, installed_at=now
    )
    if own is not None and own.status == ShopifyConnectionStatus.DISCONNECTED:
        connection.installed_at = now  # a fresh installation
    connection.shop_domain = shop
    connection.status = ShopifyConnectionStatus.CONNECTED
    connection.scopes = scope_rules.scope_string(granted)
    connection.api_version = settings.shopify_api_version
    connection.shop_name = info.name
    connection.currency = info.currency
    connection.timezone = info.timezone
    connection.error_code = None
    connection.error_message = None
    connection.webhook_status = WebhookRegistration.NOT_CONFIGURED
    store_grant(connection, grant, secrets_manager, now)
    if own is None:
        db.add(connection)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OAuthError(
            "store is linked to another organization", code="already_connected"
        ) from None
    logger.info("shopify connected org=%s shop=%s", organization_id, shop)
    return connection
