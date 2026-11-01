import json
import uuid
from datetime import UTC, datetime

from app.db.models import WhatsAppConversation
from app.services.messaging.whatsapp import handoff as handoff_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from app.services.messaging.whatsapp.validation import sign_meta_body
from app.workers.queue import InMemoryJobQueue
from tests.helpers import create_org, signup_and_login
from tests.whatsapp.conftest import APP_SECRET, VERIFY_TOKEN, seed_connection

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _tenant(client, email, name):
    headers = signup_and_login(client, email)
    org = create_org(client, headers, name)
    return headers, org


# --- connect / status / disconnect ----------------------------------------------------------------


def test_connect_status_disconnect_round_trip(whatsapp_client):
    headers, _org = _tenant(whatsapp_client, "owner@example.com", "Acme")
    status_before = whatsapp_client.get("/api/integrations/whatsapp", headers=headers)
    assert status_before.status_code == 200
    assert status_before.json()["connection"] is None

    connect = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "PNID1", "waba_id": "WABA1", "access_token": "secret-token"},
        headers=headers,
    )
    assert connect.status_code == 200, connect.text
    body = connect.json()
    assert body["phone_number_id"] == "PNID1"
    assert body["status"] == "CONNECTED"
    assert "access_token" not in body
    assert "access_token_encrypted" not in json.dumps(body)

    status_after = whatsapp_client.get("/api/integrations/whatsapp", headers=headers)
    assert status_after.json()["connection"]["phone_number_id"] == "PNID1"

    disconnect = whatsapp_client.post("/api/integrations/whatsapp/disconnect", headers=headers)
    assert disconnect.status_code == 200
    assert disconnect.json()["connection"] is None


def test_connect_requires_owner_or_admin_role(whatsapp_client):
    owner_headers, org = _tenant(whatsapp_client, "owner2@example.com", "Acme2")
    from tests.helpers import add_member

    member_headers = signup_and_login(whatsapp_client, "member2@example.com")
    add_member(whatsapp_client, owner_headers, org["id"], "member2@example.com", "MEMBER")
    resp = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "PNID2", "waba_id": "WABA2", "access_token": "tok"},
        headers=member_headers,
    )
    assert resp.status_code == 403


def test_a_phone_number_cannot_be_connected_to_two_organizations_at_once(whatsapp_client):
    headers_a, _ = _tenant(whatsapp_client, "ownera@example.com", "A")
    headers_b, _ = _tenant(whatsapp_client, "ownerb@example.com", "B")
    first = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "SHARED_PNID", "waba_id": "W", "access_token": "t"},
        headers=headers_a,
    )
    assert first.status_code == 200
    second = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "SHARED_PNID", "waba_id": "W", "access_token": "t"},
        headers=headers_b,
    )
    assert second.status_code == 409


def test_invalid_default_country_is_rejected(whatsapp_client):
    headers, _ = _tenant(whatsapp_client, "owner3@example.com", "Acme3")
    resp = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={
            "phone_number_id": "PNID3",
            "waba_id": "WABA3",
            "access_token": "tok",
            "default_country": "ZZ",
        },
        headers=headers,
    )
    assert resp.status_code == 422


# --- conversations / messages / templates / consent: tenant isolation -----------------------------


def _seed_org_data(session_factory, secrets_manager, org_id: uuid.UUID):
    connection_id = seed_connection(
        session_factory, org_id, secrets_manager, phone_number_id=f"PNID-{org_id.hex[:8]}"
    )
    with session_factory() as db:
        contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
        db.commit()
        conversation = window_service.get_or_create_conversation(
            db, organization_id=org_id, contact_id=contact.id, connection_id=connection_id
        )
        window_service.record_inbound(conversation, now=NOW, window_hours=24)
        db.commit()
        return contact.id, conversation.id


