"""What the application writes to its logs while it connects a store, syncs it, receives webhooks,
refreshes and loses tokens, and gets throttled.

The records are captured raw (before the log formatter's redaction safety net), so a pass means the
code itself never emits a secret or a customer's personal data, not merely that it is masked later.
"""

import io
import json
import logging
import traceback
import uuid
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from app.core.logging import configure_logging
from app.db.models import Customer, ShopifyConnection
from app.services.integrations.shopify import webhooks
from app.workers.queue import InMemoryJobQueue, ShopifyWebhookJob
from tests.shopify.conftest import callback_query, state_from
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)
from tests.shopify.test_sync import begin, full_sync, run
from tests.shopify.test_webhooks import deliver

BASE = "/api/integrations/shopify"
OAUTH_CODE = "oauth-code-7f3a9c-never-log-me"

# Personal data that exists only in the shop's customers, orders and webhook bodies.
PII = {
    "first_name": "Zephyrine",
    "last_name": "Quillfeather",
    "email": "zephyrine.quillfeather@pii-test.example",
    "phone": "+4930901820777",
    "address": "99 Hidden Street",
    "webhook_email": "webhook.buyer@pii-test.example",
    "webhook_note": "gift for my neighbour at flat 4B",
}


class Capture:
    """Everything logged, as it was emitted: message, arguments and the traceback text."""

    def __init__(self, caplog) -> None:
        self.caplog = caplog

    def text(self) -> str:
        parts = []
        for record in self.caplog.records:
            parts.append(record.getMessage())
            if record.exc_info:
                parts.append("".join(traceback.format_exception(*record.exc_info)))
            if record.stack_info:
                parts.append(record.stack_info)
        return "\n".join(parts)


@pytest.fixture()
def audit(caplog, fake, shopify_client, tenants, session_factory, runtime, queue):
    configure_logging("DEBUG", io.StringIO())  # the application's real logging setup
    caplog.set_level(logging.DEBUG)
    issued: list[str] = []
    original = fake._issue

    def recording_issue():
        body = original()
        issued.extend(v for k, v in body.items() if k in {"access_token", "refresh_token"})
        return body

    fake._issue = recording_issue
    fake.valid_codes = {OAUTH_CODE}
    fake.customers = [
        make_customer(
            n,
            email=PII["email"] if n == 1 else None,
            phone=PII["phone"] if n == 1 else None,
            first=PII["first_name"] if n == 1 else "Other",
            last=PII["last_name"] if n == 1 else "Person",
        )
        for n in range(1, 6)
    ]
    fake.products = [make_product(n, variants=[make_variant(n * 100 + 1)]) for n in range(1, 4)]
    fake.orders = [
        make_order(n, customer=1, items=[make_line_item(n * 10, product=1, variant=101)])
        for n in range(1, 4)
    ]
    a, _b = tenants
    return type(
        "Audit",
        (),
        {
            "c": shopify_client,
            "headers": a.headers,
            "org": a.org["id"],
            "fake": fake,
            "rt": runtime,
            "sf": session_factory,
            "q": queue,
            "log": Capture(caplog),
            "issued": issued,
            "query": {},
        },
    )


def connect(audit):
    started = audit.c.post(f"{BASE}/connect", json={"shop": audit.fake.shop}, headers=audit.headers)
    assert started.status_code == 200, started.text
    state = state_from(started.json()["authorization_url"])
    audit.query = callback_query(audit.fake, state, code=OAUTH_CODE)
    done = audit.c.get(f"{BASE}/callback?{urlencode(audit.query)}", headers=audit.headers)
    assert done.status_code == 200, done.text
    audit.state = state
    with audit.sf() as db:
        audit.cid = db.scalar(select(ShopifyConnection.id))


def forbidden(audit, extra: dict[str, str] | None = None) -> dict[str, str]:
    secrets = {
        "oauth code": OAUTH_CODE,
        "oauth state": getattr(audit, "state", "state-not-created-yet"),
        "oauth hmac": audit.query.get("hmac", "hmac-not-created-yet"),
        "app client secret": audit.fake.client_secret,
        "encryption key": audit.rt.settings.secret_encryption_key.get_secret_value(),
    }
    secrets.update({f"issued token {i}": t for i, t in enumerate(audit.issued)})
    secrets.update({f"personal data: {k}": v for k, v in PII.items()})
    secrets.update(extra or {})
    return secrets


def assert_clean(audit, extra: dict[str, str] | None = None):
    text = audit.log.text()
    assert len(text) > 200, "nothing was captured: the audit proves nothing"
    leaked = [name for name, value in forbidden(audit, extra).items() if value and value in text]
    assert leaked == [], f"logged: {leaked}"


# --- the flows -------------------------------------------------------------------------------------------------


def test_connecting_a_store_logs_no_code_state_signature_or_token(audit):
    connect(audit)
    assert audit.issued, "the fake issued no tokens, so nothing was exercised"
    assert "shopify" in audit.log.text().lower()
    assert_clean(audit)


