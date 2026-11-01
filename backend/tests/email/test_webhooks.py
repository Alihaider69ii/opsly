import json
import time
from datetime import UTC, datetime

import pytest

from app.db.models import (
    Customer,
    EmailClassification,
    EmailConnection,
    EmailMessage,
    EmailMessageStatus,
    EmailThread,
    EmailWebhookEvent,
    SenderType,
)
from app.services.email import send as send_service
from app.services.email import webhooks as wh
from app.services.email.provider import InboundEmailContent
from app.services.email.providers.fake import (
    FakeEmailProvider,
    register_fake_inbound,
    reset_fake_inbound,
)
from app.services.email.validation import sign_svix_body
from tests.email.conftest import WEBHOOK_SECRET, seed_connection, seed_customer, seed_thread

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_fake_inbound_registry():
    reset_fake_inbound()
    yield
    reset_fake_inbound()


def _signed_body(body: bytes, *, secret: str = WEBHOOK_SECRET, svix_id: str = "msg_1", ts: str | None = None):
    svix_timestamp = ts or str(int(time.time()))
    signature = sign_svix_body(svix_id=svix_id, svix_timestamp=svix_timestamp, body=body, secret=secret)
    headers = {"svix-id": svix_id, "svix-timestamp": svix_timestamp, "svix-signature": signature}
    return body, headers


def _signed(payload: dict, *, secret: str = WEBHOOK_SECRET, svix_id: str = "msg_1", ts: str | None = None):
    return _signed_body(json.dumps(payload).encode("utf-8"), secret=secret, svix_id=svix_id, ts=ts)


def _inbound_payload(*, email_id: str, to: list[str], from_addr: str = "Sam <sam@example.com>", subject: str = "Help"):
    return {
        "type": "email.received",
        "created_at": NOW.isoformat(),
        "data": {"email_id": email_id, "from": from_addr, "to": to, "subject": subject},
    }


def _status_payload(*, email_id: str, event_type: str, created_at: str = "2026-01-01T12:00:00Z"):
    return {"type": event_type, "created_at": created_at, "data": {"email_id": email_id}}


# --- signature / intake -------------------------------------------------------------------------


def test_invalid_signature_is_rejected(db, org_id, session_factory, queue, email_settings):
    seed_connection(session_factory, org_id)
    body, headers = _signed(_inbound_payload(email_id="e1", to=["support@acme.example"]))
    headers["svix-signature"] = "v1,totally-wrong"
    with pytest.raises(wh.WebhookRejected) as exc_info:
        wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    assert exc_info.value.status_code == 401


def test_missing_signature_headers_is_rejected(db, email_settings, queue):
    body, _ = _signed(_inbound_payload(email_id="e1", to=["support@acme.example"]))
    with pytest.raises(wh.WebhookRejected) as exc_info:
        wh.receive_webhook(db, body=body, headers={}, settings=email_settings, queue=queue)
    assert exc_info.value.status_code == 401


def test_malformed_json_body_is_rejected(db, email_settings, queue):
    body, headers = _signed_body(b"not json")
    with pytest.raises(wh.WebhookRejected) as exc_info:
        wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    assert exc_info.value.status_code == 400


def test_an_inbound_to_an_unrecognized_address_is_ledgered_with_no_organization(
    db, org_id, session_factory, queue, email_settings
):
    seed_connection(session_factory, org_id)
    body, headers = _signed(_inbound_payload(email_id="e1", to=["nobody@elsewhere.example"]))
    result = wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    assert result.status == "accepted"
    event = db.get(EmailWebhookEvent, result.event_id)
    assert event.organization_id is None


def test_duplicate_delivery_of_the_same_inbound_email_is_deduped(
    db, org_id, session_factory, queue, email_settings
):
    connection_id = seed_connection(session_factory, org_id)
    connection = db.get(EmailConnection, connection_id)
    body, headers = _signed(_inbound_payload(email_id="dup-1", to=[connection.inbound_address]))

    first = wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    second = wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)

    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert db.query(EmailWebhookEvent).filter_by(dedupe_key="inbound:dup-1").count() == 1


