import json
import logging
import uuid

from pydantic import SecretStr
from sqlalchemy import select

from app.core.rate_limit import limiter
from app.db.models import Conversation, Message
from tests.chat.conftest import open_chat, say
from tests.helpers import signup_and_login

DASHBOARD_ORIGIN = "http://localhost:3000"  # in CORS_ORIGINS by default
CUSTOMER_SITE = "https://some-customer-shop.example"


# --- CORS --------------------------------------------------------------------------------------------------------


def test_preflight_from_any_website_is_answered_without_touching_the_app(client, keys):
    r = client.options(
        f"/api/public/chat/{keys.a}/messages",
        headers={
            "Origin": CUSTOMER_SITE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-conversation-token",
        },
    )
    assert r.status_code == 204 and r.content == b""
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "content-type" in allowed and "x-conversation-token" in allowed
    assert "access-control-allow-credentials" not in r.headers  # public chat never uses cookies


def test_actual_responses_carry_exactly_one_public_cors_header(client, keys, chat):
    for origin in (
        CUSTOMER_SITE,
        DASHBOARD_ORIGIN,
    ):  # the dashboard origin is also globally allowed
        r = client.get(f"/api/public/chat/{keys.a}/config", headers={"Origin": origin})
        assert r.headers.get_list("access-control-allow-origin") == ["*"]
        assert "access-control-allow-credentials" not in r.headers
        assert "Retry-After" in r.headers.get("access-control-expose-headers", "")


def test_error_responses_are_readable_by_the_widget_too(client, keys):
    r = client.get("/api/public/chat/pk_" + "x" * 32 + "/config", headers={"Origin": CUSTOMER_SITE})
    assert r.status_code == 404 and r.headers["access-control-allow-origin"] == "*"


def test_dashboard_cors_behavior_is_unchanged(client, tenants):
    allowed = client.get("/api/auth/me", headers={"Origin": DASHBOARD_ORIGIN, **tenants[0].headers})
    assert allowed.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    denied = client.get("/api/auth/me", headers={"Origin": CUSTOMER_SITE, **tenants[0].headers})
    assert (
        "access-control-allow-origin" not in denied.headers
    )  # customer sites get no dashboard CORS


def test_public_responses_are_not_cacheable_and_not_sniffable(client, keys, chat):
    r = client.get(f"/api/public/chat/{keys.a}/config")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


# --- request size -----------------------------------------------------------------------------------------------------


def test_oversized_bodies_are_rejected_before_parsing(client, keys, chat):
    conversation_id, token = open_chat(client, keys.a)
    body = json.dumps({"conversation_id": conversation_id, "content": "x" * 17_000})
    r = client.post(
        f"/api/public/chat/{keys.a}/messages",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Conversation-Token": token,
            "Origin": CUSTOMER_SITE,
        },
    )
    assert r.status_code == 413 and r.json() == {"detail": "Request body is too large"}
    assert r.headers["access-control-allow-origin"] == "*"


