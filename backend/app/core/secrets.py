"""Encryption of secrets at rest (integration tokens, and later per-organization provider keys).

Standard authenticated encryption, nothing invented: AES-256-GCM from the ``cryptography`` library
with a fresh random 96-bit nonce per encryption. Each ciphertext is an envelope

    bf1:<key id>:<urlsafe base64 of nonce || ciphertext || tag>

* **Context binding.** The caller passes a ``context`` string (for example
  ``shopify:access_token:<connection id>``) that is authenticated but not stored. A ciphertext
  copied into another row, column or tenant fails to decrypt, so database access alone cannot be
  used to swap secrets between records.
* **Key versioning.** The envelope names the key that produced it. ``SECRET_ENCRYPTION_KEY`` (with
  id ``SECRET_ENCRYPTION_KEY_ID``) encrypts everything new; ``SECRET_ENCRYPTION_PREVIOUS_KEYS``
  (``id:key,id:key``) can still decrypt, so keys rotate without downtime: deploy the new key as
  current and the old one as previous, re-encrypt with ``rotate()``, then retire the old key.
* **Fail closed.** A missing, malformed or wrong key, a tampered or truncated envelope, an unknown
  key id and a wrong context are all ``SecretError``; nothing falls back to plaintext. Errors never
  contain plaintext, ciphertext or key material.

Keys are 32 random bytes, base64 encoded. Generate one with:

    python -c "import secrets,base64; \
print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
"""

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Mapping
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings

ENVELOPE_VERSION = "bf1"
KEY_BYTES = 32
NONCE_BYTES = 12
MAX_PLAINTEXT_BYTES = 8192
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_AAD_PREFIX = b"bizflow/secret/v1|"


class SecretError(Exception):
    """A secret could not be encrypted or decrypted. ``code`` is a stable reason; the message is
    safe to log (it never contains plaintext, ciphertext or key material)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _decode_key(raw: str, label: str) -> bytes:
    text = raw.strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise SecretError("invalid_key", f"{label} is not valid base64") from None
    if len(key) != KEY_BYTES:
        raise SecretError("invalid_key", f"{label} must decode to exactly {KEY_BYTES} bytes")
    return key


def parse_keys(
    current_key: str, current_key_id: str, previous: str = ""
) -> tuple[str, dict[str, bytes]]:
    """Validate configuration and return ``(current id, {id: key bytes})``."""
    if not _KEY_ID.match(current_key_id):
        raise SecretError("invalid_key", "SECRET_ENCRYPTION_KEY_ID must be 1-16 of A-Z a-z 0-9 _ -")
    keys = {current_key_id: _decode_key(current_key, "SECRET_ENCRYPTION_KEY")}
    for item in (part.strip() for part in previous.split(",")):
        if not item:
            continue
        key_id, sep, key = item.partition(":")
        if not sep or not _KEY_ID.match(key_id):
            raise SecretError("invalid_key", "SECRET_ENCRYPTION_PREVIOUS_KEYS must be id:key pairs")
        if key_id in keys:
            raise SecretError("invalid_key", f"key id '{key_id}' is configured twice")
        keys[key_id] = _decode_key(key, f"previous key '{key_id}'")
    return current_key_id, keys


class SecretManager:
    def __init__(self, current_key_id: str, keys: Mapping[str, bytes]) -> None:
        if current_key_id not in keys:
            raise SecretError("invalid_key", "the current key id has no key")
        self._current_id = current_key_id
        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys.items()}

    def __repr__(self) -> str:  # never reveal key material, even in debug output
        return f"SecretManager(current_key_id={self._current_id!r}, keys={len(self._ciphers)})"

    @classmethod
    def from_settings(cls, settings: Settings) -> "SecretManager":
        if settings.secret_encryption_key is None or not (
            settings.secret_encryption_key.get_secret_value().strip()
        ):
            raise SecretError(
                "not_configured",
                "SECRET_ENCRYPTION_KEY is not set; integration secrets cannot be stored or read",
            )
        previous = (
            settings.secret_encryption_previous_keys.get_secret_value()
            if settings.secret_encryption_previous_keys
            else ""
        )
        current_id, keys = parse_keys(
            settings.secret_encryption_key.get_secret_value(),
            settings.secret_encryption_key_id,
            previous,
        )
        return cls(current_id, keys)

    @property
    def current_key_id(self) -> str:
        return self._current_id

    @staticmethod
    def _aad(context: str) -> bytes:
        if not context:
            raise SecretError("invalid_context", "a non-empty context is required")
        return _AAD_PREFIX + context.encode("utf-8")

    def encrypt(self, plaintext: str, *, context: str) -> str:
        data = plaintext.encode("utf-8")
        if not data:
            raise SecretError("invalid_plaintext", "refusing to encrypt an empty secret")
        if len(data) > MAX_PLAINTEXT_BYTES:
            raise SecretError("invalid_plaintext", "secret is too large")
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._ciphers[self._current_id].encrypt(nonce, data, self._aad(context))
        body = base64.urlsafe_b64encode(nonce + sealed).decode("ascii")
        return f"{ENVELOPE_VERSION}:{self._current_id}:{body}"

    @staticmethod
    def _split(token: str) -> tuple[str, bytes]:
        parts = token.split(":")
        if len(parts) != 3 or parts[0] != ENVELOPE_VERSION or not _KEY_ID.match(parts[1]):
            raise SecretError("malformed", "not a recognised secret envelope")
        try:
            raw = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        except (binascii.Error, ValueError):
            raise SecretError("malformed", "not a recognised secret envelope") from None
        if len(raw) < NONCE_BYTES + 16 + 1:  # nonce + tag + at least one byte
            raise SecretError("malformed", "secret envelope is truncated")
        return parts[1], raw

    def decrypt(self, token: str, *, context: str) -> str:
        key_id, raw = self._split(token)
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise SecretError("unknown_key", f"no key is configured for key id '{key_id}'")
        try:
            plain = cipher.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], self._aad(context))
        except InvalidTag:
            # Wrong key, tampered data or wrong context: deliberately indistinguishable.
            raise SecretError("decrypt_failed", "the secret could not be decrypted") from None
        return plain.decode("utf-8")

    def key_id_of(self, token: str) -> str:
        return self._split(token)[0]

    def needs_rotation(self, token: str) -> bool:
        return self.key_id_of(token) != self._current_id

    def rotate(self, token: str, *, context: str) -> str:
        """Re-encrypt under the current key (a no-op copy if it already is)."""
        if not self.needs_rotation(token):
            self.decrypt(token, context=context)  # still verify it is genuine
            return token
        return self.encrypt(self.decrypt(token, context=context), context=context)


@lru_cache
def _cached_manager(fingerprint: tuple[object, ...]) -> SecretManager:
    return SecretManager.from_settings(get_settings())


def get_secret_manager() -> SecretManager:
    """The process-wide manager. Raises ``SecretError('not_configured')`` if no key is set; the
    failure is never cached, so adding the key takes effect without a restart."""
    settings = get_settings()
    material = "|".join(
        (
            settings.secret_encryption_key.get_secret_value()
            if settings.secret_encryption_key
            else "",
            settings.secret_encryption_key_id,
            settings.secret_encryption_previous_keys.get_secret_value()
            if settings.secret_encryption_previous_keys
            else "",
        )
    )
    return _cached_manager((hashlib.sha256(material.encode()).hexdigest(),))
