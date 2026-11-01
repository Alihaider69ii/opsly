from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.models import (
    Customer,
    EmailClassification,
    EmailConnection,
    EmailMessageStatus,
    EmailThread,
)
from app.services.email import consent as consent_service
from app.services.email import send as send_service
from app.services.email.errors import EmailErrorCode
from app.services.email.providers.fake import FakeEmailProvider
from tests.email.conftest import seed_connection, seed_customer, seed_thread

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def connection(db, session_factory, org_id):
    connection_id = seed_connection(session_factory, org_id)
    return db.get(EmailConnection, connection_id)


@pytest.fixture()
def customer(db, session_factory, org_id):
    customer_id = seed_customer(session_factory, org_id)
    return db.get(Customer, customer_id)


@pytest.fixture()
def thread(db, session_factory, org_id, customer):
    thread_id = seed_thread(session_factory, org_id, customer_id=customer.id, customer_email=customer.email)
    return db.get(EmailThread, thread_id)


@pytest.fixture()
def provider():
    return FakeEmailProvider()


def _outbound(classification=EmailClassification.TRANSACTIONAL):
    return send_service.OutboundEmail(
        subject="Your order shipped",
        text_body="hello",
        html_body="<p>hello</p>",
        classification=classification,
    )


def test_transactional_send_succeeds_and_is_not_gated_by_marketing_consent(
    db, org_id, connection, customer, thread, provider
):
    assert customer.email_marketing_consent is None  # unknown, never asked
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(),
        idempotency_key="k1",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.SENT
    assert row.provider_message_id is not None


def test_marketing_send_blocked_without_consent(db, org_id, connection, customer, thread, provider):
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(EmailClassification.MARKETING),
        idempotency_key="k2",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == EmailErrorCode.OPTED_OUT.value


def test_marketing_send_succeeds_once_consent_is_recorded(
    db, org_id, connection, customer, thread, provider
):
    consent_service.set_marketing_consent(customer, consent=True)
    db.commit()
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(EmailClassification.MARKETING),
        idempotency_key="k3",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.SENT
    assert "Unsubscribe:" in row.body_text  # marketing email always carries a working opt-out link


def test_transactional_send_never_gets_an_unsubscribe_footer(
    db, org_id, connection, customer, thread, provider
):
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(EmailClassification.TRANSACTIONAL),
        idempotency_key="k3b",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.SENT
    assert "Unsubscribe:" not in row.body_text


def test_marketing_send_is_blocked_when_no_unsubscribe_link_is_configured(
    db, org_id, connection, customer, thread, provider, monkeypatch
):
    consent_service.set_marketing_consent(customer, consent=True)
    db.commit()
    unconfigured = get_settings().model_copy(update={"email_unsubscribe_base_url": ""})
    monkeypatch.setattr(send_service, "get_settings", lambda: unconfigured)

    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(EmailClassification.MARKETING),
        idempotency_key="k3c",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == EmailErrorCode.POLICY_BLOCKED.value


def test_suppression_blocks_even_a_transactional_send(
    db, org_id, connection, customer, thread, provider
):
    """Suppression is a universal kill switch -- unlike marketing consent, it also blocks
    transactional/lifecycle email (brief section 20)."""
    consent_service.suppress(customer, reason="bounced", now=NOW)
    db.commit()
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(EmailClassification.TRANSACTIONAL),
        idempotency_key="k4",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == EmailErrorCode.SUPPRESSED.value


def test_send_blocked_when_connection_is_disabled(db, org_id, session_factory, customer, thread, provider):
    from tests.email.conftest import seed_connection as _seed

    connection_id = _seed(session_factory, org_id, enabled=False)
    connection = db.get(EmailConnection, connection_id)
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(),
        idempotency_key="k5",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == EmailErrorCode.UNCONFIGURED.value


def test_send_blocked_once_the_organization_rate_limit_is_exceeded(
    db, org_id, connection, customer, thread, provider
):
    settings = get_settings()
    key = f"email-send:{org_id}"
    for _ in range(settings.email_send_rate_limit):
        assert limiter.allow(key, settings.email_send_rate_limit, settings.email_send_rate_window_seconds)
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(),
        idempotency_key="k6",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == EmailErrorCode.RATE_LIMITED.value
    assert row.provider_message_id is None


def test_repeated_idempotency_key_never_calls_the_provider_twice(
    db, org_id, connection, customer, thread, provider
):
    first = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(),
        idempotency_key="same-key",
        provider=provider,
        now=NOW,
    )
    second = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        outbound=_outbound(),
        idempotency_key="same-key",
        provider=provider,
        now=NOW,
    )
    assert first.id == second.id
    assert first.provider_message_id == second.provider_message_id


def test_missing_recipient_address_fails_as_invalid_recipient(
    db, org_id, connection, thread, provider
):
    no_email_customer = Customer(organization_id=org_id, email=None)
    db.add(no_email_customer)
    db.commit()
    row = send_service.send_email(
        db,
        organization_id=org_id,
        connection=connection,
        customer=no_email_customer,
        thread=thread,
        outbound=_outbound(),
        idempotency_key="k7",
        provider=provider,
        now=NOW,
    )
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == EmailErrorCode.INVALID_RECIPIENT.value


def test_sanitize_header_strips_crlf_to_prevent_header_injection():
    raw = "Subject line\r\nBcc: attacker@evil.example"
    cleaned = send_service.sanitize_header(raw, max_len=998)
    assert "\r" not in cleaned
    assert "\n" not in cleaned
    assert "Bcc:" in cleaned  # the injection attempt is neutralized, not silently dropped either


def test_cancel_orphaned_sending_marks_stuck_rows_failed(db, org_id, connection, customer, thread):
    import uuid as _uuid

    from sqlalchemy import update

    from app.db.models import EmailDirection, EmailMessage

    row = EmailMessage(
        id=_uuid.uuid4(),
        organization_id=org_id,
        thread_id=thread.id,
        direction=EmailDirection.OUTBOUND,
        status=EmailMessageStatus.SENDING,
        idempotency_key="stuck",
        from_address=connection.from_address,
        to_addresses=[customer.email],
        subject="x",
    )
    db.add(row)
    db.commit()
    # backdate created_at so it looks stale without sleeping in the test
    db.execute(
        update(EmailMessage).where(EmailMessage.id == row.id).values(created_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    db.commit()

    count = send_service.cancel_orphaned_sending(db, stale_after_seconds=1)
    db.refresh(row)
    assert count == 1
    assert row.status == EmailMessageStatus.FAILED
    assert row.error_code == "worker_lost"
