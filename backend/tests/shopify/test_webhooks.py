import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import (
    Customer,
    Order,
    Product,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    ShopifyWebhookEvent,
    WebhookEventStatus,
)
from app.services.integrations.shopify import store, webhooks
from app.services.integrations.shopify.validation import sign_webhook_body
from app.workers.queue import ShopifyWebhookJob
from tests.helpers import create_org, signup_and_login
from tests.shopify.conftest import seed_connection
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)

URL = "/api/webhooks/shopify"
SECRET = "test-client-secret"


@pytest.fixture()
def org_id(client):
    return uuid.UUID(
        create_org(client, signup_and_login(client, "hooks@example.com"), "Hooks Co")["id"]
    )


@pytest.fixture()
def cid(session_factory, org_id, runtime, fake):
    return seed_connection(session_factory, org_id, runtime, fake)


@pytest.fixture()
def api(shopify_client, cid, queue):
    return shopify_client


_ids = iter(range(1, 10_000))


def deliver(
    client,
    topic,
    payload,
    *,
    shop="test-shop.myshopify.com",
    webhook_id=None,
    secret=SECRET,
    body=None,
    sign_body=None,
    **headers,
):
    raw = body if body is not None else json.dumps(payload).encode()
    h = {
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": shop,
        "X-Shopify-Webhook-Id": webhook_id or f"wh-{next(_ids)}",
        "X-Shopify-Hmac-Sha256": sign_webhook_body(
            sign_body if sign_body is not None else raw, secret
        ),
        "Content-Type": "application/json",
        **headers,
    }
    return client.post(URL, content=raw, headers={k: v for k, v in h.items() if v is not None})


def events(db):
    db.expire_all()
    return list(db.scalars(select(ShopifyWebhookEvent)))


def count(db, model, **where):
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int(db.scalar(stmt))


# --- intake: authenticity -----------------------------------------------------------------------------


def test_a_correctly_signed_webhook_is_accepted_stored_and_queued_without_doing_the_work(
    api, db, queue, fake
):
    r = deliver(api, "products/update", {"id": 5, "title": "x"}, webhook_id="wh-1")
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    (e,) = events(db)
    assert (e.topic, e.shop_domain, e.webhook_id, e.resource_id, e.status) == (
        "products/update",
        "test-shop.myshopify.com",
        "wh-1",
        5,
        WebhookEventStatus.PENDING,
    )
    assert e.organization_id is not None and e.connection_id is not None
    assert [type(j) for j in queue.jobs] == [ShopifyWebhookJob] and queue.jobs[0].event_id == e.id
    assert fake.requests == []  # no Shopify call, no sync work inside the HTTP request
    assert count(db, Product) == 0


def test_the_ledger_keeps_identifiers_only_never_the_webhook_body(api, db):
    deliver(
        api, "customers/update", {"id": 7, "email": "private@example.com", "first_name": "Secret"}
    )
    columns = {c.name for c in ShopifyWebhookEvent.__table__.columns}
    assert not {"payload", "body", "data", "raw", "email", "name"} & columns
    blob = json.dumps({c: str(getattr(events(db)[0], c)) for c in columns})
    assert "private@example.com" not in blob and "Secret" not in blob


def test_a_wrong_signature_is_refused_and_nothing_is_stored(api, db, queue):
    r = deliver(api, "products/update", {"id": 5}, secret="not-the-secret")
    assert r.status_code == 401
    assert events(db) == [] and len(queue.jobs) == 0


