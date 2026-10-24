import base64
import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.secrets import SecretManager
from app.db.models import ShopifyConnection, ShopifyConnectionStatus
from app.services.integrations.shopify import lifecycle
from app.services.integrations.shopify.auth import token_context
from app.services.integrations.shopify.errors import ShopifyAuthError, ShopifyUnavailable
from tests.helpers import create_org, signup_and_login
from tests.shopify.conftest import OTHER_KEY, seed_connection


@pytest.fixture()
def org_id(client):
    headers = signup_and_login(client, "tokens@example.com")
    return uuid.UUID(create_org(client, headers, "Tokens Co")["id"])


@pytest.fixture()
def conn(session_factory, org_id, runtime, fake):
    return seed_connection(session_factory, org_id, runtime, fake)


def row(session_factory, cid) -> ShopifyConnection:
    with session_factory() as db:
        return db.get(ShopifyConnection, cid)


def plaintext(runtime, r, kind="access_token"):
    column = r.access_token_encrypted if kind == "access_token" else r.refresh_token_encrypted
    return runtime.secrets.decrypt(column, context=token_context(kind, r.id))


# --- when a refresh happens --------------------------------------------------------------------------


def test_a_fresh_token_is_used_without_contacting_shopify(runtime, session_factory, conn, fake):
    tokens = runtime.tokens_for(session_factory, conn)
    token = tokens.access_token()
    assert token in fake.valid_access
    assert fake.token_requests == []
    assert tokens.access_token() == token  # stable while valid


def test_a_token_close_to_expiry_is_refreshed_proactively_and_both_tokens_rotate(
    runtime, session_factory, conn, fake, clock
):
    before = row(session_factory, conn)
    old_access, old_refresh = (
        plaintext(runtime, before),
        plaintext(runtime, before, "refresh_token"),
    )
    clock.advance(3600 - 200)  # inside the 5-minute safety margin
    token = runtime.tokens_for(session_factory, conn).access_token()

    assert token != old_access and token in fake.valid_access
    assert len(fake.token_requests) == 1
    assert fake.token_requests[0]["grant_type"] == "refresh_token"
    assert fake.token_requests[0]["refresh_token"] == old_refresh
    after = row(session_factory, conn)
    assert plaintext(runtime, after) == token
    assert (
        plaintext(runtime, after, "refresh_token") != old_refresh
    )  # the refresh token rotated too
    # Shopify keeps the presented refresh token usable for a grace period (until the newer one is
    # used, a new token is acquired, 30 days pass, or its own expiry), so it is NOT yet retired.
    assert old_refresh in fake.valid_refresh
    expires = after.access_token_expires_at
    assert expires.replace(tzinfo=None) > clock.now().replace(tzinfo=None) + timedelta(minutes=50)
    assert after.status == ShopifyConnectionStatus.CONNECTED and after.error_code is None


def test_after_refreshing_the_new_token_is_reused(runtime, session_factory, conn, fake, clock):
    clock.advance(3500)
    tokens = runtime.tokens_for(session_factory, conn)
    first = tokens.access_token()
    assert tokens.access_token() == first
    assert len(fake.token_requests) == 1  # not once per call


def test_tokens_that_never_expire_are_never_refreshed(
    session_factory, org_id, runtime, fake, clock
):
    cid = seed_connection(session_factory, org_id, runtime, fake, expiring=False)
    clock.advance(10 * 24 * 3600)
    assert runtime.tokens_for(session_factory, cid).access_token() in fake.valid_access
    assert fake.token_requests == []


def test_ciphertext_in_the_database_is_never_the_token(runtime, session_factory, conn):
    r = row(session_factory, conn)
    token = plaintext(runtime, r)
    assert token not in r.access_token_encrypted
    assert (
        base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
        not in r.access_token_encrypted
    )


# --- concurrency: the refresh token rotates, so two refreshes must not both go to Shopify ----------------


def test_two_workers_that_both_hit_a_401_rotate_only_once(runtime, session_factory, conn, fake):
    worker_a = runtime.tokens_for(session_factory, conn)
    worker_b = runtime.tokens_for(session_factory, conn)
    stale = worker_a.access_token()
    assert (
        worker_b.access_token() == stale
    )  # both are holding the same (about to be rejected) token

    fresh = worker_a.force_refresh()  # A refreshes after its 401...
    assert fresh != stale
    assert worker_b.force_refresh() == fresh  # ...B finds A's new token instead of rotating again
    assert len(fake.token_requests) == 1


def test_a_second_forced_refresh_by_the_same_worker_does_rotate_again(
    runtime, session_factory, conn, fake
):
    tokens = runtime.tokens_for(session_factory, conn)
    first = tokens.access_token()
    second = tokens.force_refresh()
    assert second != first and len(fake.token_requests) == 1
    third = tokens.force_refresh()  # the token we just got was rejected as well
    assert third != second and len(fake.token_requests) == 2


