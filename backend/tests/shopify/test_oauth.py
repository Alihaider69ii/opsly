import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from sqlalchemy import select, update

from app.db.models import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifyOAuthState,
    ShopifySyncState,
    SyncStatus,
)
from app.services.integrations.shopify.validation import sign_oauth_query
from app.workers.queue import ShopifySyncJob
from tests.helpers import add_member, signup_and_login
from tests.shopify.conftest import (
    TEST_KEY,
    callback_query,
    connect_through_api,
    seed_connection,
    state_from,
)

CONNECT = "/api/integrations/shopify/connect"
CALLBACK = "/api/integrations/shopify/callback"
STATUS = "/api/integrations/shopify"


@pytest.fixture()
def api(shopify_client, tenants, fake, runtime, session_factory):
    a, b = tenants
    return type(
        "Api",
        (),
        {"c": shopify_client, "a": a, "b": b, "fake": fake, "rt": runtime, "sf": session_factory},
    )


def start(api, who, shop=None):
    r = api.c.post(CONNECT, json={"shop": shop or api.fake.shop}, headers=who.headers)
    assert r.status_code == 200, r.text
    return r.json()["authorization_url"]


def complete(api, who, query):
    return api.c.get(f"{CALLBACK}?{urlencode(query)}", headers=who.headers)


def code_of(response) -> str:
    return response.json()["detail"]["code"]


def connections(db):
    db.expire_all()
    return list(db.scalars(select(ShopifyConnection)))


# --- starting the flow --------------------------------------------------------------------------------


def test_connect_returns_the_shopify_authorization_url(api, shopify_settings):
    url = start(api, api.a)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        == "https://test-shop.myshopify.com/admin/oauth/authorize"
    )
    assert query["client_id"] == ["test-client-id"]
    assert query["scope"] == ["read_products,read_customers,read_orders"]
    assert query["redirect_uri"] == [shopify_settings.shopify_redirect_uri]
    assert len(query["state"][0]) >= 43  # 256 bits of randomness, url-safe encoded
    assert "grant_options[]" not in query  # an offline (not per-user) token is requested
    assert "client_secret" not in url and "test-client-secret" not in url


def test_the_state_is_unpredictable_and_stored_only_as_a_hash(api, db):
    states = {state_from(start(api, api.a)) for _ in range(5)}
    assert len(states) == 5
    state = state_from(start(api, api.a))
    rows = list(db.scalars(select(ShopifyOAuthState)))
    (row,) = rows  # earlier unfinished attempts by the same user were discarded
    assert row.state_hash == hashlib.sha256(state.encode()).hexdigest()
    assert state not in row.state_hash and row.consumed_at is None
    assert (row.organization_id, row.shop_domain) == (uuid.UUID(api.a.org["id"]), api.fake.shop)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    assert timedelta(minutes=9) < expires - datetime.now(UTC) <= timedelta(minutes=10, seconds=5)


def test_a_new_attempt_invalidates_the_users_previous_unfinished_one(api):
    first = state_from(start(api, api.a))
    start(api, api.a)
    response = complete(api, api.a, callback_query(api.fake, first))
    assert response.status_code == 400 and code_of(response) == "invalid_state"


@pytest.mark.parametrize(
    "shop",
    [
        "evil.com",
        "localhost",
        "http://127.0.0.1:8000",
        "169.254.169.254",
        "my-store.myshopify.com.evil.com",
        "https://evil.com@my-store.myshopify.com",
        "my-store.myshopify.com/admin",
        "a.b.myshopify.com",
        "not a domain",
        "",
    ],
)
def test_arbitrary_and_malicious_shop_domains_are_rejected_before_anything_happens(api, db, shop):
    r = api.c.post(CONNECT, json={"shop": shop}, headers=api.a.headers)
    assert r.status_code == 422
    assert db.scalar(select(ShopifyOAuthState.id)) is None  # no state was even created
    assert api.fake.requests == [] and api.fake.token_requests == []


