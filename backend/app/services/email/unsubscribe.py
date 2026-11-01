"""Phase 9: the one-click email unsubscribe link.

A stateless, signed token -- no database lookup is needed to validate it, so the public
``GET /api/email/unsubscribe`` endpoint needs no authentication and works even for a customer who
has no account. The token is HMAC-SHA256 over ``"{organization_id}:{customer_id}"``, keyed by a
value derived from ``Settings.secret_encryption_key`` (context-separated, never the raw key
itself), so this feature needs no second secret provisioned.

A MARKETING-classified email is refused outright by ``services/email/send.py`` when this is not
configured (no base URL, or no encryption key to derive a signing key from) -- a marketing email
is never sent with no working way to opt out.
"""

import base64
import binascii
import hashlib
import hmac
import uuid

_CONTEXT = b"email-unsubscribe-v1"


def derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8") + _CONTEXT).digest()


def generate_token(*, organization_id: uuid.UUID, customer_id: uuid.UUID, key: bytes) -> str:
    payload = f"{organization_id}:{customer_id}".encode()
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )


def verify_token(token: str, *, key: bytes) -> tuple[uuid.UUID, uuid.UUID] | None:
    """The ``(organization_id, customer_id)`` the token was issued for, or ``None`` if it is
    missing, malformed, expired-looking, or signed with a different key. Never raises -- this
    validates untrusted input from a public, unauthenticated link."""
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_part, signature_part = parts
    try:
        payload = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
        signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
    except (binascii.Error, ValueError):
        return None
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        org_str, customer_str = payload.decode("utf-8").split(":", 1)
        return uuid.UUID(org_str), uuid.UUID(customer_str)
    except (UnicodeDecodeError, ValueError):
        return None


def build_unsubscribe_url(
    *, base_url: str, organization_id: uuid.UUID, customer_id: uuid.UUID, key: bytes
) -> str:
    token = generate_token(organization_id=organization_id, customer_id=customer_id, key=key)
    return f"{base_url.rstrip('/')}/api/email/unsubscribe?token={token}"