def test_an_expired_token_seen_by_two_workers_is_refreshed_once(
    runtime, session_factory, conn, fake, clock
):
    clock.advance(3550)
    a, b = runtime.tokens_for(session_factory, conn), runtime.tokens_for(session_factory, conn)
    assert a.access_token() == b.access_token()
    assert len(fake.token_requests) == 1


# --- failures ---------------------------------------------------------------------------------------------


def test_a_rejected_refresh_token_means_the_merchant_must_reauthorize(
    runtime, session_factory, conn, fake, clock
):
    fake.valid_refresh.clear()  # revoked at Shopify
    clock.advance(3550)
    with pytest.raises(ShopifyAuthError) as exc:
        runtime.tokens_for(session_factory, conn).access_token()
    assert exc.value.code == "reauthorization_required"
    after = row(session_factory, conn)
    assert after.status == ShopifyConnectionStatus.ERROR
    assert (
        after.error_code == "reauthorization_required"
        and "reconnect" in after.error_message.lower()
    )


def test_a_transient_refresh_failure_changes_nothing(runtime, session_factory, conn, fake, clock):
    before = row(session_factory, conn)
    fake.token_endpoint_status = 503
    clock.advance(3550)
    with pytest.raises(ShopifyUnavailable):
        runtime.tokens_for(session_factory, conn).access_token()
    after = row(session_factory, conn)
    assert after.status == ShopifyConnectionStatus.CONNECTED and after.error_code is None
    assert after.access_token_encrypted == before.access_token_encrypted
    assert after.refresh_token_encrypted == before.refresh_token_encrypted
    fake.token_endpoint_status = None
    assert runtime.tokens_for(session_factory, conn).access_token() in fake.valid_access  # recovers


def test_no_refresh_token_and_an_expired_access_token_needs_reauthorization(
    runtime, session_factory, conn, fake, clock
):
    with session_factory() as db:
        db.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == conn)
            .values(refresh_token_encrypted=None)
        )
        db.commit()
    clock.advance(3550)
    with pytest.raises(ShopifyAuthError) as exc:
        runtime.tokens_for(session_factory, conn).access_token()
    assert exc.value.code == "reauthorization_required"
    assert row(session_factory, conn).status == ShopifyConnectionStatus.ERROR


def test_a_disconnected_or_missing_connection_has_no_token(runtime, session_factory, conn):
    with session_factory() as db:
        c = db.get(ShopifyConnection, conn)
        c.status = ShopifyConnectionStatus.DISCONNECTED
        c.access_token_encrypted = None
        db.commit()
    with pytest.raises(ShopifyAuthError):
        runtime.tokens_for(session_factory, conn).access_token()
    with pytest.raises(ShopifyAuthError):
        runtime.tokens_for(session_factory, uuid.uuid4()).access_token()


def test_credentials_that_cannot_be_decrypted_fail_closed_without_leaking(
    runtime, session_factory, conn, shopify_settings, caplog
):
    caplog.set_level(logging.DEBUG)
    wrong_key = SecretManager.from_settings(
        shopify_settings.model_copy(
            update={"secret_encryption_key": __import__("pydantic").SecretStr(OTHER_KEY)}
        )
    )
    broken = type(runtime)(
        settings=runtime.settings,
        secrets=wrong_key,
        transport=runtime.transport,
        sleep=runtime.sleep,
    )
    with pytest.raises(ShopifyAuthError) as exc:
        broken.tokens_for(session_factory, conn).access_token()
    stored = row(session_factory, conn).access_token_encrypted
    assert stored not in str(exc.value) and stored not in caplog.text
    assert "shpat_" not in caplog.text


def test_a_ciphertext_copied_to_another_connection_is_useless(
    runtime, session_factory, conn, org_id, client, fake
):
    other_org = uuid.UUID(
        create_org(client, signup_and_login(client, "other-tokens@example.com"), "Other Co")["id"]
    )
    other = seed_connection(
        session_factory, other_org, runtime, fake, shop="other-shop.myshopify.com"
    )
    with session_factory() as db:  # an attacker with DB write access swaps the stored token
        stolen = db.get(ShopifyConnection, conn).access_token_encrypted
        db.get(ShopifyConnection, other).access_token_encrypted = stolen
        db.commit()
    with pytest.raises(ShopifyAuthError):
        runtime.tokens_for(session_factory, other).access_token()


def test_tokens_are_never_logged(runtime, session_factory, conn, fake, clock, caplog):
    caplog.set_level(logging.DEBUG)
    clock.advance(3550)
    token = runtime.tokens_for(session_factory, conn).access_token()
    secret = runtime.settings.shopify_client_secret.get_secret_value()
    assert token not in caplog.text and secret not in caplog.text and "shprt_" not in caplog.text


# --- keeping idle connections alive -------------------------------------------------------------------------------


def test_refresh_tokens_close_to_expiry_are_rotated_by_the_worker(
    runtime, session_factory, conn, fake, org_id, client
):
    with session_factory() as db:
        db.get(ShopifyConnection, conn).refresh_token_expires_at = runtime.now() + timedelta(days=5)
        db.commit()
    old_refresh = plaintext(runtime, row(session_factory, conn), "refresh_token")
    assert lifecycle.rotate_aging_tokens(runtime, session_factory) == 1
    assert plaintext(runtime, row(session_factory, conn), "refresh_token") != old_refresh
    assert old_refresh in fake.valid_refresh  # still usable during Shopify's grace period


