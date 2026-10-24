"""Encryption-key rotation: the tool that proves an old key can be retired, and the guarantees it
relies on (a secret stays readable across rotation; it is never readable under the wrong context)."""

import base64
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.secrets import SecretManager
from app.db.models import ShopifyConnection
from app.services.integrations.shopify.auth import token_context
from tests.shopify.conftest import OTHER_KEY, TEST_KEY, seed_connection
from tools import reencrypt_secrets

NEW_KEY = base64.urlsafe_b64encode(bytes(range(200, 232))).decode()


def manager(current: str, current_id: str, previous: str = "") -> SecretManager:
    from app.core.config import Settings

    return SecretManager.from_settings(
        Settings(
            _env_file=None,
            database_url="sqlite://",
            jwt_secret_key="k" * 40,
            secret_encryption_key=SecretStr(current),
            secret_encryption_key_id=current_id,
            secret_encryption_previous_keys=SecretStr(previous) if previous else None,
        )
    )


@pytest.fixture()
def stores(tenants, session_factory, runtime, fake):
    """Two connections whose tokens were stored under the key the runtime holds (id 'k1')."""
    a, b = tenants
    ids = [
        seed_connection(session_factory, uuid.UUID(a.org["id"]), runtime, fake),
        seed_connection(
            session_factory, uuid.UUID(b.org["id"]), runtime, fake, shop="two.myshopify.com"
        ),
    ]
    return ids


def stored(session_factory, connection_id):
    with session_factory() as db:
        row = db.get(ShopifyConnection, connection_id)
        return row.access_token_encrypted, row.refresh_token_encrypted


def rotated_manager(runtime):
    """The deployment after the key was rotated: new current key, the old one kept as previous."""
    old_id = runtime.secrets.current_key_id
    return manager(NEW_KEY, "k2", f"{old_id}:{TEST_KEY}")


def test_the_report_counts_secrets_per_key_and_changes_nothing(stores, session_factory, runtime):
    new = rotated_manager(runtime)
    before = [stored(session_factory, i) for i in stores]
    report = reencrypt_secrets.reencrypt(session_factory, new, apply=False)
    assert dict(report.by_key) == {runtime.secrets.current_key_id: 4}  # 2 stores x 2 tokens
    assert (report.needs_rotation, report.rotated, report.unreadable) == (4, 0, 0)
    assert not report.clean
    assert [stored(session_factory, i) for i in stores] == before


def test_apply_moves_every_secret_to_the_new_key_and_they_stay_readable(
    stores, session_factory, runtime
):
    new = rotated_manager(runtime)
    original = {
        i: (
            runtime.secrets.decrypt(
                stored(session_factory, i)[0], context=token_context("access_token", i)
            ),
            runtime.secrets.decrypt(
                stored(session_factory, i)[1], context=token_context("refresh_token", i)
            ),
        )
        for i in stores
    }
    report = reencrypt_secrets.reencrypt(session_factory, new, apply=True)
    assert (report.rotated, report.remaining, report.unreadable) == (4, 0, 0)
    for i in stores:
        access, refresh = stored(session_factory, i)
        assert new.key_id_of(access) == new.key_id_of(refresh) == "k2"
        assert new.decrypt(access, context=token_context("access_token", i)) == original[i][0]
        assert new.decrypt(refresh, context=token_context("refresh_token", i)) == original[i][1]


def test_after_apply_the_old_key_is_provably_unneeded_and_can_be_retired(
    stores, session_factory, runtime
):
    new = rotated_manager(runtime)
    reencrypt_secrets.reencrypt(session_factory, new, apply=True)
    only_new = manager(NEW_KEY, "k2")  # the previous key has been removed from the configuration
    report = reencrypt_secrets.reencrypt(session_factory, only_new, apply=False)
    assert report.clean and dict(report.by_key) == {"k2": 4}