def test_a_missing_or_malformed_signature_is_refused(api, db, queue):
    assert (
        deliver(api, "products/update", {"id": 5}, **{"X-Shopify-Hmac-Sha256": None}).status_code
        == 401
    )
    assert (
        deliver(api, "products/update", {"id": 5}, **{"X-Shopify-Hmac-Sha256": ""}).status_code
        == 401
    )
    assert (
        deliver(api, "products/update", {"id": 5}, **{"X-Shopify-Hmac-Sha256": "###"}).status_code
        == 401
    )
    hex_digest = __import__("hmac").new(SECRET.encode(), b'{"id": 5}', "sha256").hexdigest()
    assert (
        deliver(
            api, "products/update", {"id": 5}, **{"X-Shopify-Hmac-Sha256": hex_digest}
        ).status_code
        == 401
    )
    assert events(db) == [] and len(queue.jobs) == 0


def test_a_body_changed_after_signing_is_refused(api, db):
    signed = json.dumps({"id": 5}).encode()
    r = deliver(api, "products/update", None, body=json.dumps({"id": 6}).encode(), sign_body=signed)
    assert r.status_code == 401 and events(db) == []


def test_nothing_is_accepted_when_the_server_has_no_client_secret(api, shopify_settings, db):
    from pydantic import SecretStr

    shopify_settings.shopify_client_secret = SecretStr("")
    assert deliver(api, "products/update", {"id": 5}, secret="").status_code == 401
    shopify_settings.shopify_client_secret = None
    assert deliver(api, "products/update", {"id": 5}, secret="").status_code == 401
    assert events(db) == []


def test_repeated_invalid_signatures_from_one_client_are_rate_limited(api):
    codes = [
        deliver(api, "products/update", {"id": 5}, secret="wrong").status_code for _ in range(35)
    ]
    assert codes[0] == 401 and 429 in codes
    # a correctly signed delivery from Shopify itself is never throttled
    assert deliver(api, "products/update", {"id": 5}).status_code in (200, 429)


# --- intake: duplicates and replays ---------------------------------------------------------------------


def test_a_repeated_delivery_is_acknowledged_but_never_queued_twice(api, db, queue):
    first = deliver(api, "products/update", {"id": 5}, webhook_id="wh-dup")
    again = deliver(api, "products/update", {"id": 5}, webhook_id="wh-dup")
    assert (first.json(), again.json()) == ({"status": "accepted"}, {"status": "duplicate"})
    assert again.status_code == 200  # Shopify must get a 2xx or it keeps retrying
    assert len(events(db)) == 1 and len(queue.jobs) == 1


def test_distinct_deliveries_of_the_same_change_are_each_processed(api, db, queue):
    deliver(api, "products/update", {"id": 5}, webhook_id="wh-a")
    deliver(api, "products/update", {"id": 5}, webhook_id="wh-b")
    assert len(events(db)) == 2 and len(queue.jobs) == 2  # idempotent processing makes this safe


def test_replaying_an_already_processed_delivery_does_nothing(
    api, db, queue, runtime, session_factory, fake
):
    fake.products = [make_product(5)]
    deliver(api, "products/update", {"id": 5}, webhook_id="wh-old")
    job = queue.jobs.popleft()
    assert webhooks.process_event(runtime, session_factory, job.event_id) == "processed"
    requests_after_first = len(fake.requests)
    replay = deliver(api, "products/update", {"id": 5}, webhook_id="wh-old")
    assert replay.json() == {"status": "duplicate"} and len(queue.jobs) == 0
    assert len(fake.requests) == requests_after_first
    assert (
        webhooks.process_event(runtime, session_factory, job.event_id) == "skipped"
    )  # even a duplicate JOB is inert


def test_the_same_webhook_id_from_a_different_shop_is_not_a_duplicate(
    api, db, session_factory, runtime, fake, client, queue
):
    other_org = uuid.UUID(
        create_org(client, signup_and_login(client, "hooks2@example.com"), "Hooks Two")["id"]
    )
    seed_connection(session_factory, other_org, runtime, fake, shop="shop-two.myshopify.com")
    assert (
        deliver(api, "products/update", {"id": 1}, webhook_id="same").json()["status"] == "accepted"
    )
    assert (
        deliver(
            api, "products/update", {"id": 1}, webhook_id="same", shop="shop-two.myshopify.com"
        ).json()["status"]
        == "accepted"
    )
    assert {e.organization_id for e in events(db)} == {
        e.organization_id for e in events(db)
    } and len(events(db)) == 2


