"""Webhook authenticity: the GET verification handshake and the POST signature check.

Verified against current official Meta documentation in this session
(https://developers.facebook.com/docs/graph-api/webhooks/getting-started):

* GET handshake: ``hub.mode=subscribe``, ``hub.verify_token`` must match the configured token,
  echo back ``hub.challenge`` verbatim (as plain text, not JSON).
* POST signature: ``X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body, keyed with the
  Meta App secret>``.
"""

import hashlib
import hmac


def verify_handshake(*, mode: str | None, token: str | None, configured_token: str) -> bool:
    if not configured_token:
        return False
    return (
        mode == "subscribe"
        and token is not None
        and hmac.compare_digest(token.encode("utf-8"), configured_token.encode("utf-8"))
    )


def verify_meta_signature(body: bytes, header_value: str | None, app_secret: str) -> bool:
    if not header_value or not app_secret:
        return False
    prefix, _, hex_digest = header_value.strip().partition("=")
    if prefix != "sha256" or not hex_digest:
        return False
    try:
        provided = bytes.fromhex(hex_digest)
    except ValueError:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, provided)


def sign_meta_body(body: bytes, app_secret: str) -> str:
    """The ``X-Hub-Signature-256`` value Meta would send. Used by the fake provider path and
    tests to simulate an authentic delivery."""
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
