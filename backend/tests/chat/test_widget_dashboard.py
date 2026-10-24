import pytest
from sqlalchemy import select

from app.core.rate_limit import limiter
from app.db.models import ChatWidget
from tests.chat.conftest import open_chat
from tests.helpers import add_member, signup_and_login

WIDGET = "/api/chat/widget"


def test_widget_settings_are_created_on_first_use_with_a_public_key(client, tenants, db):
    a, _ = tenants
    r = client.get(WIDGET, headers=a.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["public_key"].startswith("pk_") and len(body["public_key"]) >= 30
    assert body["enabled"] is True and body["allowed_origins"] == []
    assert body["welcome_message"]  # a sensible default
    assert str(db.scalar(select(ChatWidget.organization_id))) == a.org["id"]


def test_the_key_is_stable_and_unique_per_organization(client, tenants):
    a, b = tenants
    first = client.get(WIDGET, headers=a.headers).json()["public_key"]
    assert client.get(WIDGET, headers=a.headers).json()["public_key"] == first
    assert client.get(WIDGET, headers=b.headers).json()["public_key"] != first


def test_public_keys_are_unguessable_random_values(client, db):
    keys = set()
    for i in range(30):
        limiter.reset()  # registering 30 users in one test would trip the signup rate limit
        headers = signup_and_login(client, f"u{i}@example.com")
        org = client.post("/api/organizations", json={"name": f"Org {i}"}, headers=headers).json()
        keys.add(client.get(WIDGET, headers=headers).json()["public_key"])
        assert org["id"] not in client.get(WIDGET, headers=headers).text  # no tenant id in the key
    assert len(keys) == 30  # no collisions
    assert all(len(k) == 35 for k in keys)  # "pk_" + 32 url-safe characters (192 bits)


def test_every_member_can_read_but_only_managers_can_change(client, tenants):
    a, _ = tenants
    people = {"owner": a.headers}
    for role in ("ADMIN", "MEMBER"):
        email = f"{role.lower()}@example.com"
        people[role.lower()] = signup_and_login(client, email)
        assert add_member(client, a.headers, a.org["id"], email, role).status_code == 201
    for who in ("owner", "admin", "member"):
        assert client.get(WIDGET, headers=people[who]).status_code == 200
    assert (
        client.patch(WIDGET, json={"enabled": False}, headers=people["member"]).status_code == 403
    )
    assert client.patch(WIDGET, json={"enabled": False}, headers=people["admin"]).status_code == 200
    assert client.patch(WIDGET, json={"enabled": True}, headers=people["owner"]).status_code == 200


def test_unauthenticated_requests_are_rejected(client, tenants):
    assert client.get(WIDGET).status_code == 401
    assert client.patch(WIDGET, json={"enabled": False}).status_code == 401


def test_an_organization_cannot_see_or_change_another_organizations_widget(client, tenants):
    a, b = tenants
    key_a = client.get(WIDGET, headers=a.headers).json()["public_key"]
    key_b = client.get(WIDGET, headers=b.headers).json()["public_key"]
    assert key_a != key_b
    r = client.get(WIDGET, headers={**a.headers, "X-Organization-ID": b.org["id"]})
    assert r.status_code == 404 and key_b not in r.text
    client.patch(WIDGET, json={"welcome_message": "A's greeting"}, headers=a.headers)
    assert client.get(WIDGET, headers=b.headers).json()["welcome_message"] != "A's greeting"


def test_settings_can_be_updated_and_welcome_message_reset(client, tenants):
    a, _ = tenants
    r = client.patch(
        WIDGET,
        json={
            "welcome_message": "  Hello from Luna!  ",
            "allowed_origins": ["https://Luna.example/", "https://luna.example"],
        },
        headers=a.headers,
    )
    assert r.status_code == 200
    assert r.json()["welcome_message"] == "Hello from Luna!"
    assert r.json()["allowed_origins"] == ["https://luna.example"]  # normalized and de-duplicated
    reset = client.patch(WIDGET, json={"welcome_message": ""}, headers=a.headers).json()
    assert reset["welcome_message"] == "Hi! How can I help you today?"
    assert reset["allowed_origins"] == ["https://luna.example"]  # untouched fields stay


@pytest.mark.parametrize(
    "origin",
    [
        "luna.example",
        "javascript:alert(1)",
        "https://luna.example/path",
        "https://",
        "ftp://x.example",
        "https://a b.example",
        "*",
    ],
)
def test_invalid_origins_are_rejected(client, tenants, origin):
    a, _ = tenants
    assert (
        client.patch(WIDGET, json={"allowed_origins": [origin]}, headers=a.headers).status_code
        == 422
    )


def test_too_many_origins_and_long_messages_are_rejected(client, tenants):
    a, _ = tenants
    many = [f"https://site{i}.example" for i in range(21)]
    assert (
        client.patch(WIDGET, json={"allowed_origins": many}, headers=a.headers).status_code == 422
    )
    assert (
        client.patch(WIDGET, json={"welcome_message": "x" * 501}, headers=a.headers).status_code
        == 422
    )


def test_disabling_the_widget_turns_the_public_chat_off(client, tenants, keys, chat):
    open_chat(client, keys.a)  # works
    client.patch(WIDGET, json={"enabled": False}, headers=keys.a_headers)
    r = client.post(f"/api/public/chat/{keys.a}/conversations", json={})
    assert r.status_code == 404
    client.patch(WIDGET, json={"enabled": True}, headers=keys.a_headers)
    assert client.post(f"/api/public/chat/{keys.a}/conversations", json={}).status_code == 201


def test_the_dashboard_origin_can_always_preview_the_widget(client, tenants, keys, chat):
    client.patch(WIDGET, json={"allowed_origins": ["https://luna.example"]}, headers=keys.a_headers)
    url = f"/api/public/chat/{keys.a}/config"
    assert client.get(url, headers={"Origin": "http://localhost:3000"}).status_code == 200
    assert client.get(url, headers={"Origin": "http://LOCALHOST:3000/"}).status_code == 200
    assert client.get(url, headers={"Origin": "http://localhost:3001"}).status_code == 403
    assert client.get(url, headers={"Origin": "https://evil.example"}).status_code == 403


def test_allowed_origins_are_enforced_for_the_public_api(client, tenants, keys, chat):
    client.patch(WIDGET, json={"allowed_origins": ["https://luna.example"]}, headers=keys.a_headers)
    url = f"/api/public/chat/{keys.a}/conversations"
    assert client.post(url, json={}, headers={"Origin": "https://luna.example"}).status_code == 201
    assert client.post(url, json={}, headers={"Origin": "https://LUNA.example/"}).status_code == 201
    assert client.post(url, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(url, json={}).status_code == 403  # no Origin header at all
    # other organizations are unaffected by this organization's restriction
    assert (
        client.post(
            f"/api/public/chat/{keys.b}/conversations",
            json={},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 201
    )
