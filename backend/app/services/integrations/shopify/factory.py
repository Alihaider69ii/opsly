"""Builds Shopify clients and token sources from settings, in one place.

``ShopifyRuntime`` bundles what every Shopify code path needs (settings, the secret manager and the
HTTP seams). Production code builds one from settings; tests pass a ``MockTransport`` and a no-op
``sleep`` so the whole integration runs without a network or real delays.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from app.core.config import Settings
from app.core.secrets import SecretManager
from app.services.integrations.shopify.auth import ConnectionTokens, SessionFactory
from app.services.integrations.shopify.client import ShopifyClient
from app.services.integrations.shopify.versioning import support_status

logger = logging.getLogger(__name__)


def check_api_version(version: str, today: date | None = None) -> str:
    """Log (once, at startup) when the configured Admin API version is near or past the end of
    Shopify's 12-month minimum support window, or not released yet. Returns the status."""
    status = support_status(version, today or datetime.now(UTC).date())
    if status in ("ageing", "unsupported", "unreleased"):
        logger.warning(
            "shopify api version %s is %s: plan an upgrade (SHOPIFY_API_VERSION, docs/shopify.md)",
            version,
            status,
        )
    return status


@dataclass
class ShopifyRuntime:
    settings: Settings
    secrets: SecretManager
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def client_for(self, shop_domain: str) -> ShopifyClient:
        """A client without a token source (OAuth endpoints)."""
        s = self.settings
        return ShopifyClient(
            shop_domain=shop_domain,
            api_version=s.shopify_api_version,
            timeout=s.shopify_request_timeout_seconds,
            max_retries=s.shopify_max_retries,
            dev_base_url=s.shopify_dev_base_url,
            transport=self.transport,
            sleep=self.sleep,
            clock=self.monotonic,
        )

    def tokens_for(
        self, session_factory: SessionFactory, connection_id: uuid.UUID
    ) -> ConnectionTokens:
        return ConnectionTokens(
            session_factory,
            connection_id,
            settings=self.settings,
            secrets=self.secrets,
            client_factory=self.client_for,
            clock=self.now,
        )

    def authorized_client(
        self, session_factory: SessionFactory, connection_id: uuid.UUID, shop_domain: str
    ) -> ShopifyClient:
        """A client for a stored connection; its tokens refresh themselves when needed."""
        return self.client_for(shop_domain).authorize(
            self.tokens_for(session_factory, connection_id)
        )