def test_conversation_and_contact_data_is_isolated_between_tenants(
    whatsapp_client, session_factory, secrets_manager
):
    headers_a, org_a = _tenant(whatsapp_client, "isoa@example.com", "IsoA")
    headers_b, org_b = _tenant(whatsapp_client, "isob@example.com", "IsoB")
    org_a_id, org_b_id = uuid.UUID(org_a["id"]), uuid.UUID(org_b["id"])
    _, conversation_a = _seed_org_data(session_factory, secrets_manager, org_a_id)
    contact_b, _ = _seed_org_data(session_factory, secrets_manager, org_b_id)

    # Org B cannot see org A's conversation (not merely forbidden -- not found).
    resp = whatsapp_client.get(f"/api/whatsapp/conversations/{conversation_a}", headers=headers_b)
    assert resp.status_code == 404

    resp = whatsapp_client.get(
        f"/api/whatsapp/conversations/{conversation_a}/messages", headers=headers_b
    )
    assert resp.status_code == 404

    # Org A cannot see org B's contact's consent.
    resp = whatsapp_client.get(f"/api/whatsapp/contacts/{contact_b}/consent", headers=headers_a)
    assert resp.status_code == 404

    # Each org's own conversation list contains only its own rows.
    list_a = whatsapp_client.get("/api/whatsapp/conversations", headers=headers_a)
    assert len(list_a.json()) == 1
    list_b = whatsapp_client.get("/api/whatsapp/conversations", headers=headers_b)
    assert len(list_b.json()) == 1
    assert list_a.json()[0]["id"] != list_b.json()[0]["id"]


# --- Phase 8: AI settings, auto-reply toggle, human handoff -----------------------------------


def test_auto_reply_toggle_round_trip(whatsapp_client):
    headers, _org = _tenant(whatsapp_client, "ai-toggle@example.com", "AI Toggle Co")
    whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "PNID-AI1", "waba_id": "WABA-AI1", "access_token": "tok"},
        headers=headers,
    )
    on = whatsapp_client.post(
        "/api/integrations/whatsapp/auto-reply", json={"enabled": True}, headers=headers
    )
    assert on.status_code == 200 and on.json()["ai_auto_reply_enabled"] is True
    off = whatsapp_client.post(
        "/api/integrations/whatsapp/auto-reply", json={"enabled": False}, headers=headers
    )
    assert off.status_code == 200 and off.json()["ai_auto_reply_enabled"] is False