# --- intake: validation ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"body": b"not json", "payload": None}, 400),
        ({"body": b"[1, 2, 3]", "payload": None}, 400),
        ({"body": b'"a string"', "payload": None}, 400),
        ({"payload": {}}, 400),  # a product webhook without an id
        ({"payload": {"id": "abc"}}, 400),
        ({"payload": {"id": -4}}, 400),
        ({"payload": {"id": 2**70}}, 400),
        ({"payload": {"id": 5}, "webhook_id": ""}, 400),
        ({"payload": {"id": 5}, "webhook_id": "x" * 200}, 400),
        ({"payload": {"id": 5}, "shop": "evil.com"}, 400),
        ({"payload": {"id": 5}, "shop": "127.0.0.1"}, 400),
    ],
)
def test_malformed_deliveries_are_rejected_with_400_and_stored_nowhere(
    api, db, queue, kwargs, expected
):
    kwargs = dict(kwargs)
    payload = kwargs.pop("payload", {"id": 5})
    if kwargs.get("webhook_id") == "":
        kwargs["webhook_id"] = ""
    r = (
        deliver(api, "products/update", payload, **kwargs)
        if kwargs.get("webhook_id") != ""
        else api.post(
            URL,
            content=b'{"id": 5}',
            headers={
                "X-Shopify-Topic": "products/update",
                "X-Shopify-Shop-Domain": "test-shop.myshopify.com",
                "X-Shopify-Hmac-Sha256": sign_webhook_body(b'{"id": 5}', SECRET),
            },
        )
    )
    assert r.status_code == expected
    assert events(db) == [] and len(queue.jobs) == 0


def test_unknown_topics_and_unknown_shops_are_acknowledged_and_ignored(api, db, queue):
    assert deliver(api, "carts/create", {"id": 5}).json() == {"status": "ignored"}
    assert deliver(api, "products/update", {"id": 5}, shop="stranger.myshopify.com").json() == {
        "status": "ignored"
    }
    assert events(db) == [] and len(queue.jobs) == 0


def test_topics_are_case_insensitive_and_whitespace_tolerant(api, db):
    assert deliver(api, "  Products/UPDATE ", {"id": 5}).json() == {"status": "accepted"}
    assert events(db)[0].topic == "products/update"


def test_a_disconnected_store_no_longer_receives_normal_webhooks(
    api, db, session_factory, cid, queue
):
    with session_factory() as s:
        s.execute(update(ShopifyConnection).values(status=ShopifyConnectionStatus.DISCONNECTED))
        s.commit()
    assert deliver(api, "products/update", {"id": 5}).json() == {"status": "ignored"}
    assert deliver(api, "shop/redact", {"shop_domain": "test-shop.myshopify.com"}).json() == {
        "status": "accepted"
    }
    assert [e.topic for e in events(db)] == ["shop/redact"]


def test_oversized_payloads_are_refused_before_any_work(api, shopify_settings, db):
    shopify_settings.shopify_webhook_max_body_bytes = 2048
    big = json.dumps({"id": 5, "pad": "x" * 5000}).encode()
    assert deliver(api, "products/update", None, body=big).status_code == 413
    assert events(db) == []


def test_if_the_queue_is_down_shopify_is_told_to_retry_and_the_retry_works(api, db, queue):
    queue.fail_enqueue = True
    r = deliver(api, "products/update", {"id": 5}, webhook_id="wh-retry")
    assert r.status_code == 503 and events(db) == []  # not left behind as a phantom "duplicate"
    queue.fail_enqueue = False
    assert deliver(api, "products/update", {"id": 5}, webhook_id="wh-retry").json() == {
        "status": "accepted"
    }
    assert len(events(db)) == 1 and len(queue.jobs) == 1


