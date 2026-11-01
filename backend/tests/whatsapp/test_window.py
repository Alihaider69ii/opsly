from datetime import UTC, datetime, timedelta

from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _setup(db, session_factory, org_id, secrets_manager):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    conversation = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection_id
    )
    db.commit()
    return conversation


def test_no_inbound_message_ever_means_window_is_closed(
    db, session_factory, org_id, secrets_manager
):
    conversation = _setup(db, session_factory, org_id, secrets_manager)
    assert conversation.last_inbound_at is None
    assert window_service.is_window_open(conversation, now=NOW) is False


def test_inbound_message_opens_the_window_for_24_hours(
    db, session_factory, org_id, secrets_manager
):
    conversation = _setup(db, session_factory, org_id, secrets_manager)
    window_service.record_inbound(conversation, now=NOW, window_hours=24)
    db.commit()
    assert (
        window_service.is_window_open(conversation, now=NOW + timedelta(hours=23, minutes=59))
        is True
    )


def test_window_closes_exactly_at_the_boundary(db, session_factory, org_id, secrets_manager):
    conversation = _setup(db, session_factory, org_id, secrets_manager)
    window_service.record_inbound(conversation, now=NOW, window_hours=24)
    db.commit()
    assert (
        window_service.is_window_open(conversation, now=NOW + timedelta(hours=24, seconds=1))
        is False
    )
    # Exactly at the boundary instant the window has just expired (strict ">" at the open check).
    assert window_service.is_window_open(conversation, now=NOW + timedelta(hours=24)) is False


def test_a_second_inbound_message_resets_the_window(db, session_factory, org_id, secrets_manager):
    conversation = _setup(db, session_factory, org_id, secrets_manager)
    window_service.record_inbound(conversation, now=NOW, window_hours=24)
    later = NOW + timedelta(hours=23)
    window_service.record_inbound(conversation, now=later, window_hours=24)
    db.commit()
    # The window now extends 24h from the SECOND message, past where the first would have expired.
    assert (
        window_service.is_window_open(conversation, now=NOW + timedelta(hours=24, minutes=30))
        is True
    )


def test_a_new_window_resets_the_ai_reply_counter(db, session_factory, org_id, secrets_manager):
    """Phase 8: ai_replies_in_window is the AI auto-reply loop/cost control
    (services/messaging/whatsapp/autoreply.py). It must reset when a genuinely NEW window opens,
    but NOT when an inbound message merely refreshes an already-open window."""
    conversation = _setup(db, session_factory, org_id, secrets_manager)
    window_service.record_inbound(conversation, now=NOW, window_hours=24)
    conversation.ai_replies_in_window = 5
    db.commit()

    # Still within the first window: refreshing it must not reset the counter.
    window_service.record_inbound(conversation, now=NOW + timedelta(hours=1), window_hours=24)
    db.commit()
    assert conversation.ai_replies_in_window == 5

    # The window has now expired; the next inbound message opens a genuinely new one.
    much_later = NOW + timedelta(hours=48)
    window_service.record_inbound(conversation, now=much_later, window_hours=24)
    db.commit()
    assert conversation.ai_replies_in_window == 0


def test_message_count_increments_on_both_directions(db, session_factory, org_id, secrets_manager):
    conversation = _setup(db, session_factory, org_id, secrets_manager)
    assert conversation.message_count == 0
    window_service.record_inbound(conversation, now=NOW, window_hours=24)
    window_service.record_outbound(conversation, now=NOW)
    db.commit()
    assert conversation.message_count == 2


def test_get_or_create_conversation_is_idempotent(db, session_factory, org_id, secrets_manager):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155559999")
    db.commit()
    first = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection_id
    )
    db.commit()
    second = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection_id
    )
    assert first.id == second.id
