import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import User
from tests.helpers import PASSWORD, signup_and_login


def _register(client, email="ali@example.com", password=PASSWORD, **extra):
    return client.post("/api/auth/register", json={"email": email, "password": password, **extra})


def _login(client, email="ali@example.com", password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# --- registration -------------------------------------------------------------------------


def test_register_success(client):
    r = _register(client, full_name="  Ali Haider ")
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "ali@example.com"
    assert body["full_name"] == "Ali Haider"
    assert uuid.UUID(body["id"])
    assert "password" not in body and "password_hash" not in body


def test_password_is_stored_hashed_never_plaintext(client, db):
    _register(client)
    stored = db.scalar(select(User.password_hash).where(User.email == "ali@example.com"))
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_email_is_normalized_to_lowercase(client, db):
    r = _register(client, email="Ali.Haider@Example.COM")
    assert r.status_code == 201
    assert r.json()["email"] == "ali.haider@example.com"
    assert db.scalar(select(User.email)) == "ali.haider@example.com"


def test_duplicate_email_rejected_case_insensitively(client):
    assert _register(client, email="ali@example.com").status_code == 201
    r = _register(client, email="ALI@example.com")
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


@pytest.mark.parametrize("email", ["", "not-an-email", "a@", "@example.com", "a b@example.com"])
def test_invalid_email_rejected(client, email):
    assert _register(client, email=email).status_code == 422


@pytest.mark.parametrize("password", ["", "short", "123456789", "x" * 129])
def test_weak_or_oversized_password_rejected(client, password):
    r = _register(client, password=password)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][-1] == "password"


def test_register_requires_fields(client):
    assert client.post("/api/auth/register", json={}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "a@example.com"}).status_code == 422


# --- login --------------------------------------------------------------------------------


def test_login_success(client):
    _register(client)
    r = _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] == get_settings().access_token_expire_minutes * 60
    assert body["user"]["email"] == "ali@example.com"
    assert "password_hash" not in body["user"]


def test_login_email_is_case_insensitive(client):
    _register(client)
    assert _login(client, email="ALI@Example.com").status_code == 200


def test_login_token_contains_only_identity(client):
    _register(client)
    token = _login(client).json()["access_token"]
    secret = get_settings().jwt_secret_key.get_secret_value()
    claims = jwt.decode(token, secret, algorithms=["HS256"])
    assert set(claims) == {"sub", "iat", "exp"}


def test_login_wrong_password(client):
    _register(client)
    r = _login(client, password="definitely-wrong-password")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_login_unknown_user_indistinguishable_from_wrong_password(client):
    _register(client)
    wrong_pw = _login(client, password="definitely-wrong-password")
    no_user = _login(client, email="nobody@example.com")
    assert no_user.status_code == wrong_pw.status_code == 401
    assert no_user.json() == wrong_pw.json()


def test_inactive_user_cannot_login(client, db):
    _register(client)
    db.scalar(select(User)).is_active = False
    db.commit()
    r = _login(client)
    assert r.status_code == 401
    assert r.json() == _login(client, email="nobody@example.com").json()


def test_login_is_rate_limited(client):
    limit = get_settings().auth_rate_limit_attempts
    for _ in range(limit):
        assert _login(client, email="nobody@example.com").status_code == 401
    r = _login(client, email="nobody@example.com")
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_register_is_rate_limited(client):
    limit = get_settings().auth_rate_limit_attempts
    for i in range(limit):
        assert _register(client, email=f"u{i}@example.com").status_code == 201
    assert _register(client, email="one-too-many@example.com").status_code == 429


# --- /me and the current_user dependency --------------------------------------------------


def test_me_returns_authenticated_user(client):
    headers = signup_and_login(client, "ali@example.com")
    r = client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["email"] == "ali@example.com"


def test_me_requires_authentication(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    ["Bearer garbage", "Bearer ", "Basic YWxpOnB3", "garbage", "Token abc"],
)
def test_me_rejects_bad_credentials(client, authorization):
    r = client.get("/api/auth/me", headers={"Authorization": authorization})
    assert r.status_code == 401


def _token_for(user_id, **overrides) -> str:
    now = datetime.now(UTC)
    claims = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=5), **overrides}
    secret = get_settings().jwt_secret_key.get_secret_value()
    return jwt.encode(claims, secret, algorithm="HS256")


def test_me_rejects_expired_token(client, db):
    signup_and_login(client, "ali@example.com")
    user = db.scalar(select(User))
    expired = _token_for(user.id, exp=datetime.now(UTC) - timedelta(minutes=1))
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_me_rejects_token_for_unknown_user(client):
    token = _token_for(uuid.uuid4())
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    )


def test_deactivated_user_loses_access_with_existing_token(client, db):
    headers = signup_and_login(client, "ali@example.com")
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    db.scalar(select(User)).is_active = False
    db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401