def test_duplicate_status_event_for_the_same_email_and_created_at_is_deduped(
    db, org_id, session_factory, queue, email_settings
):
    seed_connection(session_factory, org_id)
    body, headers = _signed(_status_payload(email_id="msg-1", event_type="email.delivered"))

    first = wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    second = wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)

    assert first.status == "accepted"
    assert second.status == "duplicate"


# --- processing: inbound -------------------------------------------------------------------------


def test_inbound_processing_creates_customer_thread_message_and_automation_event(
    db, org_id, session_factory, queue, email_settings
):
    connection_id = seed_connection(session_factory, org_id)
    connection = db.get(EmailConnection, connection_id)
    email_id = "inbound-1"
    register_fake_inbound(
        email_id,
        InboundEmailContent(text_body="I need help with my order", html_body="<p>I need help</p>"),
    )
    body, headers = _signed(_inbound_payload(email_id=email_id, to=[connection.inbound_address]))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    job = queue.dequeue()

    outcome = wh.process_event(session_factory, job.event_id, email_settings)
    assert outcome == "processed"

    with session_factory() as fresh:
        message = fresh.query(EmailMessage).filter_by(organization_id=org_id).one()
        assert message.body_text == "I need help with my order"
        assert message.from_address == "sam@example.com"
        assert message.sender_type == SenderType.CUSTOMER
        customer = fresh.query(Customer).filter_by(organization_id=org_id).one()
        assert customer.email == "sam@example.com"
        thread = fresh.get(EmailThread, message.thread_id)
        assert thread.message_count == 1


def test_inbound_processing_is_idempotent_for_a_resubmitted_ledger_event(
    db, org_id, session_factory, queue, email_settings
):
    connection_id = seed_connection(session_factory, org_id)
    connection = db.get(EmailConnection, connection_id)
    email_id = "inbound-2"
    register_fake_inbound(email_id, InboundEmailContent(text_body="hello", html_body="<p>hello</p>"))
    body, headers = _signed(_inbound_payload(email_id=email_id, to=[connection.inbound_address]))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    job = queue.dequeue()

    first_outcome = wh.process_event(session_factory, job.event_id, email_settings)
    second_outcome = wh.process_event(session_factory, job.event_id, email_settings)

    assert first_outcome == "processed"
    assert second_outcome == "skipped"  # claim fails: event is no longer PENDING
    with session_factory() as fresh:
        assert fresh.query(EmailMessage).filter_by(organization_id=org_id).count() == 1


def test_an_inbound_event_with_no_resolved_connection_is_ignored(
    db, org_id, session_factory, queue, email_settings
):
    seed_connection(session_factory, org_id)
    body, headers = _signed(_inbound_payload(email_id="orphan-1", to=["nobody@elsewhere.example"]))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    job = queue.dequeue()

    outcome = wh.process_event(session_factory, job.event_id, email_settings)
    assert outcome == "ignored"


# --- processing: status transitions ---------------------------------------------------------------


def _send_one(db, org_id, connection, customer, thread, *, key: str) -> EmailMessage:
    return send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=send_service.OutboundEmail(
            subject="hi", text_body="hi", html_body="<p>hi</p>", classification=EmailClassification.TRANSACTIONAL
        ),
        idempotency_key=key,
        provider=FakeEmailProvider(),
        now=NOW,
    )


def test_delivered_status_progresses_rank_and_is_applied(db, org_id, session_factory, queue, email_settings):
    connection_id = seed_connection(session_factory, org_id)
    customer_id = seed_customer(session_factory, org_id)
    thread_id = seed_thread(session_factory, org_id, customer_id=customer_id)
    connection, customer, thread = (
        db.get(EmailConnection, connection_id),
        db.get(Customer, customer_id),
        db.get(EmailThread, thread_id),
    )
    sent = _send_one(db, org_id, connection, customer, thread, key="k-status-1")
    email_id = sent.provider_message_id

    body, headers = _signed(_status_payload(email_id=email_id, event_type="email.delivered", created_at="t1"))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    job = queue.dequeue()
    outcome = wh.process_event(session_factory, job.event_id, email_settings)
    assert outcome == "processed"

    db.refresh(sent)
    assert sent.status == EmailMessageStatus.DELIVERED
    assert sent.delivered_at is not None