def test_unknown_fields_in_the_connect_body_are_refused(api):
    r = api.c.post(
        CONNECT,
        json={"shop": api.fake.shop, "organization_id": api.b.org["id"], "scopes": "write_orders"},
        headers=api.a.headers,
    )
    assert r.status_code == 422


def test_connect_needs_login_and_a_manager_role(api, shopify_client):
    assert shopify_client.post(CONNECT, json={"shop": api.fake.shop}).status_code == 401
    member = signup_and_login(shopify_client, "member@example.com")
    assert (
        add_member(
            shopify_client, api.a.headers, api.a.org["id"], "member@example.com", "MEMBER"
        ).status_code
        == 201
    )
    r = shopify_client.post(
        CONNECT,
        json={"shop": api.fake.shop},
        headers={**member, "X-Organization-ID": api.a.org["id"]},
    )
    assert r.status_code == 403


def test_connect_is_unavailable_without_app_credentials(api, shopify_settings):
    shopify_settings.shopify_client_id = ""
    r = api.c.post(CONNECT, json={"shop": api.fake.shop}, headers=api.a.headers)
    assert r.status_code == 503 and code_of(r) == "not_configured"


def test_connect_is_unavailable_without_secure_storage(api, shopify_client):
    from app.api.integrations.shopify import get_shopify_runtime
    from app.main import app

    app.dependency_overrides[get_shopify_runtime] = lambda: None
    r = shopify_client.post(CONNECT, json={"shop": api.fake.shop}, headers=api.a.headers)
    assert r.status_code == 503 and code_of(r) == "secrets_not_configured"
    s = shopify_client.get(STATUS, headers=api.a.headers).json()
    assert s["secure_storage"] is False


def test_the_dev_override_redirects_to_the_local_fake_shopify(api, shopify_settings):
    shopify_settings.shopify_dev_base_url = "http://localhost:9300"
    url = start(api, api.a)
    assert url.startswith("http://localhost:9300/admin/oauth/authorize?")


def test_connect_attempts_are_rate_limited(api):
    codes = [
        api.c.post(CONNECT, json={"shop": api.fake.shop}, headers=api.a.headers).status_code
        for _ in range(25)
    ]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]


# --- the happy path -----------------------------------------------------------------------------------------------


def test_a_valid_flow_stores_an_encrypted_connection_and_queues_the_initial_sync(
    api, db, queue, secrets_manager
):
    r = connect_through_api(api.c, api.a.headers, api.fake)
    assert r.status_code == 200 and r.json() == {
        "status": "connected",
        "shop_domain": api.fake.shop,
    }

    (row,) = connections(db)
    assert row.organization_id == uuid.UUID(api.a.org["id"]) and row.shop_domain == api.fake.shop
    assert row.status == ShopifyConnectionStatus.SYNCING
    assert (row.shop_name, row.currency, row.timezone) == ("Test Shop", "USD", "America/New_York")
    assert row.api_version == "2026-10"
    assert set(row.scopes.split(",")) == {"read_products", "read_customers", "read_orders"}
    # tokens are ciphertext at rest, and decrypt with the connection-bound context
    assert row.access_token_encrypted.startswith(
        "bf1:1:"
    ) and row.refresh_token_encrypted.startswith("bf1:1:")
    for column in (row.access_token_encrypted, row.refresh_token_encrypted):
        assert "shpat_" not in column and "shprt_" not in column
    access = secrets_manager.decrypt(
        row.access_token_encrypted, context=f"shopify:access_token:{row.id}"
    )
    assert access in api.fake.valid_access
    assert row.access_token_expires_at is not None and row.refresh_token_expires_at is not None

    assert [type(j) for j in queue.jobs] == [ShopifySyncJob] and queue.jobs[
        0
    ].connection_id == row.id
    states = {s.resource.value: s.status for s in db.scalars(select(ShopifySyncState))}
    assert states == {
        "PRODUCTS": SyncStatus.PENDING,
        "CUSTOMERS": SyncStatus.PENDING,
        "ORDERS": SyncStatus.PENDING,
    }
    assert api.fake.requests and api.fake.operations() == [
        "ShopInfo"
    ]  # only the shop lookup, no import inline