def test_healthy_and_disconnected_connections_are_not_rotated(runtime, session_factory, conn, fake):
    assert lifecycle.rotate_aging_tokens(runtime, session_factory) == 0  # 90 days left
    with session_factory() as db:
        c = db.get(ShopifyConnection, conn)
        c.refresh_token_expires_at = runtime.now() + timedelta(days=1)
        c.status = ShopifyConnectionStatus.DISCONNECTED
        db.commit()
    assert lifecycle.rotate_aging_tokens(runtime, session_factory) == 0
    assert fake.token_requests == []


def test_a_failed_rotation_is_reported_not_raised(runtime, session_factory, conn, fake):
    with session_factory() as db:
        db.get(ShopifyConnection, conn).refresh_token_expires_at = runtime.now() + timedelta(days=1)
        db.commit()
    fake.token_endpoint_status = 503
    assert lifecycle.rotate_aging_tokens(runtime, session_factory) == 0  # logged, loop continues
    with session_factory() as db:
        assert db.scalar(select(ShopifyConnection.status)) == ShopifyConnectionStatus.CONNECTED


# --- refresh-token grace period (Shopify docs: the presented token stays usable until the earliest of
#     "your app uses the newer refresh token", "your app acquires a new token", "30 days after your app
#     first used that token to refresh", "the original 90-day expiry") ---------------------------------


def test_using_the_newer_refresh_token_retires_the_one_it_replaced(
    runtime, session_factory, conn, fake, clock
):
    first = plaintext(runtime, row(session_factory, conn), "refresh_token")
    clock.advance(3550)
    runtime.tokens_for(session_factory, conn).access_token()  # refresh #1 uses `first`
    second = plaintext(runtime, row(session_factory, conn), "refresh_token")
    assert first in fake.valid_refresh and second in fake.valid_refresh
    clock.advance(3550)
    runtime.tokens_for(session_factory, conn).access_token()  # refresh #2 uses `second`
    assert first not in fake.valid_refresh and second in fake.valid_refresh


def test_a_worker_killed_after_shopify_rotated_but_before_saving_loses_nothing(
    runtime, session_factory, conn, fake, clock, monkeypatch
):
    """The dangerous moment of a rotating token: Shopify has issued new tokens, the process dies before
    they are stored. The stored (old) refresh token is still honoured, so the next refresh works."""
    from app.services.integrations.shopify import auth

    stored_before = row(session_factory, conn).refresh_token_encrypted
    real_store = auth.store_grant

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("worker killed before the new tokens were committed")

    clock.advance(3550)
    monkeypatch.setattr(auth, "store_grant", crash)
    with pytest.raises(KeyboardInterrupt):
        runtime.tokens_for(session_factory, conn).access_token()
    assert len(fake.token_requests) == 1  # Shopify did rotate...
    assert (
        row(session_factory, conn).refresh_token_encrypted == stored_before
    )  # ...we never saved it
    assert row(session_factory, conn).status == ShopifyConnectionStatus.CONNECTED

    monkeypatch.setattr(auth, "store_grant", real_store)  # the restarted worker
    token = runtime.tokens_for(session_factory, conn).access_token()
    assert token in fake.valid_access and len(fake.token_requests) == 2
    assert fake.token_requests[1]["refresh_token"] == fake.token_requests[0]["refresh_token"]
    after = row(session_factory, conn)
    assert after.status == ShopifyConnectionStatus.CONNECTED and after.error_code is None


def test_after_the_grace_period_a_lost_rotation_means_the_merchant_must_reconnect(
    runtime, session_factory, conn, fake, clock, monkeypatch
):
    """The limit of the safety above, stated honestly: if the new tokens are lost AND more than 30
    days pass, the stored refresh token is dead. The connection says so instead of looping."""
    from app.services.integrations.shopify import auth

    clock.advance(3550)
    monkeypatch.setattr(
        auth, "store_grant", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("killed"))
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.tokens_for(session_factory, conn).access_token()
    monkeypatch.undo()
    fake.expire_refresh_grace()  # thirty days later

    with pytest.raises(ShopifyAuthError) as exc:
        runtime.tokens_for(session_factory, conn).access_token()
    assert exc.value.code == "reauthorization_required"
    after = row(session_factory, conn)
    assert after.status == ShopifyConnectionStatus.ERROR
    assert len(fake.token_requests) == 2  # one try, not a retry storm


def test_reconnecting_retires_every_earlier_refresh_token(runtime, session_factory, conn, fake):
    before = set(fake.valid_refresh)
    fake.valid_codes.add("again")
    client = runtime.client_for(fake.shop)
    grant = client.exchange_code(
        client_id="test-client-id", client_secret="test-client-secret", code="again"
    )
    client.close()
    assert grant.refresh_token not in before
    assert not (before & fake.valid_refresh)
