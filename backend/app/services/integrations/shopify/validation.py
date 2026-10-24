"""Input validation for everything that comes from outside: shop domains, Shopify ids, signatures.

The shop domain is the SSRF-critical input: BizFlow makes server-to-server requests to
``https://<shop>/...``. It is therefore accepted only in the exact form Shopify uses
(``<handle>.myshopify.com``), never as a free-form URL: no other host, port, userinfo, path, IP
address, ``localhost``, internationalized name or nested subdomain can get through.
"""

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit

from app.services.integrations.shopify.errors import InvalidShopDomain

# Shopify's documented pattern, anchored at both ends. Dots are not allowed inside the handle, so
# "evil.com.myshopify.com" and "a.b.myshopify.com" are rejected.
_SHOP_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
_ADMIN_STORE_PATH = re.compile(r"^/store/([a-z0-9][a-z0-9\-]*)/?$")
MAX_HANDLE_LENGTH = 60
_GID = re.compile(r"^gid://shopify/([A-Za-z]+)/(\d{1,19})(?:\?.*)?$")


def normalize_shop_domain(raw: str) -> str:
    """Return ``<handle>.myshopify.com`` or raise ``InvalidShopDomain``.

    Accepts a full ``my-store.myshopify.com``, optionally with a scheme and one trailing slash, or
    the admin address merchants copy from their browser,
    ``https://admin.shopify.com/store/my-store``. Anything else is rejected.
    """
    if not isinstance(raw, str):
        raise InvalidShopDomain("shop must be text")
    text = raw.strip()
    if not text or len(text) > 255 or not text.isascii():
        raise InvalidShopDomain("empty, too long or non-ASCII shop")
    if any(ch.isspace() or ord(ch) < 32 or ch in "\\%" for ch in text):
        raise InvalidShopDomain("shop contains forbidden characters")
    lowered = text.lower()

    if "://" in lowered:
        parts = urlsplit(lowered)
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            raise InvalidShopDomain("unsupported scheme or credentials in shop")
        if parts.port is not None or parts.query or parts.fragment:
            raise InvalidShopDomain("shop must not include a port or query")
        host = parts.hostname or ""
        if host == "admin.shopify.com":
            match = _ADMIN_STORE_PATH.match(parts.path)
            if not match:
                raise InvalidShopDomain("unrecognised Shopify admin address")
            host = f"{match.group(1)}.myshopify.com"
        elif parts.path not in ("", "/"):
            raise InvalidShopDomain("shop must be a bare domain")
    else:
        host = lowered[:-1] if lowered.endswith("/") else lowered

    if not _SHOP_DOMAIN.match(host):
        raise InvalidShopDomain("not a myshopify.com store domain")
    if len(host.removesuffix(".myshopify.com")) > MAX_HANDLE_LENGTH:
        raise InvalidShopDomain("store handle is too long")
    return host


def is_valid_shop_domain(raw: str) -> bool:
    try:
        normalize_shop_domain(raw)
    except InvalidShopDomain:
        return False
    return True


def parse_gid(gid: object, expected_type: str) -> int | None:
    """Numeric id from ``gid://shopify/<Type>/<id>``; None if it is anything else."""
    if not isinstance(gid, str):
        return None
    match = _GID.match(gid)
    if match is None or match.group(1) != expected_type:
        return None
    return int(match.group(2))


def to_int_id(value: object) -> int | None:
    """A numeric resource id from a webhook payload (an int, or digits as text)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 2**63 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        number = int(value)
        return number if 0 < number < 2**63 else None
    return None


# --- signatures ---------------------------------------------------------------------------------


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def oauth_query_message(query: Mapping[str, str]) -> str:
    """The string Shopify signs for an OAuth callback: every parameter except ``hmac``, sorted by
    name, as ``key=value`` joined by ``&``."""
    return "&".join(f"{key}={value}" for key, value in sorted(query.items()) if key != "hmac")


def verify_oauth_hmac(query: Mapping[str, str], client_secret: str) -> bool:
    """Verify the callback signature (hex HMAC-SHA256 with the app's client secret)."""
    provided = query.get("hmac", "")
    if not provided or not client_secret:
        return False
    expected = hmac.new(
        client_secret.encode("utf-8"), oauth_query_message(query).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return _constant_time_equal(expected, provided.lower())


def sign_oauth_query(query: Mapping[str, str], client_secret: str) -> str:
    """The hex signature Shopify would attach. Used by the local fake Shopify and in tests."""
    return hmac.new(
        client_secret.encode("utf-8"), oauth_query_message(query).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def parse_query_string(raw_query: str) -> dict[str, str]:
    """Query parameters as a dict, rejecting duplicates (an attacker repeating ``shop``/``code``
    must not be able to make signature verification and later use see different values)."""
    pairs = parse_qsl(raw_query, keep_blank_values=True, max_num_fields=50)
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidShopDomain(f"duplicate query parameter '{key}'")
        result[key] = value
    return result


def verify_webhook_hmac(body: bytes, header_value: str | None, client_secret: str) -> bool:
    """Verify ``X-Shopify-Hmac-Sha256``: base64 of HMAC-SHA256(raw body, client secret)."""
    if not header_value or not client_secret:
        return False
    try:
        provided = base64.b64decode(header_value.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    expected = hmac.new(client_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, provided)


def sign_webhook_body(body: bytes, client_secret: str) -> str:
    """The base64 signature Shopify would send for ``body``. Used by the fake Shopify and tests."""
    digest = hmac.new(client_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")