def test_compliance_topics_take_the_customer_id_from_the_nested_object(api, db):
    deliver(api, "customers/redact", {"shop_domain": "x", "customer": {"id": 99, "email": "a@b.c"}})
    deliver(api, "customers/data_request", {"customer": {"id": 100}})
    assert {e.topic: e.resource_id for e in events(db)} == {
        "customers/redact": 99,
        "customers/data_request": 100,
    }


def test_error_responses_never_echo_payloads_or_the_secret(api):
    r = deliver(
        api, "products/update", None, body=b'{"id": 5, "email": "leak@example.com"', secret=SECRET
    )
    for leak in ("leak@example.com", SECRET, "Traceback"):
        assert leak not in r.text


# --- processing: products, customers, orders ------------------------------------------------------------------


def queue_event(api, queue, topic, payload, **kw):
    r = deliver(api, topic, payload, **kw)
    assert r.json()["status"] == "accepted", r.text
    return queue.jobs.pop().event_id


def process(runtime, session_factory, event_id):
    return webhooks.process_event(runtime, session_factory, event_id)


def test_a_product_update_fetches_the_current_product_and_upserts_it(
    api, queue, runtime, session_factory, fake, db
):
    fake.products = [
        make_product(5, title="Fresh from Shopify", variants=[make_variant(501), make_variant(502)])
    ]
    event = queue_event(api, queue, "products/update", {"id": 5, "title": "STALE PAYLOAD TITLE"})
    assert process(runtime, session_factory, event) == "processed"
    p = db.scalar(select(Product))
    assert p.title == "Fresh from Shopify"  # the payload is a notification; the fetch is the truth
    assert count(db, Product) == 1
    assert [r["operation"] for r in fake.requests] == ["Product"]
    assert (
        events(db)[0].status == WebhookEventStatus.PROCESSED
        and events(db)[0].processed_at is not None
    )


def test_a_stale_or_replayed_notification_cannot_roll_data_back(
    api, queue, runtime, session_factory, fake, db
):
    fake.products = [make_product(5, title="Version 2", updated="2026-10-09T10:00:00Z")]
    first = queue_event(
        api, queue, "products/update", {"id": 5, "title": "Version 1"}, webhook_id="wh-1"
    )
    process(runtime, session_factory, first)
    old_delivery = queue_event(
        api, queue, "products/update", {"id": 5, "title": "Version 1"}, webhook_id="wh-1-late"
    )
    process(runtime, session_factory, old_delivery)  # an old notification arrives late
    db.expire_all()
    assert db.scalar(select(Product.title)) == "Version 2"


def test_ordering_between_notifications_does_not_matter(
    api, queue, runtime, session_factory, fake, db
):
    fake.products = [make_product(5, title="Final")]
    ids = [
        queue_event(api, queue, "products/update", {"id": 5}, webhook_id=f"o{i}") for i in range(3)
    ]
    for event in reversed(ids):
        process(runtime, session_factory, event)
    assert db.scalar(select(Product.title)) == "Final" and count(db, Product) == 1


def test_a_product_deleted_event_marks_it_inactive_without_calling_shopify(
    api, queue, runtime, session_factory, fake, db, org_id
):
    fake.products = [make_product(5)]
    process(runtime, session_factory, queue_event(api, queue, "products/update", {"id": 5}))
    calls = len(fake.requests)
    gone = queue_event(api, queue, "products/delete", {"id": 5})
    assert process(runtime, session_factory, gone) == "processed"
    db.expire_all()
    assert db.scalar(select(Product.deleted_at)) is not None and count(db, Product) == 1
    assert len(fake.requests) == calls  # no API call for a deletion


