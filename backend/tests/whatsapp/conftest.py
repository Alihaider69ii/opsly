import base64
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.secrets import SecretManager
from app.db.models import Organization, WhatsAppConnection, WhatsAppConnectionStatus
from app.main import app
from app.services.messaging.whatsapp.send import SECRET_CONTEXT

TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
APP_SECRET = "test-whatsapp-app-secret"
VERIFY_TOKEN = "test-verify-token"


@pytest.fixture()
def whatsapp_settings(app_settings):
    """The API's settings, configured as a deployment with a WhatsApp app and secure storage."""
    app_settings.whatsapp_app_id = "test-app-id"
    app_settings.whatsapp_app_secret = SecretStr(APP_SECRET)
    app_settings.whatsapp_webhook_verify_token = SecretStr(VERIFY_TOKEN)
    app_settings.secret_encryption_key = SecretStr(TEST_KEY)
    app_settings.whatsapp_messaging_provider = "fake"
    app_settings.whatsapp_connect_rate_limit = 1000
    app_settings.whatsapp_send_rate_limit = 1000
    return app_settings


@pytest.fixture()
def secrets_manager(whatsapp_settings) -> SecretManager:
    return SecretManager.from_settings(whatsapp_settings)


@pytest.fixture()
def whatsapp_client(client, whatsapp_settings):
    """The API client with WhatsApp settings and the test DB wired in."""
    app.dependency_overrides[get_settings] = lambda: whatsapp_settings
    return client


def seed_connection(
    session_factory,
    org_id: uuid.UUID,
    secrets: SecretManager,
    *,
    phone_number_id: str = "PNID_TEST",
    waba_id: str = "WABA_TEST",
    status: WhatsAppConnectionStatus = WhatsAppConnectionStatus.CONNECTED,
    default_country: str | None = None,
    access_token: str = "test-token",
    ai_auto_reply_enabled: bool = False,
) -> uuid.UUID:
    now = datetime.now(UTC)
    connection_id = uuid.uuid4()
    with session_factory() as db:
        connection = WhatsAppConnection(
            id=connection_id,
            organization_id=org_id,
            status=status,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            api_version="v21.0",
            default_country=default_country,
            ai_auto_reply_enabled=ai_auto_reply_enabled,
            connected_at=now,
            access_token_encrypted=secrets.encrypt(
                access_token, context=SECRET_CONTEXT.format(connection_id=connection_id)
            ),
            token_set_at=now,
        )
        db.add(connection)
        db.commit()
    return connection_id


@pytest.fixture()
def org_id(session_factory) -> uuid.UUID:
    oid = uuid.uuid4()
    with session_factory() as db:
        db.add(Organization(id=oid, name="Acme", slug=f"acme-{oid.hex[:8]}"))
        db.commit()
    return oid
