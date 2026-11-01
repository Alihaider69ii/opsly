import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.db.models import (
    Customer,
    EmailConnection,
    EmailThread,
    Organization,
    WhatsAppAIState,
    WhatsAppConnectionStatus,
)

WEBHOOK_SECRET = "whsec_" + "dGVzdC1lbWFpbC13ZWJob29rLXNlY3JldC0zMmJ5dGVz"


@pytest.fixture()
def org_id(session_factory) -> uuid.UUID:
    oid = uuid.uuid4()
    with session_factory() as db:
        db.add(Organization(id=oid, name="Acme", slug=f"acme-{oid.hex[:8]}"))
        db.commit()
    return oid


@pytest.fixture()
def email_settings(app_settings):
    """The API's settings, configured for the fake email provider with a known webhook secret."""
    app_settings.email_provider = "fake"
    app_settings.email_webhook_secret = SecretStr(WEBHOOK_SECRET)
    app_settings.email_send_rate_limit = 1000
    return app_settings


def seed_connection(
    session_factory,
    org_id: uuid.UUID,
    *,
    status: WhatsAppConnectionStatus = WhatsAppConnectionStatus.CONNECTED,
    enabled: bool = True,
    from_address: str = "support@acme.example",
    ai_auto_reply_enabled: bool = False,
) -> uuid.UUID:
    connection_id = uuid.uuid4()
    with session_factory() as db:
        db.add(
            EmailConnection(
                id=connection_id,
                organization_id=org_id,
                status=status,
                enabled=enabled,
                from_address=from_address,
                from_name="Acme Support",
                inbound_address=f"support-{org_id.hex[:8]}@inbound.bizflow.invalid",
                ai_auto_reply_enabled=ai_auto_reply_enabled,
                connected_at=datetime.now(UTC),
            )
        )
        db.commit()
    return connection_id


def seed_customer(
    session_factory, org_id: uuid.UUID, *, email: str = "sam@example.com", **kwargs
) -> uuid.UUID:
    customer_id = uuid.uuid4()
    with session_factory() as db:
        db.add(
            Customer(
                id=customer_id,
                organization_id=org_id,
                email=email,
                normalized_email=email.lower(),
                **kwargs,
            )
        )
        db.commit()
    return customer_id


def seed_thread(
    session_factory,
    org_id: uuid.UUID,
    *,
    customer_id: uuid.UUID | None = None,
    customer_email: str = "sam@example.com",
    ai_state: WhatsAppAIState = WhatsAppAIState.AI_ACTIVE,
) -> uuid.UUID:
    thread_id = uuid.uuid4()
    with session_factory() as db:
        db.add(
            EmailThread(
                id=thread_id,
                organization_id=org_id,
                customer_id=customer_id,
                customer_email=customer_email,
                ai_state=ai_state,
            )
        )
        db.commit()
    return thread_id