def test_nothing_secret_is_ever_returned_by_the_api(api):
    connect_through_api(api.c, api.a.headers, api.fake)
    blob = api.c.get(STATUS, headers=api.a.headers).text
    for secret in (
        "shpat_",
        "shprt_",
        "test-client-secret",
        TEST_KEY,
        "access_token",
        "refresh_token",
        "bf1:",
    ):
        assert secret not in blob, secret
    body = api.c.get(STATUS, headers=api.a.headers).json()
    assert set(body["connection"]) == {
        "shop_domain",
        "shop_name",
        "currency",
        "timezone",
        "api_version",
        "status",
        "installed_at",
        "last_sync_at",
        "webhook_status",
        "error_code",
        "error_message",
    }


def test_the_state_is_consumed_and_cannot_be_replayed(api, db):
    url = start(api, api.a)
    state = state_from(url)
    query = callback_query(api.fake, state)
    assert complete(api, api.a, query).status_code == 200
    db.expire_all()
    assert db.scalar(select(ShopifyOAuthState.consumed_at)) is not None
    api.fake.valid_codes.add("good-code")  # even if Shopify would accept the code again
    replay = complete(api, api.a, callback_query(api.fake, state))
    assert replay.status_code == 400 and code_of(replay) == "invalid_state"
    assert len(api.fake.token_requests) == 1  # the replay never reached the token endpoint


# --- forged, stale and misdirected callbacks ----------------------------------------------------------------


def test_an_unknown_state_is_rejected(api, db):
    r = complete(api, api.a, callback_query(api.fake, "never-issued-" + "x" * 30))
    assert r.status_code == 400 and code_of(r) == "invalid_state"
    assert connections(db) == [] and api.fake.token_requests == []