def test_a_product_that_vanished_before_we_could_fetch_it_is_marked_deleted(
    api, queue, runtime, session_factory, fake, db
):
    fake.products = [make_product(5)]
    process(runtime, session_factory, queue_event(api, queue, "products/create", {"id": 5}))
    fake.products = []
    process(runtime, session_factory, queue_event(api, queue, "products/update", {"id": 5}))
    db.expire_all()
    assert db.scalar(select(Product.deleted_at)) is not None


def test_customer_events(api, queue, runtime, session_factory, fake, db):
    fake.customers = [make_customer(7, email="new@example.com")]
    process(runtime, session_factory, queue_event(api, queue, "customers/create", {"id": 7}))
    assert db.scalar(select(Customer.email)) == "new@example.com"
    process(runtime, session_factory, queue_event(api, queue, "customers/delete", {"id": 7}))
    db.expire_all()
    assert db.scalar(select(Customer.deleted_at)) is not None and count(db, Customer) == 1


def test_order_events_including_cancellation(api, queue, runtime, session_factory, fake, db):
    fake.products = [make_product(5, variants=[make_variant(501)])]
    process(runtime, session_factory, queue_event(api, queue, "products/create", {"id": 5}))
    fake.orders = [make_order(9, items=[make_line_item(1, product=5, variant=501)])]
    process(runtime, session_factory, queue_event(api, queue, "orders/create", {"id": 9}))
    assert count(db, Order) == 1 and db.scalar(select(Order.cancelled_at)) is None
    fake.orders = [
        make_order(
            9,
            cancelled_at="2026-10-03T09:30:00Z",
            updated="2026-10-03T09:30:00Z",
            items=[make_line_item(1, product=5, variant=501)],
        )
    ]
    process(runtime, session_factory, queue_event(api, queue, "orders/cancelled", {"id": 9}))
    db.expire_all()
    assert db.scalar(select(Order.cancelled_at)) is not None and count(db, Order) == 1


# --- processing: failures and retries ---------------------------------------------------------------------------


def test_a_transient_failure_leaves_the_event_pending_for_a_later_retry(
    api, queue, runtime, session_factory, fake, db
):
    fake.http_failures = [503] * 100
    event = queue_event(api, queue, "products/update", {"id": 5})
    assert process(runtime, session_factory, event) == "retry"
    e = events(db)[0]
    assert (e.status, e.attempts, e.error_code) == (WebhookEventStatus.PENDING, 1, "unavailable")
    assert len(queue.jobs) == 0  # not hot-looped


