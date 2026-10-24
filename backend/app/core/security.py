import uuid
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

# Pinned, not configurable: an attacker-influenced algorithm setting is a classic JWT footgun.
JWT_ALGORITHM = "HS256"

# Argon2id with argon2-cffi's defaults (RFC 9106 low-memory profile).
_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    """The token is malformed, expired, or otherwise not acceptable."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@cache
def dummy_password_hash() -> str:
    """Verified against for unknown emails, so login timing doesn't reveal which accounts exist."""
    return _hasher.hash("bizflow-timing-equalizer")


def create_access_token(user_id: uuid.UUID) -> str:
    """Identity only (sub, iat, exp). Org and role are resolved server-side on each request."""
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret_key.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise InvalidTokenError from exc