def test_an_expired_state_is_rejected(api, db):
    state = state_from(start(api, api.a))
    db.execute(
        update(ShopifyOAuthState).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    db.commit()
    r = complete(api, api.a, callback_query(api.fake, state))
    assert r.status_code == 400 and code_of(r) == "state_expired"
    assert connections(db) == [] and api.fake.token_requests == []


def test_the_state_ttl_is_configurable(api, shopify_settings, db):
    shopify_settings.shopify_oauth_state_ttl_seconds = 120
    start(api, api.a)
    db.expire_all()
    row = db.scalar(select(ShopifyOAuthState))
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    assert expires - datetime.now(UTC) < timedelta(minutes=2, seconds=5)


def test_another_organizations_user_cannot_complete_my_authorization(api, db):
    state = state_from(start(api, api.a))
    attempt = complete(api, api.b, callback_query(api.fake, state))
    assert attempt.status_code == 400 and code_of(attempt) == "invalid_state"
    assert connections(db) == [] and api.fake.token_requests == []
    # the rightful owner can still finish: the failed attempt did not burn the state
    assert complete(api, api.a, callback_query(api.fake, state)).status_code == 200


def test_another_user_in_the_same_organization_cannot_complete_it_either(api, shopify_client, db):
    admin = signup_and_login(shopify_client, "admin@example.com")
    add_member(shopify_client, api.a.headers, api.a.org["id"], "admin@example.com", "ADMIN")
    admin_headers = {**admin, "X-Organization-ID": api.a.org["id"]}
    state = state_from(start(api, api.a))  # started by the owner
    r = shopify_client.get(
        f"{CALLBACK}?{urlencode(callback_query(api.fake, state))}", headers=admin_headers
    )
    assert r.status_code == 400 and code_of(r) == "invalid_state"
    assert connections(db) == []


def test_a_callback_for_a_different_store_than_the_one_started_is_rejected(api, db):
    state = state_from(start(api, api.a, shop="store-one.myshopify.com"))
    query = callback_query(api.fake, state, shop="store-two.myshopify.com")  # validly signed
    r = complete(api, api.a, query)
    assert r.status_code == 400 and code_of(r) == "shop_mismatch"
    assert connections(db) == [] and api.fake.token_requests == []


@pytest.mark.parametrize(
    "evil", ["evil.com", "169.254.169.254", "localhost", "x.myshopify.com.evil.com"]
)
def test_a_callback_naming_a_non_shopify_host_never_triggers_an_outbound_request(api, evil):
    state = state_from(start(api, api.a))
    r = complete(
        api, api.a, callback_query(api.fake, state, shop=evil)
    )  # even with a valid signature
    assert r.status_code == 400
    assert api.fake.token_requests == [] and api.fake.requests == []


def test_tampering_with_any_parameter_breaks_the_signature(api, db):
    state = state_from(start(api, api.a))
    good = callback_query(api.fake, state)
    for key in ("code", "shop", "state", "timestamp", "host"):
        bad = {**good, key: good[key] + "x"}
        r = complete(api, api.a, bad)
        assert r.status_code == 400 and code_of(r) == "invalid_signature", key
    assert api.fake.token_requests == []
    assert connections(db) == []


def test_a_callback_without_a_signature_or_with_a_wrong_secret_is_rejected(api):
    state = state_from(start(api, api.a))
    good = callback_query(api.fake, state)
    unsigned = {k: v for k, v in good.items() if k != "hmac"}
    assert code_of(complete(api, api.a, unsigned)) == "invalid_signature"
    forged = {**unsigned, "hmac": sign_oauth_query(unsigned, "attackers-guess")}
    assert code_of(complete(api, api.a, forged)) == "invalid_signature"
    assert api.fake.token_requests == []


def test_old_and_future_timestamps_are_rejected(api):
    state = state_from(start(api, api.a))
    for ts in (time.time() - 11 * 60, time.time() + 10 * 60):
        query = callback_query(api.fake, state, timestamp=str(int(ts)))
        r = complete(api, api.a, query)
        assert r.status_code == 400 and code_of(r) == "state_expired"
    assert api.fake.token_requests == []


def test_duplicate_query_parameters_are_refused(api):
    state = state_from(start(api, api.a))
    q = callback_query(api.fake, state)
    raw = urlencode(q) + "&shop=evil.myshopify.com"
    r = api.c.get(f"{CALLBACK}?{raw}", headers=api.a.headers)
    assert r.status_code == 400 and api.fake.token_requests == []


def test_a_callback_needs_login(api, shopify_client):
    state = state_from(start(api, api.a))
    r = shopify_client.get(f"{CALLBACK}?{urlencode(callback_query(api.fake, state))}")
    assert r.status_code == 401


def test_an_organization_id_in_the_callback_is_ignored(api, db):
    state = state_from(start(api, api.a))
    query = callback_query(
        api.fake, state, organization_id=api.b.org["id"]
    )  # signed, so Shopify-looking
    assert complete(api, api.a, query).status_code == 200
    rows = connections(db)
    assert [r.organization_id for r in rows] == [uuid.UUID(api.a.org["id"])]  # the session decides
    assert api.c.get(STATUS, headers=api.b.headers).json()["connection"] is None


# --- grants, ownership and failures ----------------------------------------------------------------------------


def test_missing_the_required_scope_fails_and_stores_nothing(api, db):
    api.fake.granted_scopes = "read_customers,read_orders"
    r = connect_through_api(api.c, api.a.headers, api.fake)
    assert r.status_code == 400 and code_of(r) == "scope_missing"
    assert connections(db) == []


def test_optional_scopes_may_be_declined_and_their_imports_are_skipped(api, db):
    api.fake.granted_scopes = "read_products"
    assert connect_through_api(api.c, api.a.headers, api.fake).status_code == 200
    states = {
        s.resource.value: (s.status, s.error_code) for s in db.scalars(select(ShopifySyncState))
    }
    assert states["PRODUCTS"][0] == SyncStatus.PENDING
    assert states["CUSTOMERS"] == (SyncStatus.SKIPPED, "scope_missing")
    assert states["ORDERS"] == (SyncStatus.SKIPPED, "scope_missing")


def test_a_write_scope_satisfies_the_read_requirement(api, db):
    api.fake.granted_scopes = "write_products"
    assert connect_through_api(api.c, api.a.headers, api.fake).status_code == 200


def test_a_store_can_belong_to_only_one_organization(api, db):
    assert connect_through_api(api.c, api.a.headers, api.fake).status_code == 200
    again = api.c.post(CONNECT, json={"shop": api.fake.shop}, headers=api.b.headers)
    assert again.status_code == 409 and code_of(again) == "already_connected"
    assert [r.organization_id for r in connections(db)] == [uuid.UUID(api.a.org["id"])]


def test_the_ownership_check_also_holds_if_the_store_is_claimed_mid_flow(api, db, runtime):
    state = state_from(start(api, api.b))  # B starts while the store is free
    seed_connection(api.sf, uuid.UUID(api.a.org["id"]), runtime, api.fake)  # A claims it meanwhile
    r = complete(api, api.b, callback_query(api.fake, state))
    assert r.status_code == 409 and code_of(r) == "already_connected"
    assert len(connections(db)) == 1


def test_a_disconnected_store_can_be_connected_by_another_organization(api, db, runtime):
    cid = seed_connection(api.sf, uuid.UUID(api.a.org["id"]), runtime, api.fake)
    assert (
        api.c.post("/api/integrations/shopify/disconnect", headers=api.a.headers).status_code == 200
    )
    assert connect_through_api(api.c, api.b.headers, api.fake).status_code == 200
    rows = {r.organization_id: r.status for r in connections(db)}
    assert rows[uuid.UUID(api.a.org["id"])] == ShopifyConnectionStatus.DISCONNECTED
    assert rows[uuid.UUID(api.b.org["id"])] == ShopifyConnectionStatus.SYNCING
    assert cid


def test_an_organization_must_disconnect_before_switching_to_a_different_store(api, runtime):
    seed_connection(api.sf, uuid.UUID(api.a.org["id"]), runtime, api.fake)
    r = api.c.post(CONNECT, json={"shop": "other-store.myshopify.com"}, headers=api.a.headers)
    assert r.status_code == 409 and code_of(r) == "different_store"


def test_reauthorizing_the_same_store_updates_the_connection_in_place(api, db, runtime):
    cid = seed_connection(
        api.sf, uuid.UUID(api.a.org["id"]), runtime, api.fake, status=ShopifyConnectionStatus.ERROR
    )
    old_token = connections(db)[0].access_token_encrypted
    assert connect_through_api(api.c, api.a.headers, api.fake).status_code == 200
    (row,) = connections(db)
    assert row.id == cid and row.status == ShopifyConnectionStatus.SYNCING
    assert row.access_token_encrypted != old_token and row.error_code is None


def test_a_bad_authorization_code_fails_cleanly(api, db):
    state = state_from(start(api, api.a))
    r = complete(api, api.a, callback_query(api.fake, state, code="not-a-real-code"))
    assert r.status_code == 502 and code_of(r) == "exchange_failed"
    assert connections(db) == []


def test_shopify_reporting_a_different_store_is_rejected(api, db):
    api.fake.shop = "someone-else.myshopify.com"  # what the shop lookup answers
    state = state_from(start(api, api.a, shop="test-shop.myshopify.com"))
    query = callback_query(api.fake, state, shop="test-shop.myshopify.com")
    r = complete(api, api.a, query)
    assert r.status_code == 400 and code_of(r) == "shop_mismatch"
    assert connections(db) == []


def test_error_responses_never_echo_the_code_state_or_secrets(api):
    state = state_from(start(api, api.a))
    bad_signature = {
        **callback_query(api.fake, state, code="SUPER-SECRET-AUTH-CODE"),
        "hmac": "bad",
    }
    rejected = complete(api, api.a, bad_signature)
    assert rejected.status_code == 400
    # and a failed exchange (a signed callback whose code Shopify refuses) does not echo it either
    refused = complete(
        api,
        api.a,
        callback_query(api.fake, state_from(start(api, api.a)), code="SUPER-SECRET-AUTH-CODE"),
    )
    assert refused.status_code == 502
    for response in (rejected, refused):
        for leak in ("SUPER-SECRET-AUTH-CODE", state, "test-client-secret", "Traceback"):
            assert leak not in response.text


def test_a_user_in_two_organizations_cannot_finish_one_organizations_flow_in_the_other(api, db):
    both = signup_and_login(api.c, "both@example.com")
    for owner in (api.a, api.b):
        assert (
            add_member(
                api.c, owner.headers, owner.org["id"], "both@example.com", "ADMIN"
            ).status_code
            == 201
        )
    in_a = {**both, "X-Organization-ID": api.a.org["id"]}
    in_b = {**both, "X-Organization-ID": api.b.org["id"]}

    started = api.c.post(CONNECT, json={"shop": api.fake.shop}, headers=in_a)
    query = callback_query(api.fake, state_from(started.json()["authorization_url"]))

    wrong_org = api.c.get(f"{CALLBACK}?{urlencode(query)}", headers=in_b)
    assert wrong_org.status_code == 400 and code_of(wrong_org) == "invalid_state"
    assert connections(db) == []  # nothing was attached to the organization the flow was not for
    assert api.fake.token_requests == []

    right_org = api.c.get(f"{CALLBACK}?{urlencode(query)}", headers=in_a)
    assert right_org.status_code == 200
    assert [c.organization_id for c in connections(db)] == [uuid.UUID(api.a.org["id"])]


def test_an_admin_demoted_mid_flow_cannot_complete_it(api, db):
    from app.db.models import OrganizationMember, OrganizationRole

    headers = signup_and_login(api.c, "demoted@example.com")
    assert (
        add_member(
            api.c, api.a.headers, api.a.org["id"], "demoted@example.com", "ADMIN"
        ).status_code
        == 201
    )
    admin = {**headers, "X-Organization-ID": api.a.org["id"]}
    started = api.c.post(CONNECT, json={"shop": api.fake.shop}, headers=admin)
    query = callback_query(api.fake, state_from(started.json()["authorization_url"]))

    db.execute(
        update(OrganizationMember)
        .where(OrganizationMember.role == OrganizationRole.ADMIN)
        .values(role=OrganizationRole.MEMBER)
    )
    db.commit()

    refused = api.c.get(f"{CALLBACK}?{urlencode(query)}", headers=admin)
    assert refused.status_code == 403
    assert connections(db) == [] and api.fake.token_requests == []


def test_starting_a_flow_discards_my_own_earlier_attempt_but_never_a_teammates(api):
    mate_headers = signup_and_login(api.c, "mate@example.com")
    assert (
        add_member(api.c, api.a.headers, api.a.org["id"], "mate@example.com", "ADMIN").status_code
        == 201
    )
    mate = type("Mate", (), {"headers": {**mate_headers, "X-Organization-ID": api.a.org["id"]}})

    mine_first = state_from(start(api, api.a))
    theirs = state_from(start(api, mate))
    state_from(start(api, api.a))  # a second attempt of mine replaces the first

    assert code_of(complete(api, api.a, callback_query(api.fake, mine_first))) == "invalid_state"
    assert complete(api, mate, callback_query(api.fake, theirs)).status_code == 200
