import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def _secret() -> str:
    return get_settings().jwt_secret_key.get_secret_value()


def test_hash_is_argon2id_salted_and_not_plaintext():
    h1, h2 = hash_password("s3cret-passphrase"), hash_password("s3cret-passphrase")
    assert h1.startswith("$argon2id$")
    assert "s3cret-passphrase" not in h1
    assert h1 != h2  # per-hash salt


def test_verify_password():
    h = hash_password("s3cret-passphrase")
    assert verify_password("s3cret-passphrase", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("anything", "not-a-valid-hash") is False


def test_token_roundtrip_and_minimal_claims():
    uid = uuid.uuid4()
    token = create_access_token(uid)
    assert decode_access_token(token) == uid
    claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    assert set(claims) == {"sub", "iat", "exp"}  # identity only, nothing business-related


def _encode(claims: dict, key: str | None = None, alg: str = "HS256") -> str:
    return jwt.encode(claims, key or _secret(), algorithm=alg)


def _claims(**overrides) -> dict:
    now = datetime.now(UTC)
    base = {"sub": str(uuid.uuid4()), "iat": now, "exp": now + timedelta(minutes=5)}
    return {**base, **overrides}


@pytest.mark.parametrize(
    "token",
    [
        "",
        "garbage",
        _encode(_claims(exp=datetime.now(UTC) - timedelta(seconds=1))),  # expired
        _encode(_claims(), key="y" * 48),  # signed with the wrong key
        _encode(_claims(), alg="HS512"),  # different algorithm than the one we pin
        _encode({k: v for k, v in _claims().items() if k != "exp"}),  # no expiry
        _encode({k: v for k, v in _claims().items() if k != "sub"}),  # no subject
        _encode(_claims(sub="not-a-uuid")),
    ],
)
def test_invalid_tokens_rejected(token):
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_unsigned_alg_none_token_rejected():
    token = jwt.encode(_claims(), key=None, algorithm="none")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_tampered_token_rejected():
    token = create_access_token(uuid.uuid4())
    header, payload, sig = token.split(".")
    forged = create_access_token(uuid.uuid4()).split(".")[1]
    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{forged}.{sig}")
    assert payload != forged
