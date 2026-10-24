import base64

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.secrets import (
    ENVELOPE_VERSION,
    MAX_PLAINTEXT_BYTES,
    SecretError,
    SecretManager,
    get_secret_manager,
    parse_keys,
)

KEY_1 = base64.urlsafe_b64encode(bytes(range(32))).decode()
KEY_2 = base64.urlsafe_b64encode(bytes(range(50, 82))).decode()
CTX = "shopify:access_token:11111111-1111-1111-1111-111111111111"
SECRET = "shpat_0123456789abcdef-SUPERSECRET"


def manager(key: str = KEY_1, key_id: str = "1", previous: str = "") -> SecretManager:
    current, keys = parse_keys(key, key_id, previous)
    return SecretManager(current, keys)


def settings(**overrides) -> Settings:
    base = {"database_url": "sqlite://", "jwt_secret_key": "k" * 40}
    return Settings(_env_file=None, **{**base, **overrides})


# --- encrypt / decrypt -------------------------------------------------------------------------------


def test_round_trip():
    m = manager()
    token = m.encrypt(SECRET, context=CTX)
    assert m.decrypt(token, context=CTX) == SECRET


def test_unicode_and_long_secrets_round_trip():
    m = manager()
    for value in ("tökén-ünïcode-密钥", "x" * MAX_PLAINTEXT_BYTES):
        assert m.decrypt(m.encrypt(value, context=CTX), context=CTX) == value


def test_the_ciphertext_never_contains_the_plaintext_and_is_randomized():
    m = manager()
    a, b = m.encrypt(SECRET, context=CTX), m.encrypt(SECRET, context=CTX)
    assert a != b  # a fresh nonce every time
    assert SECRET not in a and "SUPERSECRET" not in a
    assert base64.urlsafe_b64encode(SECRET.encode()).decode().rstrip("=") not in a
    assert a.startswith(f"{ENVELOPE_VERSION}:1:")


def test_empty_and_oversized_secrets_are_refused():
    m = manager()
    with pytest.raises(SecretError) as exc:
        m.encrypt("", context=CTX)
    assert exc.value.code == "invalid_plaintext"
    with pytest.raises(SecretError):
        m.encrypt("x" * (MAX_PLAINTEXT_BYTES + 1), context=CTX)


def test_a_context_is_required():
    with pytest.raises(SecretError) as exc:
        manager().encrypt(SECRET, context="")
    assert exc.value.code == "invalid_context"


# --- tampering, wrong key, wrong context ------------------------------------------------------------------


def test_a_wrong_key_cannot_decrypt():
    token = manager(KEY_1).encrypt(SECRET, context=CTX)
    with pytest.raises(SecretError) as exc:
        manager(KEY_2).decrypt(token, context=CTX)  # same key id, different key material
    assert exc.value.code == "decrypt_failed"


def test_a_ciphertext_cannot_be_moved_to_another_row():
    m = manager()
    token = m.encrypt(SECRET, context="shopify:access_token:row-A")
    with pytest.raises(SecretError) as exc:
        m.decrypt(token, context="shopify:access_token:row-B")
    assert exc.value.code == "decrypt_failed"
    with pytest.raises(SecretError):  # nor reinterpreted as a different kind of secret
        m.decrypt(token, context="shopify:refresh_token:row-A")


def test_every_single_byte_flip_is_detected():
    m = manager()
    token = m.encrypt(SECRET, context=CTX)
    head, key_id, body = token.split(":")
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    for i in range(len(raw)):
        flipped = bytearray(raw)
        flipped[i] ^= 0x01
        forged = f"{head}:{key_id}:{base64.urlsafe_b64encode(bytes(flipped)).decode()}"
        with pytest.raises(SecretError):
            m.decrypt(forged, context=CTX)


def test_truncated_and_malformed_envelopes_are_rejected():
    m = manager()
    token = m.encrypt(SECRET, context=CTX)
    head, key_id, body = token.split(":")
    for bad in (
        "",
        "garbage",
        f"{head}:{key_id}",
        f"{head}:{key_id}:{body}:extra",
        f"{head}:{key_id}:!!!not-base64!!!",
        f"{head}:{key_id}:{body[:10]}",
        f"v9:{key_id}:{body}",
        f"{head}:bad key id:{body}",
        f"{head}::{body}",
    ):
        with pytest.raises(SecretError):
            m.decrypt(bad, context=CTX)


def test_an_unknown_key_id_is_reported_without_guessing():
    token = manager(KEY_1, key_id="7").encrypt(SECRET, context=CTX)
    with pytest.raises(SecretError) as exc:
        manager(KEY_1, key_id="1").decrypt(token, context=CTX)
    assert exc.value.code == "unknown_key"


def test_errors_never_contain_plaintext_ciphertext_or_key_material():
    m = manager()
    token = m.encrypt(SECRET, context=CTX)
    failures = []
    for fn in (
        lambda: manager(KEY_2).decrypt(token, context=CTX),
        lambda: m.decrypt(token, context="other"),
        lambda: m.decrypt(token[:-4] + "AAAA", context=CTX),
        lambda: parse_keys("not-base64!", "1"),
    ):
        with pytest.raises(SecretError) as exc:
            fn()
        failures.append(str(exc.value) + repr(exc.value))
    joined = " ".join(failures)
    assert (
        SECRET not in joined and token not in joined and KEY_1 not in joined and KEY_2 not in joined
    )