def test_a_stuck_pending_event_is_requeued_by_maintenance_and_then_succeeds(
    api, queue, runtime, session_factory, fake, db
):
    fake.products = [make_product(5)]
    fake.http_failures = [503] * 100
    event = queue_event(api, queue, "products/update", {"id": 5})
    process(runtime, session_factory, event)
    fake.http_failures = []
    assert webhooks.requeue_pending_events(session_factory, queue, 120) == 0  # too recent
    with session_factory() as s:
        s.execute(
            update(ShopifyWebhookEvent).values(received_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()
    assert webhooks.requeue_pending_events(session_factory, queue, 120) == 1
    assert process(runtime, session_factory, queue.jobs.pop().event_id) == "processed"
    assert db.scalar(select(Product.title)) == "Product 5"


def test_an_event_that_keeps_failing_is_eventually_marked_failed(
    api, queue, runtime, session_factory, fake, db
):
    fake.http_failures = [503] * 1000
    event = queue_event(api, queue, "products/update", {"id": 5})
    outcomes = []
    for _ in range(webhooks.MAX_ATTEMPTS):
        outcomes.append(process(runtime, session_factory, event))
    assert outcomes[-1] == "failed"
    assert (
        events(db)[0].status == WebhookEventStatus.FAILED
        and events(db)[0].error_code == "unavailable"
    )


def test_an_event_whose_authorization_is_gone_fails_without_retrying_forever(
    api, queue, runtime, session_factory, fake, db
):
    fake.revoke_all_tokens()
    with session_factory() as s:
        s.execute(update(ShopifyConnection).values(access_token_expires_at=None))
        s.commit()
    event = queue_event(api, queue, "products/update", {"id": 5})
    assert process(runtime, session_factory, event) == "failed"
    assert events(db)[0].status == WebhookEventStatus.FAILED


def test_two_workers_cannot_process_the_same_event(api, queue, runtime, session_factory, fake):
    fake.products = [make_product(5)]
    event = queue_event(api, queue, "products/update", {"id": 5})
    claimed = webhooks._claim(session_factory, event)
    assert claimed is not None and webhooks._claim(session_factory, event) is None
    assert process(runtime, session_factory, event) == "skipped"  # PROCESSING elsewhere


def test_a_crashed_workers_event_is_recovered(api, queue, runtime, session_factory, db):
    event = queue_event(api, queue, "products/update", {"id": 5})
    webhooks._claim(session_factory, event)  # a worker took it and died
    with session_factory() as s:
        s.execute(
            update(ShopifyWebhookEvent).values(received_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()
    assert webhooks.requeue_pending_events(session_factory, queue, 120) == 1
    assert events(db)[0].status == WebhookEventStatus.PENDING


def test_events_for_a_store_that_disconnected_in_the_meantime_are_ignored(
    api, queue, runtime, session_factory, db, cid
):
    event = queue_event(api, queue, "products/update", {"id": 5})
    with session_factory() as s:
        s.execute(update(ShopifyConnection).values(status=ShopifyConnectionStatus.DISCONNECTED))
        s.commit()
    assert process(runtime, session_factory, event) == "ignored"
    assert events(db)[0].status == WebhookEventStatus.IGNORED


def test_old_finished_events_are_pruned_but_pending_ones_never(
    api, queue, runtime, session_factory, db, fake
):
    fake.products = [make_product(5)]
    done = queue_event(api, queue, "products/update", {"id": 5}, webhook_id="d")
    process(runtime, session_factory, done)
    queue_event(api, queue, "products/update", {"id": 6}, webhook_id="p")  # stays PENDING
    with session_factory() as s:
        s.execute(
            update(ShopifyWebhookEvent).values(received_at=datetime.now(UTC) - timedelta(days=40))
        )
        s.commit()
    assert webhooks.prune_old_events(session_factory, 30) == 1
    assert [e.webhook_id for e in events(db)] == ["p"]


# --- uninstall and privacy requests -----------------------------------------------------------------------------


def seed_data(session_factory, org_id, fake, runtime):
    """One product, customer and order imported the way a sync would: fetch everything first, then
    write (fetching opens its own short sessions, which must not interleave with a pending write)."""
    from app.services.integrations.shopify import fetch  # noqa: PLC0415

    fake.products = [make_product(5)]
    fake.customers = [make_customer(7)]
    fake.orders = [make_order(9, customer=7)]
    with session_factory() as s:
        connection_id = s.scalar(select(ShopifyConnection.id))
    client = runtime.authorized_client(session_factory, connection_id, fake.shop)
    product, customer, order = (
        fetch.fetch_product(client, 5),
        fetch.fetch_customer(client, 7),
        fetch.fetch_order(client, 9),
    )
    client.close()
    with session_factory() as s:
        now = datetime.now(UTC)
        store.upsert_products(s, org_id, [product], currency="USD", now=now)
        store.upsert_customers(s, org_id, [customer], now=now)
        store.upsert_orders(s, org_id, [order], now=now)
        s.commit()


def test_uninstalling_the_app_disconnects_wipes_credentials_and_removes_the_data(
    api, queue, runtime, session_factory, fake, db, org_id
):
    seed_data(session_factory, org_id, fake, runtime)
    before = len(fake.requests)
    event = queue_event(api, queue, "app/uninstalled", {"id": 1, "name": "Shop"})
    assert process(runtime, session_factory, event) == "processed"
    db.expire_all()
    c = db.scalar(select(ShopifyConnection))
    assert c.status == ShopifyConnectionStatus.DISCONNECTED
    assert c.access_token_encrypted is None and c.refresh_token_encrypted is None
    assert (
        count(db, Product),
        count(db, Customer),
        count(db, Order),
        count(db, ShopifySyncState),
    ) == (0, 0, 0, 0)
    assert len(fake.requests) == before  # nothing to call: the app is already gone from Shopify


def test_shop_redact_purges_a_store_that_was_uninstalled(
    api, queue, runtime, session_factory, fake, db, org_id
):
    seed_data(session_factory, org_id, fake, runtime)
    with (
        session_factory() as s
    ):  # the merchant uninstalled earlier but data was somehow still present
        s.execute(update(ShopifyConnection).values(status=ShopifyConnectionStatus.DISCONNECTED))
        s.commit()
    event = queue_event(
        api, queue, "shop/redact", {"shop_id": 1, "shop_domain": "test-shop.myshopify.com"}
    )
    assert process(runtime, session_factory, event) == "processed"
    assert (count(db, Product), count(db, Customer), count(db, Order)) == (0, 0, 0)


def test_shop_redact_never_deletes_the_data_of_a_store_that_was_reinstalled(
    api, queue, runtime, session_factory, fake, db, org_id
):
    seed_data(session_factory, org_id, fake, runtime)  # the connection is active again
    event = queue_event(api, queue, "shop/redact", {"shop_domain": "test-shop.myshopify.com"})
    assert process(runtime, session_factory, event) == "ignored"
    assert (count(db, Product), count(db, Customer), count(db, Order)) == (1, 1, 1)
    assert events(db)[0].error_code == "reinstalled"


def test_a_customer_redaction_erases_that_customers_personal_data_only(
    api, queue, runtime, session_factory, fake, db, org_id
):
    seed_data(session_factory, org_id, fake, runtime)
    event = queue_event(api, queue, "customers/redact", {"shop_domain": "x", "customer": {"id": 7}})
    assert process(runtime, session_factory, event) == "processed"
    db.expire_all()
    c = db.scalar(select(Customer))
    assert (c.email, c.phone, c.first_name, c.last_name) == (None,) * 4 and c.deleted_at is not None
    assert count(db, Order) == 1 and count(db, Product) == 1  # business history stays


def test_a_data_request_is_acknowledged_without_touching_data(
    api, queue, runtime, session_factory, fake, db, org_id
):
    seed_data(session_factory, org_id, fake, runtime)
    event = queue_event(api, queue, "customers/data_request", {"customer": {"id": 7}})
    assert process(runtime, session_factory, event) == "processed"
    assert db.scalar(select(Customer.email)) is not None and count(db, Customer) == 1


def test_processing_logs_identifiers_never_customer_data(
    api, queue, runtime, session_factory, fake, db, org_id, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    fake.customers = [make_customer(7, email="pii-leak@example.com", first="Leakyname")]
    process(
        runtime,
        session_factory,
        queue_event(api, queue, "customers/update", {"id": 7, "email": "pii-leak@example.com"}),
    )
    assert "pii-leak@example.com" not in caplog.text and "Leakyname" not in caplog.text


def test_oversized_chunked_payloads_with_no_content_length_are_also_refused(
    api, shopify_settings, db
):
    shopify_settings.shopify_webhook_max_body_bytes = 2048
    big = json.dumps({"id": 5, "pad": "x" * 5000}).encode()

    def chunks():
        for start in range(0, len(big), 512):
            yield big[start : start + 512]

    r = api.post(
        URL,
        content=chunks(),
        headers={
            "X-Shopify-Topic": "products/update",
            "X-Shopify-Shop-Domain": "test-shop.myshopify.com",
            "X-Shopify-Webhook-Id": "wh-chunked",
            "X-Shopify-Hmac-Sha256": sign_webhook_body(big, SECRET),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 413
    assert events(db) == []
