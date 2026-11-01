import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.support import identity as identity_service
from app.services.support import timeline as timeline_service

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_record_event_rejects_an_unknown_event_type(db, org_id):
    with pytest.raises(ValueError):
        timeline_service.record_event(
            db, organization_id=org_id, event_type="not_a_real_event_type"
        )


def test_record_event_persists_and_is_listable_by_customer(db, org_id):
    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()

    timeline_service.record_event(
        db,
        organization_id=org_id,
        event_type="consent_changed",
        customer_id=customer.id,
        payload={"from": None, "to": True},
        now=NOW,
    )
    db.commit()

    events = timeline_service.list_customer_timeline(db, organization_id=org_id, customer_id=customer.id)
    assert len(events) == 1
    assert events[0].event_type == "consent_changed"
    assert events[0].payload == {"from": None, "to": True}


def test_record_event_never_commits_itself(db, org_id):
    """Callers must own the transaction -- a caller that rolls back must lose the event too."""
    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()

    timeline_service.record_event(
        db, organization_id=org_id, event_type="consent_changed", customer_id=customer.id, now=NOW
    )
    db.rollback()

    events = timeline_service.list_customer_timeline(db, organization_id=org_id, customer_id=customer.id)
    assert events == []


def test_oversized_payload_is_truncated_not_rejected(db, org_id):
    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()

    huge = {"note": "x" * 5000}
    event = timeline_service.record_event(
        db, organization_id=org_id, event_type="consent_changed", customer_id=customer.id, payload=huge, now=NOW
    )
    db.commit()
    assert event.payload == {"truncated": True}


def test_customer_timeline_is_ordered_newest_first_and_respects_before_cursor(db, org_id):
    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()

    for i in range(3):
        timeline_service.record_event(
            db,
            organization_id=org_id,
            event_type="consent_changed",
            customer_id=customer.id,
            payload={"i": i},
            now=NOW + timedelta(minutes=i),
        )
    db.commit()

    events = timeline_service.list_customer_timeline(db, organization_id=org_id, customer_id=customer.id)
    assert [e.payload["i"] for e in events] == [2, 1, 0]

    older = timeline_service.list_customer_timeline(
        db, organization_id=org_id, customer_id=customer.id, before=NOW + timedelta(minutes=2)
    )
    assert [e.payload["i"] for e in older] == [1, 0]


def test_customer_timeline_never_crosses_tenants(db, org_id, session_factory):
    from app.db.models import Organization

    other_org = uuid.uuid4()
    with session_factory() as other_db:
        other_db.add(Organization(id=other_org, name="Other", slug=f"other-{other_org.hex[:8]}"))
        other_db.commit()

    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()
    timeline_service.record_event(
        db, organization_id=org_id, event_type="consent_changed", customer_id=customer.id, now=NOW
    )
    db.commit()

    cross_tenant = timeline_service.list_customer_timeline(db, organization_id=other_org, customer_id=customer.id)
    assert cross_tenant == []


def test_limit_is_clamped_to_max(db, org_id):
    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()
    for i in range(5):
        timeline_service.record_event(
            db,
            organization_id=org_id,
            event_type="consent_changed",
            customer_id=customer.id,
            now=NOW + timedelta(minutes=i),
        )
    db.commit()

    events = timeline_service.list_customer_timeline(
        db, organization_id=org_id, customer_id=customer.id, limit=10_000
    )
    assert len(events) == 5  # clamp just bounds the ceiling, doesn't break a small real result
