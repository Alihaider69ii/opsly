"""Adversarial coverage for the WhatsApp subsystem: token protection, cross-tenant access, role
enforcement, and the policy gate's bypass resistance. Constructive ("does the right thing work")
coverage lives in test_send.py/test_webhooks.py/test_api.py; this file is "can the wrong thing be
made to happen."
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.core.secrets import SecretError
from app.db.models import (
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppMessageStatus,
)
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.providers.fake import FakeMessagingProvider
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import send as send_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from tests.helpers import add_member, create_org, signup_and_login
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _tenant(client, email, name):
    headers = signup_and_login(client, email)
    org = create_org(client, headers, name)
    return headers, org


# --- token protection ------------------------------------------------------------------------------


def test_corrupted_ciphertext_fails_closed_not_crashes(
    db, org_id, session_factory, secrets_manager
):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    from app.db.models import WhatsAppConnection

    connection = db.get(WhatsAppConnection, connection_id)
    connection.access_token_encrypted = connection.access_token_encrypted[:-4] + "zzzz"
    db.commit()
    with pytest.raises(MessagingError) as excinfo:
        send_service.decrypt_access_token(connection, secrets_manager)
    assert excinfo.value.code == MessagingErrorCode.UNCONFIGURED


def test_wrong_connection_context_cannot_decrypt_a_token_meant_for_another_connection(
    db, session_factory, secrets_manager
):
    """Each connection's token is context-bound to its own id. Copying the ciphertext into another
    connection's row (e.g. via direct DB access, or a bug swapping rows) must not decrypt. Two
    different organizations, since the schema allows only one connection per organization."""
    from app.db.models import Organization, WhatsAppConnection

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    with session_factory() as seed_db:
        seed_db.add_all(
            [
                Organization(id=org_a, name="A", slug=f"a-{org_a.hex[:8]}"),
                Organization(id=org_b, name="B", slug=f"b-{org_b.hex[:8]}"),
            ]
        )
        seed_db.commit()
    connection_id_a = seed_connection(
        session_factory, org_a, secrets_manager, phone_number_id="PNID_A"
    )
    connection_id_b = seed_connection(
        session_factory, org_b, secrets_manager, phone_number_id="PNID_B"
    )

    connection_a = db.get(WhatsAppConnection, connection_id_a)
    connection_b = db.get(WhatsAppConnection, connection_id_b)
    # Swap ciphertexts between the two connections (same key, different bound context).
    connection_a.access_token_encrypted, connection_b.access_token_encrypted = (
        connection_b.access_token_encrypted,
        connection_a.access_token_encrypted,
    )
    db.commit()
    with pytest.raises(MessagingError):
        send_service.decrypt_access_token(connection_a, secrets_manager)
    with pytest.raises(MessagingError):
        send_service.decrypt_access_token(connection_b, secrets_manager)


def test_missing_encryption_key_fails_closed():
    from app.core.config import Settings
    from app.core.secrets import SecretManager

    with pytest.raises(SecretError):
        SecretManager.from_settings(
            Settings(_env_file=None, database_url="sqlite://", jwt_secret_key="x" * 40)
        )


def _manager(current: str, current_id: str, previous: str = ""):
    from pydantic import SecretStr

    from app.core.config import Settings
    from app.core.secrets import SecretManager

    return SecretManager.from_settings(
        Settings(
            _env_file=None,
            database_url="sqlite://",
            jwt_secret_key="k" * 40,
            secret_encryption_key=SecretStr(current),
            secret_encryption_key_id=current_id,
            secret_encryption_previous_keys=SecretStr(previous) if previous else None,
        )
    )


def test_token_rotation_produces_a_decryptable_envelope_under_the_new_key(
    db, org_id, session_factory, secrets_manager
):
    import base64

    from app.db.models import WhatsAppConnection
    from tests.whatsapp.conftest import TEST_KEY

    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    connection = db.get(WhatsAppConnection, connection_id)
    old_envelope = connection.access_token_encrypted
    context = send_service.SECRET_CONTEXT.format(connection_id=connection.id)

    new_key = base64.urlsafe_b64encode(bytes(range(200, 232))).decode()
    # The deployment after rotation: a new current key, the old one still trusted as "previous".
    rotated = _manager(new_key, "2", f"1:{TEST_KEY}")

    rotated_token = rotated.rotate(old_envelope, context=context)
    assert rotated.decrypt(rotated_token, context=context) == rotated.decrypt(
        old_envelope, context=context
    )
    assert rotated.needs_rotation(old_envelope)
    assert not rotated.needs_rotation(rotated_token)

    # Once the old key is fully retired (dropped from "previous"), the pre-rotation envelope can
    # no longer be read -- rotation must actually have happened before retiring the old key.
    retired = _manager(new_key, "2")
    with pytest.raises(SecretError):
        retired.decrypt(old_envelope, context=context)


def test_access_token_never_appears_in_the_connect_response_or_status_response(whatsapp_client):
    headers, _ = _tenant(whatsapp_client, "tok-leak@example.com", "LeakOrg")
    resp = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "PNIDX", "waba_id": "WABAX", "access_token": "super-secret-token"},
        headers=headers,
    )
    assert "super-secret-token" not in resp.text
    status = whatsapp_client.get("/api/integrations/whatsapp", headers=headers)
    assert "super-secret-token" not in status.text


def test_messaging_error_messages_never_contain_the_access_token():
    exc = MessagingError(MessagingErrorCode.AUTHENTICATION_ERROR, provider="whatsapp_cloud")
    assert "token" not in str(exc).lower() or "access" not in str(exc).lower()


# --- cross-tenant access --------------------------------------------------------------------------


def test_cross_tenant_message_id_guessing_returns_404_not_403(
    whatsapp_client, session_factory, secrets_manager
):
    headers_a, org_a = _tenant(whatsapp_client, "cta@example.com", "CTA")
    headers_b, org_b = _tenant(whatsapp_client, "ctb@example.com", "CTB")
    org_a_id = uuid.UUID(org_a["id"])
    connection_id = seed_connection(session_factory, org_a_id, secrets_manager)
    with session_factory() as db:
        contact = get_or_create_contact(db, organization_id=org_a_id, phone_e164="+14155551234")
        db.commit()
        conversation = window_service.get_or_create_conversation(
            db, organization_id=org_a_id, contact_id=contact.id, connection_id=connection_id
        )
        db.commit()

    # Org B, authenticated and legitimate, tries org A's real conversation id.
    resp = whatsapp_client.get(f"/api/whatsapp/conversations/{conversation.id}", headers=headers_b)
    assert resp.status_code == 404
    # A random, non-existent id behaves identically (does not confirm existence either way).
    resp2 = whatsapp_client.get(f"/api/whatsapp/conversations/{uuid.uuid4()}", headers=headers_b)
    assert resp2.status_code == 404


def test_cross_tenant_contact_consent_cannot_be_set(
    whatsapp_client, session_factory, secrets_manager
):
    headers_a, org_a = _tenant(whatsapp_client, "cca@example.com", "CCA")
    headers_b, org_b = _tenant(whatsapp_client, "ccb@example.com", "CCB")
    org_b_id = uuid.UUID(org_b["id"])
    seed_connection(session_factory, org_b_id, secrets_manager)
    with session_factory() as db:
        contact = get_or_create_contact(db, organization_id=org_b_id, phone_e164="+14155559999")
        db.commit()
        contact_id = contact.id

    resp = whatsapp_client.post(
        f"/api/whatsapp/contacts/{contact_id}/consent",
        json={"category": "MARKETING", "status": "OPTED_IN", "source": "dashboard_manual"},
        headers=headers_a,
    )
    assert resp.status_code == 404
    # Confirm org B's own consent was NOT affected by org A's attempt.
    with session_factory() as db:
        status = consent_service.get_consent_status(
            db,
            organization_id=org_b_id,
            contact_id=contact_id,
            category=WhatsAppConsentCategory.MARKETING,
        )
        assert status is None


def test_cross_tenant_disconnect_does_not_affect_another_orgs_connection(
    whatsapp_client, session_factory, secrets_manager
):
    headers_a, org_a = _tenant(whatsapp_client, "dca@example.com", "DCA")
    headers_b, org_b = _tenant(whatsapp_client, "dcb@example.com", "DCB")
    org_b_id = uuid.UUID(org_b["id"])
    seed_connection(session_factory, org_b_id, secrets_manager, phone_number_id="DC_PNID_B")

    # Org A has no connection of its own; disconnecting must 404, never touch org B's row.
    resp = whatsapp_client.post("/api/integrations/whatsapp/disconnect", headers=headers_a)
    assert resp.status_code == 404
    status_b = whatsapp_client.get("/api/integrations/whatsapp", headers=headers_b)
    assert status_b.json()["connection"]["status"] == "CONNECTED"


# --- role enforcement ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("POST", "/api/integrations/whatsapp/disconnect", None),
        ("POST", "/api/whatsapp/templates/sync", None),
    ],
)
def test_mutating_endpoints_require_owner_or_admin(whatsapp_client, method, path, json_body):
    owner_headers, org = _tenant(whatsapp_client, "role-owner@example.com", "RoleOrg")
    member_headers = signup_and_login(whatsapp_client, "role-member@example.com")
    add_member(whatsapp_client, owner_headers, org["id"], "role-member@example.com", "MEMBER")
    resp = whatsapp_client.request(method, path, json=json_body, headers=member_headers)
    assert resp.status_code == 403


def test_opt_out_requires_owner_or_admin(whatsapp_client, session_factory, secrets_manager):
    owner_headers, org = _tenant(whatsapp_client, "optout-owner@example.com", "OptOutOrg")
    member_headers = signup_and_login(whatsapp_client, "optout-member@example.com")
    add_member(whatsapp_client, owner_headers, org["id"], "optout-member@example.com", "MEMBER")
    org_id = uuid.UUID(org["id"])
    seed_connection(session_factory, org_id, secrets_manager)
    with session_factory() as db:
        contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155557777")
        db.commit()
        contact_id = contact.id
    resp = whatsapp_client.post(
        f"/api/whatsapp/contacts/{contact_id}/opt-out", json={"reason": "x"}, headers=member_headers
    )
    assert resp.status_code == 403


# --- policy bypass resistance ----------------------------------------------------------------------


def test_opt_out_cannot_be_bypassed_by_a_workflow_retry(
    db, org_id, session_factory, secrets_manager
):
    """A contact who opted out must stay blocked even if the automation layer retries the exact
    same logical send (same idempotency key) after the opt-out was recorded."""
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    from app.db.models import WhatsAppConnection

    connection = db.get(WhatsAppConnection, connection_id)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.SUPPORT,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="test",
        now=NOW,
    )
    conversation = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
    )
    window_service.record_inbound(conversation, now=NOW, window_hours=24)
    db.commit()

    # The contact opts out BEFORE the first send attempt ever reaches the provider.
    consent_service.suppress(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        reason="user_opt_out_stop",
        source="inbound",
        now=NOW,
    )
    db.commit()

    provider = FakeMessagingProvider()
    for attempt_key in ("workflow-exec-1:0", "workflow-exec-1:0"):  # a "retry" reuses the same key
        row = send_service.send_text(
            db,
            organization_id=org_id,
            connection=connection,
            contact=contact,
            outbound=send_service.OutboundText(
                content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key=attempt_key,
            provider=provider,
            access_token="tok",
            now=NOW,
        )
        assert row.status == WhatsAppMessageStatus.FAILED
        assert row.error_code == MessagingErrorCode.POLICY_BLOCKED.value


def test_template_bypass_is_impossible_via_a_text_send_outside_the_window(
    db, org_id, session_factory, secrets_manager
):
    """There is no action/path that sends free-form text outside the window; send_text always
    checks the window, regardless of consent or any other state."""
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    from app.db.models import WhatsAppConnection

    connection = db.get(WhatsAppConnection, connection_id)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.MARKETING,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="test",
        now=NOW,
    )
    db.commit()
    # No inbound message was ever recorded: the window has never been open.
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="buy now!", consent_category=WhatsAppConsentCategory.MARKETING
        ),
        idempotency_key="bypass-key",
        provider=FakeMessagingProvider(),
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.WINDOW_CLOSED.value
