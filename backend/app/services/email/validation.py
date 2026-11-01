"""Webhook authenticity for inbound email provider deliveries.

Resend signs webhooks using Svix (https://docs.svix.com/receiving/verifying-payloads/how):
``svix-id``, ``svix-timestamp`` and ``svix-signature`` headers; the signed content is
``"{svix-id}.{svix-timestamp}.{raw body}"``, HMAC-SHA256 keyed with the base64 portion of a
``whsec_...`` secret, base64-encoded in the ``v1,<signature>`` form (``svix-signature`` may list
several space-separated candidates; any match is accepted, which is how Svix supports secret
rotation). A timestamp outside a bounded tolerance is rejected (replay protection), mirroring the
WhatsApp module's fail-closed style (``app/services/messaging/whatsapp/validation.py``).
"""

import base64
import binascii
import hmac
import time
from hashlib import sha256

MAX_TIMESTAMP_SKEW_SECONDS = 300


def verify_svix_signature(
    *,
    body: bytes,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    secret: str,
    now: float | None = None,
) -> bool:
    if not (svix_id and svix_timestamp and svix_signature and secret):
        return False
    if not svix_timestamp.isdigit():
        return False
    timestamp = int(svix_timestamp)
    if abs((now if now is not None else time.time()) - timestamp) > MAX_TIMESTAMP_SKEW_SECONDS:
        return False
    key_material = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(key_material + "=" * (-len(key_material) % 4))
    except (binascii.Error, ValueError):
        return False
    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed_content, sha256).digest()).decode("ascii")
    for candidate in svix_signature.split():
        _, _, value = candidate.partition(",")
        if value and hmac.compare_digest(value, expected):
            return True
    return False


def sign_svix_body(*, svix_id: str, svix_timestamp: str, body: bytes, secret: str) -> str:
    """The ``svix-signature`` value a real delivery would carry. Used by tests/E2E to simulate an
    authentic delivery against the fake provider path."""
    key_material = secret.removeprefix("whsec_")
    key = base64.b64decode(key_material + "=" * (-len(key_material) % 4))
    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + body
    signature = base64.b64encode(hmac.new(key, signed_content, sha256).digest()).decode("ascii")
    return f"v1,{signature}"