def test_the_manager_hides_key_material_in_its_repr():
    assert KEY_1 not in repr(manager()) and "keys=1" in repr(manager())


# --- configuration -----------------------------------------------------------------------------------------------


def test_a_missing_key_is_a_clear_not_configured_error():
    for key in (None, SecretStr(""), SecretStr("   ")):
        with pytest.raises(SecretError) as exc:
            SecretManager.from_settings(settings(secret_encryption_key=key))
        assert exc.value.code == "not_configured" and "SECRET_ENCRYPTION_KEY" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        base64.urlsafe_b64encode(b"x" * 16).decode(),  # too short
        base64.urlsafe_b64encode(b"x" * 33).decode(),  # too long
        "%%%%",
    ],
)
def test_an_invalid_key_is_rejected_at_startup_not_at_first_use(bad):
    with pytest.raises(SecretError) as exc:
        SecretManager.from_settings(settings(secret_encryption_key=SecretStr(bad)))
    assert exc.value.code == "invalid_key"
    assert bad not in str(exc.value) or len(bad) < 8


def test_standard_and_url_safe_base64_keys_decode_to_the_same_key():
    raw = bytes(
        [0xFB, 0xEF, 0xFF] * 10 + [0x01, 0x02]
    )  # encodes with both '+' and '/' / '-' and '_'
    standard = base64.b64encode(raw).decode()
    url_safe = base64.urlsafe_b64encode(raw).decode()
    assert standard != url_safe
    a, b = manager(standard), manager(url_safe)
    assert b.decrypt(a.encrypt(SECRET, context=CTX), context=CTX) == SECRET


# --- key versioning and rotation -------------------------------------------------------------------------------


def test_old_keys_still_decrypt_and_new_secrets_use_the_current_key():
    old = manager(KEY_1, "1")
    token_old = old.encrypt(SECRET, context=CTX)
    rotated = manager(KEY_2, "2", previous=f"1:{KEY_1}")
    assert rotated.current_key_id == "2"
    assert rotated.decrypt(token_old, context=CTX) == SECRET  # a previous key is decrypt-only
    assert rotated.encrypt(SECRET, context=CTX).startswith(f"{ENVELOPE_VERSION}:2:")


def test_rotation_reencrypts_under_the_current_key_and_is_idempotent():
    old_token = manager(KEY_1, "1").encrypt(SECRET, context=CTX)
    m = manager(KEY_2, "2", previous=f"1:{KEY_1}")
    assert m.needs_rotation(old_token) and m.key_id_of(old_token) == "1"
    new_token = m.rotate(old_token, context=CTX)
    assert not m.needs_rotation(new_token) and m.decrypt(new_token, context=CTX) == SECRET
    assert m.rotate(new_token, context=CTX) == new_token  # already current: unchanged
    # once the old key is retired, rotated secrets keep working and un-rotated ones fail closed
    retired = manager(KEY_2, "2")
    assert retired.decrypt(new_token, context=CTX) == SECRET
    with pytest.raises(SecretError):
        retired.decrypt(old_token, context=CTX)


def test_rotation_does_not_launder_a_forged_secret():
    m = manager(KEY_2, "2", previous=f"1:{KEY_1}")
    forged = manager(KEY_1, "1").encrypt(SECRET, context="other-context")
    with pytest.raises(SecretError):
        m.rotate(forged, context=CTX)


def test_key_configuration_errors():
    for previous in ("1", "1:", "x y:" + KEY_1, f"2:{KEY_1}"):  # the last one duplicates the id
        with pytest.raises(SecretError):
            parse_keys(KEY_2, "2", previous)
    with pytest.raises(SecretError):
        parse_keys(KEY_1, "bad:id")
    with pytest.raises(SecretError):
        parse_keys(KEY_1, "x" * 17)


def test_the_process_wide_manager_picks_up_configuration(monkeypatch):
    from app.core import secrets as module

    module._cached_manager.cache_clear()
    current = {"s": settings()}  # no key yet
    monkeypatch.setattr(module, "get_settings", lambda: current["s"])
    with pytest.raises(SecretError):
        get_secret_manager()
    current["s"] = settings(secret_encryption_key=SecretStr(KEY_1))
    first = get_secret_manager()
    assert first is get_secret_manager()  # cached once configured
    current["s"] = settings(secret_encryption_key=SecretStr(KEY_2), secret_encryption_key_id="2")
    assert get_secret_manager() is not first  # a changed key is picked up without a restart
    module._cached_manager.cache_clear()


def test_secret_settings_never_appear_in_settings_representations():
    s = settings(
        secret_encryption_key=SecretStr(KEY_1),
        secret_encryption_previous_keys=SecretStr(f"0:{KEY_2}"),
        shopify_client_secret=SecretStr("shpss_supersecret"),
    )
    text = repr(s) + str(s)
    assert KEY_1 not in text and KEY_2 not in text and "shpss_supersecret" not in text