def test_ai_settings_round_trip_and_defaults(whatsapp_client):
    headers, _org = _tenant(whatsapp_client, "ai-settings@example.com", "AI Settings Co")
    connect = whatsapp_client.post(
        "/api/integrations/whatsapp/connect",
        json={"phone_number_id": "PNID-AI2", "waba_id": "WABA-AI2", "access_token": "tok"},
        headers=headers,
    )
    defaults = connect.json()
    assert defaults["ai_grounding_required"] is True
    assert defaults["ai_min_confidence"] == "MEDIUM"
    assert defaults["ai_handoff_enabled"] is True
    assert defaults["ai_max_replies_per_window"] == 5

    updated = whatsapp_client.post(
        "/api/integrations/whatsapp/ai-settings",
        json={
            "grounding_required": False,
            "min_confidence": "HIGH",
            "handoff_enabled": False,
            "fallback_message": "Custom fallback.",
            "handoff_message": "Custom handoff.",
            "max_replies_per_window": 3,
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["ai_grounding_required"] is False
    assert body["ai_min_confidence"] == "HIGH"
    assert body["ai_handoff_enabled"] is False
    assert body["ai_fallback_message"] == "Custom fallback."
    assert body["ai_handoff_message"] == "Custom handoff."
    assert body["ai_max_replies_per_window"] == 3


def test_ai_settings_requires_owner_or_admin_role(whatsapp_client):
    owner_headers, org = _tenant(whatsapp_client, "ai-settings-owner@example.com", "Acme3")
    from tests.helpers import add_member

    member_headers = signup_and_login(whatsapp_client, "ai-settings-member@example.com")
    add_member(
        whatsapp_client, owner_headers, org["id"], "ai-settings-member@example.com", "MEMBER"
    )
    resp = whatsapp_client.post(
        "/api/integrations/whatsapp/ai-settings",
        json={
            "grounding_required": True,
            "min_confidence": "MEDIUM",
            "handoff_enabled": True,
            "fallback_message": None,
            "handoff_message": None,
            "max_replies_per_window": 5,
        },
        headers=member_headers,
    )
    assert resp.status_code == 403


def test_handoff_claim_and_resolve_round_trip(whatsapp_client, session_factory, secrets_manager):
    headers, org = _tenant(whatsapp_client, "handoff@example.com", "Handoff Co")
    org_id = uuid.UUID(org["id"])
    _, conversation_id = _seed_org_data(session_factory, secrets_manager, org_id)

    with session_factory() as db:
        conv = db.get(WhatsAppConversation, conversation_id)
        handoff_service.trigger_handoff(
            db,
            InMemoryJobQueue(),
            conversation=conv,
            reason="customer_requested_human",
            now=NOW,
        )

    needs_human = whatsapp_client.get(
        f"/api/whatsapp/conversations/{conversation_id}", headers=headers
    )
    assert needs_human.json()["ai_state"] == "HUMAN_REQUIRED"
    assert needs_human.json()["handoff_reason"] == "customer_requested_human"

    claimed = whatsapp_client.post(
        f"/api/whatsapp/conversations/{conversation_id}/handoff/claim", headers=headers
    )
    assert claimed.status_code == 200 and claimed.json()["ai_state"] == "HUMAN_ACTIVE"

    resolved = whatsapp_client.post(
        f"/api/whatsapp/conversations/{conversation_id}/handoff/resolve", headers=headers
    )
    assert resolved.status_code == 200
    assert resolved.json()["ai_state"] == "AI_ACTIVE"
    assert resolved.json()["handoff_reason"] is None


def test_handoff_actions_are_tenant_isolated(whatsapp_client, session_factory, secrets_manager):
    headers_a, org_a = _tenant(whatsapp_client, "handoff-a@example.com", "HandoffA")
    headers_b, org_b = _tenant(whatsapp_client, "handoff-b@example.com", "HandoffB")
    _, conversation_a = _seed_org_data(session_factory, secrets_manager, uuid.UUID(org_a["id"]))

    resp = whatsapp_client.post(
        f"/api/whatsapp/conversations/{conversation_a}/handoff/claim", headers=headers_b
    )
    assert resp.status_code == 404
    resp = whatsapp_client.post(
        f"/api/whatsapp/conversations/{conversation_a}/handoff/resolve", headers=headers_b
    )
    assert resp.status_code == 404


def test_consent_get_set_opt_out_round_trip(whatsapp_client, session_factory, secrets_manager):
    headers, org = _tenant(whatsapp_client, "consent@example.com", "ConsentOrg")
    org_id = uuid.UUID(org["id"])
    contact_id, _ = _seed_org_data(session_factory, secrets_manager, org_id)

    initial = whatsapp_client.get(f"/api/whatsapp/contacts/{contact_id}/consent", headers=headers)
    assert initial.status_code == 200
    body = initial.json()
    assert body["suppressed"] is False
    assert all(c["status"] is None for c in body["consents"])  # UNKNOWN by default

    set_resp = whatsapp_client.post(
        f"/api/whatsapp/contacts/{contact_id}/consent",
        json={"category": "MARKETING", "status": "OPTED_IN", "source": "dashboard_manual"},
        headers=headers,
    )
    assert set_resp.status_code == 200
    marketing = next(c for c in set_resp.json()["consents"] if c["category"] == "MARKETING")
    assert marketing["status"] == "OPTED_IN"

    opt_out = whatsapp_client.post(
        f"/api/whatsapp/contacts/{contact_id}/opt-out",
        json={"reason": "user_opt_out_stop"},
        headers=headers,
    )
    assert opt_out.status_code == 200
    assert opt_out.json()["suppressed"] is True
    # Opting out (suppression) leaves the per-category consent record untouched.
    marketing_after = next(c for c in opt_out.json()["consents"] if c["category"] == "MARKETING")
    assert marketing_after["status"] == "OPTED_IN"


def test_template_sync_is_refused_while_the_fake_provider_is_active(
    whatsapp_client, session_factory, secrets_manager
):
    headers, org = _tenant(whatsapp_client, "tmpl@example.com", "TmplOrg")
    org_id = uuid.UUID(org["id"])
    seed_connection(session_factory, org_id, secrets_manager)
    resp = whatsapp_client.post("/api/whatsapp/templates/sync", headers=headers)
    # The offline/test provider must never claim a real sync happened.
    assert resp.status_code == 503


def test_automation_usage_lists_only_workflows_with_whatsapp_actions(whatsapp_client, db):
    from app.db.models import AutomationWorkflow, WorkflowStatus

    headers, org = _tenant(whatsapp_client, "usage@example.com", "UsageOrg")
    org_id = uuid.UUID(org["id"])
    with_whatsapp = AutomationWorkflow(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="WA flow",
        status=WorkflowStatus.ACTIVE,
        version=1,
        definition={
            "trigger": {"event": "website_lead_captured"},
            "conditions": [],
            "actions": [
                {"type": "send_whatsapp_text", "content": "hi", "consent_category": "SUPPORT"}
            ],
        },
    )
    without_whatsapp = AutomationWorkflow(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="Other flow",
        status=WorkflowStatus.ACTIVE,
        version=1,
        definition={
            "trigger": {"event": "website_lead_captured"},
            "conditions": [],
            "actions": [{"type": "log", "message": "hi"}],
        },
    )
    db.add_all([with_whatsapp, without_whatsapp])
    db.commit()

    resp = whatsapp_client.get("/api/whatsapp/automation-usage", headers=headers)
    assert resp.status_code == 200
    names = [row["name"] for row in resp.json()]
    assert names == ["WA flow"]


# --- public webhook endpoint (real HTTP) -----------------------------------------------------------


def test_webhook_get_verification_handshake(whatsapp_client):
    resp = whatsapp_client.get(
        "/api/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1234"},
    )
    assert resp.status_code == 200
    assert resp.text == "1234"


def test_webhook_get_verification_rejects_wrong_token(whatsapp_client):
    resp = whatsapp_client.get(
        "/api/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1234"},
    )
    assert resp.status_code == 403