def test_streamed_bodies_without_a_content_length_are_cut_off_too(client, keys, chat):
    def endless():
        for _ in range(100):
            yield b"x" * 1024  # 100 KB, no Content-Length declared

    r = client.post(
        f"/api/public/chat/{keys.a}/conversations",
        content=endless(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_bodies_within_the_limit_are_accepted(client, keys, chat):
    conversation_id, token = open_chat(client, keys.a)
    r = say(client, keys.a, conversation_id, token, "x" * 2000)  # the maximum message length
    assert r.status_code == 200


def test_the_size_cap_applies_only_to_the_public_api(client, tenants):
    a, _ = tenants
    r = client.post(
        "/api/organizations", json={"name": "Big", "business_type": "y" * 50}, headers=a.headers
    )
    assert r.status_code == 201  # unaffected (and well under any limit)


# --- rate limiting (the existing limiter, extended; no second implementation) ----------------------------------


def test_messages_are_rate_limited_per_client_and_key(client, keys, chat, app_settings):
    app_settings.public_chat_rate_limit = 3
    conversation_id, token = open_chat(client, keys.a)
    for _ in range(3):
        assert say(client, keys.a, conversation_id, token, "shipping?").status_code == 200
    blocked = say(client, keys.a, conversation_id, token, "shipping?")
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    assert blocked.json() == {
        "detail": "You are sending messages too quickly. Please wait a moment."
    }
    assert blocked.headers["access-control-allow-origin"] == "*"
    # another business's widget has its own budget for the same visitor
    b_id, b_token = open_chat(client, keys.b)
    assert say(client, keys.b, b_id, b_token, "shipping?").status_code == 200
    limiter.reset()
    assert say(client, keys.a, conversation_id, token, "shipping?").status_code == 200


def test_new_conversations_are_rate_limited(client, keys, chat, app_settings):
    app_settings.public_chat_conversation_limit = 2
    for _ in range(2):
        assert client.post(f"/api/public/chat/{keys.a}/conversations", json={}).status_code == 201
    r = client.post(f"/api/public/chat/{keys.a}/conversations", json={})
    assert r.status_code == 429 and "conversations" in r.json()["detail"]


def test_guessing_keys_is_throttled_per_client_not_per_key(client, keys, app_settings):
    """Each random key is a new per-key bucket, so scanning is stopped by the per-client limit."""
    app_settings.public_chat_rate_limit = 2  # per-client budget is three times this
    statuses = [
        client.get(f"/api/public/chat/pk_{uuid.uuid4().hex}xxxxxxxx/config").status_code
        for _ in range(8)
    ]
    assert statuses[:6] == [404] * 6 and statuses[6:] == [429, 429]


def test_the_limit_is_configurable_per_environment(client, keys, chat, app_settings):
    app_settings.public_chat_rate_limit = 50
    conversation_id, token = open_chat(client, keys.a)
    assert all(
        say(client, keys.a, conversation_id, token, "shipping?").status_code == 200
        for _ in range(10)
    )


# --- secrets and sensitive data ------------------------------------------------------------------------------------


API_KEY = "sk-test-SUPERSECRET-9876543210"


def test_secrets_never_appear_in_responses_logs_or_stored_data(
    client, keys, chat, llm, app_settings, db, caplog
):
    caplog.set_level(logging.DEBUG)
    app_settings.openai_api_key = SecretStr(API_KEY)
    conversation_id, token = open_chat(
        client,
        keys.a,
        headers={
            "Authorization": "Bearer jwt-should-never-be-logged",
            "Cookie": "session=cookie-value-123",
        },
    )
    llm.replies = ["Brand A ships within two business days."]
    responses = [
        say(
            client,
            keys.a,
            conversation_id,
            token,
            "shipping?",
            headers={
                "Authorization": "Bearer jwt-should-never-be-logged",
                "Cookie": "session=cookie-value-123",
            },
        ),
        client.get(f"/api/public/chat/{keys.a}/config"),
        client.get(
            f"/api/public/chat/{keys.a}/conversations/{conversation_id}",
            headers={"X-Conversation-Token": token},
        ),
    ]
    llm.fail_with = __import__("app.services.ai.provider", fromlist=["LLMError"]).LLMError(
        "auth", f"rejected {API_KEY}"
    )
    responses.append(say(client, keys.a, conversation_id, token, "again shipping?"))
    assert responses[-1].status_code == 503

    everything = "\n".join(r.text + str(dict(r.headers)) for r in responses) + caplog.text
    stored = "\n".join(f"{m.content} {m.meta}" for m in db.scalars(select(Message))) + "\n".join(
        f"{c.token_hash}" for c in db.scalars(select(Conversation))
    )
    for secret in (API_KEY, "jwt-should-never-be-logged", "cookie-value-123", token):
        assert secret not in everything, secret
        assert secret not in stored, secret


def test_conversation_tokens_are_random_unique_and_only_hashed_at_rest(client, keys, chat, db):
    tokens = {open_chat(client, keys.a)[1] for _ in range(8)}
    limiter.reset()
    tokens |= {open_chat(client, keys.a)[1] for _ in range(8)}
    assert len(tokens) == 16 and all(len(t) >= 43 for t in tokens)  # 256 bits, URL-safe
    hashes = set(db.scalars(select(Conversation.token_hash)))
    assert len(hashes) == 16 and not (hashes & tokens)
    assert all(len(h) == 64 for h in hashes)  # SHA-256 hex


def test_token_comparison_is_constant_time_on_every_path(client, keys, chat, monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr("app.services.chat.service.hmac.compare_digest", spy)
    conversation_id, token = open_chat(client, keys.a)
    client.get(
        f"/api/public/chat/{keys.a}/conversations/{conversation_id}",
        headers={"X-Conversation-Token": token},
    )
    client.get(
        f"/api/public/chat/{keys.a}/conversations/{uuid.uuid4()}",
        headers={"X-Conversation-Token": token},
    )  # no such conversation
    client.get(f"/api/public/chat/{keys.a}/conversations/{conversation_id}")  # no token
    assert (
        len(calls) == 3
    )  # compared (against a dummy hash if need be) whether or not the conversation exists


def test_public_errors_are_json_without_stack_traces(client, keys, chat):
    for r in (
        client.get("/api/public/chat/pk_" + "x" * 32 + "/config"),
        client.post(
            f"/api/public/chat/{keys.a}/messages",
            content=b"{not json",
            headers={"content-type": "application/json"},
        ),
        client.get(f"/api/public/chat/{keys.a}/conversations/not-a-uuid"),
    ):
        assert r.headers["content-type"].startswith("application/json")
        assert (
            "Traceback" not in r.text
            and 'File "' not in r.text
            and "sqlalchemy" not in r.text.lower()
        )


def test_cannot_enumerate_organizations_through_the_public_api(client, keys, chat):
    # nothing under the public prefix lists or resolves organizations
    for path in (
        "/api/public",
        "/api/public/",
        "/api/public/chat",
        "/api/public/chat/",
        "/api/public/organizations",
    ):
        assert client.get(path).status_code in (404, 405)
    # and an authenticated user from another organization gets nothing extra either
    other = signup_and_login(client, "stranger@example.com")
    assert client.get("/api/public/chat", headers=other).status_code in (404, 405)


def test_a_body_that_is_valid_json_up_to_the_limit_never_reaches_the_endpoint(
    client, keys, chat, llm, db
):
    """The old failure mode: valid JSON followed by padding must not be acted on at all."""
    conversation_id, token = open_chat(client, keys.a)
    valid = json.dumps({"conversation_id": conversation_id, "content": "shipping?"})
    padded = valid + " " * 20_000

    def chunks():
        yield padded.encode()[:8000]
        yield padded.encode()[8000:]

    r = client.post(
        f"/api/public/chat/{keys.a}/messages",
        content=chunks(),
        headers={"content-type": "application/json", "X-Conversation-Token": token},
    )
    assert r.status_code == 413
    assert llm.calls == [] and db.scalar(select(Message)) is None  # no side effects whatsoever