def test_out_of_order_status_event_never_regresses_the_message(
    db, org_id, session_factory, queue, email_settings
):
    connection_id = seed_connection(session_factory, org_id)
    customer_id = seed_customer(session_factory, org_id)
    thread_id = seed_thread(session_factory, org_id, customer_id=customer_id)
    connection, customer, thread = (
        db.get(EmailConnection, connection_id),
        db.get(Customer, customer_id),
        db.get(EmailThread, thread_id),
    )
    sent = _send_one(db, org_id, connection, customer, thread, key="k-status-2")
    email_id = sent.provider_message_id

    # DELIVERED arrives first ...
    body, headers = _signed(_status_payload(email_id=email_id, event_type="email.delivered", created_at="t1"))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    wh.process_event(session_factory, queue.dequeue().event_id, email_settings)

    # ... then a stale "sent" event shows up late and must not regress it.
    body, headers = _signed(_status_payload(email_id=email_id, event_type="email.sent", created_at="t0"))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    wh.process_event(session_factory, queue.dequeue().event_id, email_settings)

    db.refresh(sent)
    assert sent.status == EmailMessageStatus.DELIVERED


def test_bounce_status_suppresses_the_customer_and_records_a_timeline_event(
    db, org_id, session_factory, queue, email_settings
):
    connection_id = seed_connection(session_factory, org_id)
    customer_id = seed_customer(session_factory, org_id)
    thread_id = seed_thread(session_factory, org_id, customer_id=customer_id)
    connection, customer, thread = (
        db.get(EmailConnection, connection_id),
        db.get(Customer, customer_id),
        db.get(EmailThread, thread_id),
    )
    sent = _send_one(db, org_id, connection, customer, thread, key="k-status-3")
    email_id = sent.provider_message_id

    body, headers = _signed(_status_payload(email_id=email_id, event_type="email.bounced", created_at="t1"))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    wh.process_event(session_factory, queue.dequeue().event_id, email_settings)

    db.refresh(sent)
    db.refresh(customer)
    assert sent.status == EmailMessageStatus.BOUNCED
    assert customer.email_suppressed is True

    from app.services.support import timeline as timeline_service

    events = timeline_service.list_customer_timeline(db, organization_id=org_id, customer_id=customer.id)
    assert any(e.event_type == "email_bounced" for e in events)


def test_unsubscribed_status_suppresses_the_customer(db, org_id, session_factory, queue, email_settings):
    connection_id = seed_connection(session_factory, org_id)
    customer_id = seed_customer(session_factory, org_id)
    thread_id = seed_thread(session_factory, org_id, customer_id=customer_id)
    connection, customer, thread = (
        db.get(EmailConnection, connection_id),
        db.get(Customer, customer_id),
        db.get(EmailThread, thread_id),
    )
    sent = _send_one(db, org_id, connection, customer, thread, key="k-status-unsub")
    email_id = sent.provider_message_id

    body, headers = _signed(_status_payload(email_id=email_id, event_type="email.suppressed", created_at="t1"))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    wh.process_event(session_factory, queue.dequeue().event_id, email_settings)

    db.refresh(sent)
    db.refresh(customer)
    assert sent.status == EmailMessageStatus.UNSUBSCRIBED
    assert customer.email_suppressed is True
    assert customer.email_marketing_consent is False


def test_status_event_for_an_unmatched_message_is_retried_then_eventually_failed(
    db, org_id, session_factory, queue, email_settings
):
    seed_connection(session_factory, org_id)
    seed_customer(session_factory, org_id)
    # There is no EmailMessage with this provider_message_id, so the connection can't be resolved
    # by `_connection_for_sent_message` either -- this event is ignored outright (unknown connection).
    body, headers = _signed(_status_payload(email_id="never-sent", event_type="email.delivered", created_at="t1"))
    wh.receive_webhook(db, body=body, headers=headers, settings=email_settings, queue=queue)
    outcome = wh.process_event(session_factory, queue.dequeue().event_id, email_settings)
    assert outcome == "ignored"
