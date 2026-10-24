"""Shopify access tokens: encrypted storage, expiry and rotation.

Shopify issues *expiring offline tokens*: an access token that lasts about an hour plus a refresh
token (90 days) that **rotates**: every refresh returns a new refresh token and retires the old
one. Two processes refreshing the same store at once would each retire the other's token, so
refreshing happens under a row lock on the connection (``SELECT ... FOR UPDATE``) and is re-checked
after the lock is acquired: the loser of the race simply uses the winner's new token.

Tokens exist in plaintext only in memory, for the duration of a request. At rest they are
AES-256-GCM ciphertext bound to the connection (``context``), see ``app.core.secrets``.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.secrets import SecretError, SecretManager
from app.db.models import ShopifyConnection, ShopifyConnectionStatus
from app.services.integrations.shopify.client import ShopifyClient, TokenGrant
from app.services.integrations.shopify.errors import (
    ShopifyAuthError,
    ShopifyError,
    ShopifyUnavailable,
    user_message,
)

logger = logging.getLogger(__name__)

LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL SQLSTATE: lock_timeout expired while waiting for a lock

SessionFactory = Callable[[], Session]
ClientFactory = Callable[[str], ShopifyClient]

REFRESH_MARGIN = timedelta(minutes=5)
DEFAULT_ACCESS_TTL_SECONDS = 3600
DEFAULT_REFRESH_TTL_SECONDS = 90 * 24 * 3600


def token_context(kind: str, connection_id: uuid.UUID) -> str:
    """The authenticated context for a stored token: it cannot be decrypted for another row."""
    return f"shopify:{kind}:{connection_id}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def store_grant(
    connection: ShopifyConnection,
    grant: TokenGrant,
    secrets: SecretManager,
    now: datetime,
) -> None:
    """Encrypt and store a token grant on the connection (the caller commits)."""
    connection.access_token_encrypted = secrets.encrypt(
        grant.access_token, context=token_context("access_token", connection.id)
    )
    connection.access_token_expires_at = (
        now + timedelta(seconds=grant.expires_in) if grant.expires_in else None
    )
    if grant.refresh_token:
        connection.refresh_token_encrypted = secrets.encrypt(
            grant.refresh_token, context=token_context("refresh_token", connection.id)
        )
        connection.refresh_token_expires_at = now + timedelta(
            seconds=grant.refresh_token_expires_in or DEFAULT_REFRESH_TTL_SECONDS
        )
    else:  # a non-expiring token: nothing to refresh with
        connection.refresh_token_encrypted = None
        connection.refresh_token_expires_at = None


def clear_tokens(connection: ShopifyConnection) -> None:
    connection.access_token_encrypted = None
    connection.refresh_token_encrypted = None
    connection.access_token_expires_at = None
    connection.refresh_token_expires_at = None


def mark_reauthorization_required(connection: ShopifyConnection, code: str) -> None:
    connection.status = ShopifyConnectionStatus.ERROR
    connection.error_code = code
    connection.error_message = user_message(code)


class ConnectionTokens:
    """The ``TokenSource`` for one stored connection."""

    def __init__(
        self,
        session_factory: SessionFactory,
        connection_id: uuid.UUID,
        *,
        settings: Settings,
        secrets: SecretManager,
        client_factory: ClientFactory,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._connection_id = connection_id
        self._settings = settings
        self._secrets = secrets
        self._client_factory = client_factory
        self._clock = clock
        self._last_used: str | None = None  # the token this instance last handed out

    # -- TokenSource -------------------------------------------------------------------------

    def access_token(self) -> str:
        with self._session_factory() as db:
            connection = self._load(db)
            if not self._expiring(connection):
                return self._remember(self._decrypt_access(connection))
        return self.refresh(force=False)

    def force_refresh(self) -> str:
        """Called after Shopify rejected the token. If another process already replaced the token
        we were using, take theirs instead of rotating again."""
        return self.refresh(force=True)

    # -- internals ---------------------------------------------------------------------------

    def _remember(self, token: str) -> str:
        self._last_used = token
        return token

    def _load(self, db: Session, *, lock: bool = False) -> ShopifyConnection:
        stmt = select(ShopifyConnection).where(ShopifyConnection.id == self._connection_id)
        if lock:
            stmt = stmt.with_for_update()
        connection = db.scalar(stmt)
        if (
            connection is None
            or connection.status == ShopifyConnectionStatus.DISCONNECTED
            or not connection.access_token_encrypted
        ):
            raise ShopifyAuthError("the store is not connected")
        return connection

    def _expiring(self, connection: ShopifyConnection) -> bool:
        expires = _aware(connection.access_token_expires_at)
        return expires is not None and self._clock() >= expires - REFRESH_MARGIN

    def _decrypt_access(self, connection: ShopifyConnection) -> str:
        assert connection.access_token_encrypted is not None
        try:
            return self._secrets.decrypt(
                connection.access_token_encrypted,
                context=token_context("access_token", connection.id),
            )
        except SecretError as exc:
            logger.error(
                "stored shopify token unreadable connection=%s code=%s", connection.id, exc.code
            )
            raise ShopifyAuthError("the stored credentials cannot be read") from None

    def refresh(self, *, force: bool) -> str:
        with self._session_factory() as db:
            try:
                connection = self._load(db, lock=True)
            except OperationalError as exc:
                if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
                    raise  # a real database failure is not a Shopify problem; let it surface as one
                logger.warning(
                    "token refresh could not lock the connection=%s", self._connection_id
                )
                raise ShopifyUnavailable("another worker holds the connection lock") from None
            current = self._decrypt_access(connection)
            # Someone else may have refreshed while we waited for the lock. (A fresh instance has
            # handed out no token yet, so a forced refresh there is an intentional rotation.)
            replaced = self._last_used is not None and current != self._last_used
            if not self._expiring(connection) and (not force or replaced):
                return self._remember(current)
            if not connection.refresh_token_encrypted:
                mark_reauthorization_required(connection, "reauthorization_required")
                db.commit()
                raise ShopifyAuthError(
                    "no refresh token is available", code="reauthorization_required"
                )
            try:
                refresh_token = self._secrets.decrypt(
                    connection.refresh_token_encrypted,
                    context=token_context("refresh_token", connection.id),
                )
            except SecretError:
                mark_reauthorization_required(connection, "auth_failed")
                db.commit()
                raise ShopifyAuthError("the stored credentials cannot be read") from None

            client = self._client_factory(connection.shop_domain)
            try:
                grant = client.refresh_access_token(
                    client_id=self._settings.shopify_client_id,
                    client_secret=(
                        self._settings.shopify_client_secret.get_secret_value()
                        if self._settings.shopify_client_secret
                        else ""
                    ),
                    refresh_token=refresh_token,
                )
            except ShopifyAuthError:
                mark_reauthorization_required(connection, "reauthorization_required")
                db.commit()
                logger.warning("shopify refresh rejected connection=%s", connection.id)
                raise
            except ShopifyError:
                db.rollback()  # transient: keep the stored tokens, try again later
                raise
            finally:
                client.close()
            store_grant(connection, grant, self._secrets, self._clock())
            db.commit()
            logger.info("shopify token refreshed connection=%s", connection.id)
            return self._remember(grant.access_token)