def test_webhook_post_requires_a_valid_signature(whatsapp_client):
    payload = {"object": "whatsapp_business_account", "entry": []}
    body = json.dumps(payload).encode()
    resp = whatsapp_client.post(
        "/api/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}
    )
    assert resp.status_code == 401


def test_webhook_post_with_a_valid_signature_is_accepted(
    whatsapp_client, session_factory, secrets_manager, org_id
):
    seed_connection(session_factory, org_id, secrets_manager)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID_TEST"},
                            "contacts": [{"profile": {"name": "Jane"}, "wa_id": "14155551234"}],
                            "messages": [
                                {
                                    "from": "14155551234",
                                    "id": "wamid.APITEST",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    sig = sign_meta_body(body, APP_SECRET)
    resp = whatsapp_client.post(
        "/api/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_webhook_post_rejects_an_oversized_body(whatsapp_client, whatsapp_settings):
    whatsapp_settings.whatsapp_webhook_max_body_bytes = 100
    oversized = json.dumps(
        {"object": "whatsapp_business_account", "entry": [], "pad": "x" * 500}
    ).encode()
    sig = sign_meta_body(oversized, APP_SECRET)
    resp = whatsapp_client.post(
        "/api/webhooks/whatsapp",
        content=oversized,
        headers={"X-Hub-Signature-256": sig, "Content-Length": str(len(oversized))},
    )
    assert resp.status_code == 413