def test_retiring_the_old_key_too_early_is_reported_not_silently_ignored(stores, session_factory):
    only_new = manager(NEW_KEY, "k2")
    report = reencrypt_secrets.reencrypt(session_factory, only_new, apply=True)
    assert report.unreadable == 4 and not report.clean
    assert report.rotated == 0


def test_a_second_run_has_nothing_to_do(stores, session_factory, runtime):
    new = rotated_manager(runtime)
    reencrypt_secrets.reencrypt(session_factory, new, apply=True)
    again = reencrypt_secrets.reencrypt(session_factory, new, apply=True)
    assert again.clean and again.rotated == 0


def test_a_ciphertext_moved_to_another_connection_is_reported_unreadable(
    stores, session_factory, runtime
):
    """Context binding: copying one store's token into another row does not produce a usable token."""
    first, second = stores
    with session_factory() as db:
        a = db.get(ShopifyConnection, first)
        b = db.get(ShopifyConnection, second)
        b.access_token_encrypted = a.access_token_encrypted
        db.commit()
    report = reencrypt_secrets.reencrypt(session_factory, runtime.secrets, apply=False)
    assert report.unreadable == 1 and not report.clean


def test_a_disconnected_store_with_wiped_tokens_is_not_counted(stores, session_factory, runtime):
    with session_factory() as db:
        row = db.get(ShopifyConnection, stores[0])
        row.access_token_encrypted = row.refresh_token_encrypted = None
        db.commit()
    report = reencrypt_secrets.reencrypt(session_factory, runtime.secrets, apply=False)
    assert report.clean and sum(report.by_key.values()) == 2


def test_the_output_names_keys_and_counts_but_never_a_token(stores, session_factory, runtime):
    new = rotated_manager(runtime)
    secrets_in_db = [t for i in stores for t in stored(session_factory, i)]
    plain = [
        runtime.secrets.decrypt(t, context=token_context(kind, i))
        for i in stores
        for kind, t in zip(
            ("access_token", "refresh_token"), stored(session_factory, i), strict=True
        )
    ]
    report = reencrypt_secrets.reencrypt(session_factory, new, apply=False)
    out = reencrypt_secrets.render(report, new, apply=False)
    assert "OLD" in out and "--apply" in out
    for leaked in (*secrets_in_db, *plain, TEST_KEY, NEW_KEY, OTHER_KEY):
        assert leaked not in out
    report = reencrypt_secrets.reencrypt(session_factory, new, apply=True)
    done = reencrypt_secrets.render(report, new, apply=True)
    assert "safe to remove retired keys" in done


def test_the_command_exits_nonzero_until_everything_is_on_the_current_key(
    stores, session_factory, runtime, monkeypatch, capsys
):
    monkeypatch.setattr(reencrypt_secrets, "SessionLocal", session_factory)
    new = rotated_manager(runtime)
    monkeypatch.setattr(reencrypt_secrets.SecretManager, "from_settings", lambda s: new)
    assert reencrypt_secrets.main([]) == 1
    assert reencrypt_secrets.main(["--apply"]) == 0
    assert reencrypt_secrets.main([]) == 0
    assert "OK:" in capsys.readouterr().out


def test_the_command_refuses_to_run_without_an_encryption_key(monkeypatch, capsys):
    from app.core.secrets import SecretError

    def missing(_settings):
        raise SecretError("not_configured", "SECRET_ENCRYPTION_KEY is not set")

    monkeypatch.setattr(reencrypt_secrets.SecretManager, "from_settings", missing)
    assert reencrypt_secrets.main([]) == 2
    assert "SECRET_ENCRYPTION_KEY" in capsys.readouterr().err


def test_select_is_unchanged_for_connections_the_tool_did_not_need_to_touch(
    stores, session_factory, runtime
):
    before = [stored(session_factory, i) for i in stores]
    reencrypt_secrets.reencrypt(session_factory, runtime.secrets, apply=True)  # already current
    assert [stored(session_factory, i) for i in stores] == before
    with session_factory() as db:
        assert len(list(db.scalars(select(ShopifyConnection)))) == 2
