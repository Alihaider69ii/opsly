import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from pydantic import SecretStr

from app.api.integrations.shopify import get_shopify_runtime
from app.core.secrets import SecretManager
from app.db.database import get_session_factory
from app.db.models import ShopifyConnection, ShopifyConnectionStatus, WebhookRegistration
from app.main import app
from app.services.integrations.shopify.auth import store_grant
from app.services.integrations.shopify.client import TokenGrant
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.validation import sign_oauth_query
from tests.shopify.fake_shopify import FakeShopify

TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(100, 132))).decode()
WEBHOOK_BASE = "https://api.example.com"


class Clock:
    """Controllable time: ``sleep`` advances it instead of waiting."""

    def __init__(self) -> None:
        self.t = 1_000.0
        self.base = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self) -> datetime:
        return self.base + timedelta(seconds=self.t - 1_000.0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def fake() -> FakeShopify:
    return FakeShopify()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def shopify_settings(app_settings):
    """The API's settings, configured as a deployment with a Shopify app and secure storage."""
    app_settings.shopify_client_id = "test-client-id"
    app_settings.shopify_client_secret = SecretStr("test-client-secret")
    app_settings.shopify_redirect_uri = "http://localhost:3000/integrations/shopify/callback"
    app_settings.shopify_webhook_base_url = WEBHOOK_BASE
    app_settings.secret_encryption_key = SecretStr(TEST_KEY)
    app_settings.shopify_sync_min_interval_seconds = 0
    app_settings.shopify_max_retries = 3
    return app_settings


@pytest.fixture()
def secrets_manager(shopify_settings) -> SecretManager:
    return SecretManager.from_settings(shopify_settings)


@pytest.fixture()
def runtime(shopify_settings, secrets_manager, fake, clock) -> ShopifyRuntime:
    return ShopifyRuntime(
        settings=shopify_settings,
        secrets=secrets_manager,
        transport=fake.transport(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=clock.now,
    )


@pytest.fixture()
def shopify_client(client, runtime, session_factory):
    """The API client with Shopify pointed at the fake and background work using the test DB."""
    app.dependency_overrides[get_shopify_runtime] = lambda: runtime
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    return client


def seed_connection(
    session_factory,
    org_id: uuid.UUID,
    runtime: ShopifyRuntime,
    fake: FakeShopify,
    *,
    status: ShopifyConnectionStatus = ShopifyConnectionStatus.CONNECTED,
    scopes: str | None = None,
    shop: str | None = None,
    expiring: bool | None = None,
) -> uuid.UUID:
    """A stored, working connection for ``org_id`` (tokens issued by the fake, stored encrypted)."""
    if expiring is not None:
        fake.expiring = expiring
    body = fake._issue()
    grant = TokenGrant(
        access_token=body["access_token"],
        scope=body["scope"],
        expires_in=body.get("expires_in"),
        refresh_token=body.get("refresh_token"),
        refresh_token_expires_in=body.get("refresh_token_expires_in"),
    )
    with session_factory() as db:
        connection = ShopifyConnection(
            id=uuid.uuid4(),
            organization_id=org_id,
            shop_domain=shop or fake.shop,
            status=status,
            scopes=scopes or fake.granted_scopes,
            api_version=runtime.settings.shopify_api_version,
            shop_name=fake.shop_name,
            currency=fake.currency,
            timezone=fake.timezone,
            installed_at=runtime.now(),
            webhook_status=WebhookRegistration.NOT_CONFIGURED,
        )
        store_grant(connection, grant, runtime.secrets, runtime.now())
        db.add(connection)
        db.commit()
        return connection.id


def callback_query(
    fake: FakeShopify, state: str, *, shop: str | None = None, code: str = "good-code", **extra
) -> dict[str, str]:
    """A correctly signed OAuth callback query, as Shopify would send it."""
    query = {
        "code": code,
        "host": "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvdGVzdA",
        "shop": shop or fake.shop,
        "state": state,
        "timestamp": str(int(time.time())),
        **extra,
    }
    query["hmac"] = sign_oauth_query(query, fake.client_secret)
    return query


def state_from(authorization_url: str) -> str:
    return parse_qs(urlsplit(authorization_url).query)["state"][0]


def connect_through_api(client, headers, fake: FakeShopify, *, shop: str | None = None):
    """Drive the real flow: POST /connect, then the (signed) callback. Returns the callback response."""
    started = client.post(
        "/api/integrations/shopify/connect", json={"shop": shop or fake.shop}, headers=headers
    )
    assert started.status_code == 200, started.text
    state = state_from(started.json()["authorization_url"])
    query = callback_query(fake, state, shop=shop)
    return client.get(f"/api/integrations/shopify/callback?{urlencode(query)}", headers=headers)
