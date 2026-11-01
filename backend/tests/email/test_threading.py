import uuid
from datetime import UTC, datetime

from app.db.models import EmailDirection, EmailMessage, EmailMessageStatus
from app.services.email import threading as threading_service
from tests.email.conftest import seed_customer

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _store_message(db, org_id, thread_id, *, message_id_header: str):
    msg = EmailMessage(
        id=uuid.uuid4(),
        organization_id=org_id,
        thread_id=thread_id,
        direction=EmailDirection.OUTBOUND,
        status=EmailMessageStatus.SENT,
        idempotency_key=str(uuid.uuid4()),
        from_address="support@acme.example",
        to_addresses=["sam@example.com"],
        message_id_header=message_id_header,
    )
    db.add(msg)
    db.flush()
    return msg


def test_parse_references_splits_whitespace_separated_tokens():
    assert threading_service.parse_references("<a@x> <b@x>  <c@x>") == ["<a@x>", "<b@x>", "<c@x>"]
    assert threading_service.parse_references(None) == []
    assert threading_service.parse_references("") == []


def test_parse_references_bounds_a_pathologically_long_header():
    header = " ".join(f"<{i}@x>" for i in range(1000))
    assert len(threading_service.parse_references(header)) == threading_service.MAX_REFERENCES


def test_a_new_inbound_with_no_match_starts_a_new_thread(db, org_id):
    thread = threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="Help",
        provider_thread_id=None,
        in_reply_to_header=None,
        references_header=None,
        now=NOW,
    )
    assert thread.id is not None
    assert thread.customer_email == "sam@example.com"


def test_provider_thread_id_once_seen_always_wins(db, org_id):
    first = threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="Help",
        provider_thread_id="ptid-1",
        in_reply_to_header=None,
        references_header=None,
        now=NOW,
    )
    db.commit()

    second = threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="A totally different subject",
        provider_thread_id="ptid-1",
        in_reply_to_header=None,
        references_header=None,
        now=NOW,
    )
    assert second.id == first.id


def test_reply_matched_by_in_reply_to_header_joins_the_original_thread(db, org_id, session_factory):
    customer_id = seed_customer(session_factory, org_id)
    original = threading_service.get_or_create_outbound_thread(
        db,
        organization_id=org_id,
        customer_id=customer_id,
        customer_email="sam@example.com",
        subject="Order #123",
    )
    db.flush()
    _store_message(db, org_id, original.id, message_id_header="<msg-1@acme.example>")
    db.commit()

    reply_thread = threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="Re: Order #123",
        provider_thread_id=None,
        in_reply_to_header="<msg-1@acme.example>",
        references_header=None,
        now=NOW,
    )
    assert reply_thread.id == original.id


def test_shared_sender_address_alone_never_merges_two_unrelated_threads(db, org_id):
    thread_a = threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="Order A",
        provider_thread_id=None,
        in_reply_to_header=None,
        references_header=None,
        now=NOW,
    )
    db.commit()
    thread_b = threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="Order B",
        provider_thread_id=None,
        in_reply_to_header=None,
        references_header=None,
        now=NOW,
    )
    assert thread_a.id != thread_b.id


def test_resolve_inbound_thread_never_crosses_tenants(db, org_id, session_factory):
    from app.db.models import Organization

    other_org = uuid.uuid4()
    with session_factory() as other_db:
        other_db.add(Organization(id=other_org, name="Other", slug=f"other-{other_org.hex[:8]}"))
        other_db.commit()

    threading_service.resolve_inbound_thread(
        db,
        organization_id=org_id,
        customer_id=None,
        customer_email="sam@example.com",
        subject="Help",
        provider_thread_id="shared-ptid",
        in_reply_to_header=None,
        references_header=None,
        now=NOW,
    )
    db.commit()

    with session_factory() as other_db:
        other_thread = threading_service.resolve_inbound_thread(
            other_db,
            organization_id=other_org,
            customer_id=None,
            customer_email="sam@example.com",
            subject="Help",
            provider_thread_id="shared-ptid",
            in_reply_to_header=None,
            references_header=None,
            now=NOW,
        )
        other_db.commit()
        all_threads_in_other_org = other_db.query(type(other_thread)).filter_by(organization_id=other_org).all()
        assert len(all_threads_in_other_org) == 1  # a new thread, not a cross-tenant match


def test_get_or_create_outbound_thread_reuses_an_explicit_existing_thread_id(db, org_id, session_factory):
    customer_id = seed_customer(session_factory, org_id)
    first = threading_service.get_or_create_outbound_thread(
        db, organization_id=org_id, customer_id=customer_id, customer_email="sam@example.com", subject="Hi"
    )
    db.commit()
    second = threading_service.get_or_create_outbound_thread(
        db,
        organization_id=org_id,
        customer_id=customer_id,
        customer_email="sam@example.com",
        subject="Hi",
        thread_id=first.id,
    )
    assert second.id == first.id


def test_get_or_create_outbound_thread_without_an_id_always_creates_a_new_one(db, org_id, session_factory):
    customer_id = seed_customer(session_factory, org_id)
    first = threading_service.get_or_create_outbound_thread(
        db, organization_id=org_id, customer_id=customer_id, customer_email="sam@example.com", subject="Hi"
    )
    db.commit()
    second = threading_service.get_or_create_outbound_thread(
        db, organization_id=org_id, customer_id=customer_id, customer_email="sam@example.com", subject="Hi"
    )
    assert second.id != first.id