def test_a_failed_authorization_logs_no_code_state_or_signature(audit):
    started = audit.c.post(f"{BASE}/connect", json={"shop": audit.fake.shop}, headers=audit.headers)
    audit.state = state_from(started.json()["authorization_url"])
    audit.query = callback_query(audit.fake, audit.state, code="a-wrong-code-123456")
    audit.query["hmac"] = "0" * 63 + "1"  # tampered signature
    bad = audit.c.get(f"{BASE}/callback?{urlencode(audit.query)}", headers=audit.headers)
    assert bad.status_code in (400, 401, 422)
    assert_clean(audit, {"submitted code": "a-wrong-code-123456", "forged hmac": "0" * 63 + "1"})


def test_a_full_import_of_products_customers_and_orders_logs_no_personal_data(audit):
    connect(audit)
    jobs = InMemoryJobQueue()
    assert full_sync(audit.rt, audit.sf, audit.cid, jobs) == "completed"
    with audit.sf() as db:  # the data really was imported, so the log check below means something
        assert db.scalar(select(Customer).where(Customer.email == PII["email"])) is not None
    assert_clean(audit)


def test_receiving_and_processing_webhooks_logs_no_payload_content(audit):
    connect(audit)
    body = {
        "id": 1,
        "email": PII["webhook_email"],
        "first_name": PII["first_name"],
        "note": PII["webhook_note"],
        "default_address": {"address1": PII["address"]},
        "phone": PII["phone"],
    }
    for topic in ("customers/update", "orders/create", "customers/delete", "products/update"):
        response = deliver(audit.c, topic, body, webhook_id=f"wh-{uuid.uuid4()}")
        assert response.status_code == 200
    # a request with a bad signature and a malformed body are logged without echoing either
    deliver(audit.c, "customers/update", body, secret="not-the-secret")
    deliver(audit.c, "customers/update", None, body=b'{"email": "' + PII["webhook_email"].encode())
    processed = 0
    while audit.q.jobs:
        job = audit.q.jobs.popleft()
        if isinstance(job, ShopifyWebhookJob):
            webhooks.process_event(audit.rt, audit.sf, job.event_id)
            processed += 1
    assert processed >= 4
    assert_clean(audit)


def test_token_refresh_and_a_revoked_token_log_no_token_material(audit, clock):
    connect(audit)
    jobs = InMemoryJobQueue()
    full_sync(audit.rt, audit.sf, audit.cid, jobs)
    clock.advance(10 * 24 * 3600)  # the access token has long expired: the next call refreshes it
    begin(audit.sf, audit.cid, audit.rt)
    outcome = run(audit.rt, audit.sf, audit.cid, jobs)
    assert outcome == "completed"
    assert len(audit.issued) >= 4, "a refresh should have issued a new pair"
    audit.fake.revoke_all_tokens()  # now Shopify rejects everything we hold
    clock.advance(24 * 3600)
    begin(audit.sf, audit.cid, audit.rt)
    run(audit.rt, audit.sf, audit.cid, jobs)
    with audit.sf() as db:  # the revocation was noticed, so the code path under audit really ran
        assert db.get(ShopifyConnection, audit.cid).error_code in {
            "auth_failed",
            "reauthorization_required",
        }
    assert_clean(audit)


def test_throttling_server_errors_and_malformed_answers_log_no_secret_or_pii(audit):
    connect(audit)
    jobs = InMemoryJobQueue()
    audit.fake.throttle_responses = 2
    audit.fake.http_failures = [500, 502, 503]
    full_sync(audit.rt, audit.sf, audit.cid, jobs)
    audit.fake.http_failures = [401, 403, 404, 423]
    begin(audit.sf, audit.cid, audit.rt)
    run(audit.rt, audit.sf, audit.cid, jobs)
    assert_clean(audit)


def test_restricted_customer_data_is_logged_by_count_never_by_value(audit):
    connect(audit)
    audit.fake.restrict_customer_pii = True
    full_sync(audit.rt, audit.sf, audit.cid, InMemoryJobQueue())
    assert_clean(audit)


def test_disconnecting_and_purging_log_no_secret_or_pii(audit):
    connect(audit)
    full_sync(audit.rt, audit.sf, audit.cid, InMemoryJobQueue())
    r = audit.c.post(f"{BASE}/disconnect", headers=audit.headers)
    assert r.status_code == 200
    assert_clean(audit)


def test_every_log_line_about_a_connection_uses_its_id_not_its_shop_credentials(audit):
    connect(audit)
    full_sync(audit.rt, audit.sf, audit.cid, InMemoryJobQueue())
    lines = [
        record.getMessage()
        for record in audit.log.caplog.records
        if record.name.startswith("app.services.integrations.shopify")
    ]
    assert lines
    assert any(str(audit.cid) in line for line in lines)
    for line in lines:
        assert "access_token" not in line.lower() or "access_token=" not in line.lower()
        assert not json.dumps(line).count("shpat_")
