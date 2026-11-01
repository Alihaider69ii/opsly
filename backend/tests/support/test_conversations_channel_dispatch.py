"""The unified take_over/return_to_ai/request_human actions dispatch per channel (brief: WhatsApp
and Email each stay authoritative on their own channel table, Website uses SupportConversation
directly -- see app/services/support/conversations.py module docstring). Website's own path is
already covered by tests/support/test_conversations.py; this file proves the WHATSAPP and EMAIL
dispatch branches actually reach (and correctly reflect) the real underlying channel state
machines, not just a mocked stand-in.
"""

from datetime import UTC, datetime

from app.core.secrets import SecretManager
from app.db.models import (
    AIHandoffState,
    SenderType,
    SupportChannel,
    WhatsAppAIState,
    WhatsAppConnection,
)
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from app.services.support import conversations as support_service
from app.workers.queue import InMemoryJobQueue
from tests.email.conftest import seed_connection as seed_email_connection
from tests.email.conftest import seed_customer as seed_email_customer
from tests.email.conftest import seed_thread as seed_email_thread
from tests.whatsapp.conftest import seed_connection as seed_whatsapp_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _sync(db, org_id, *, channel, channel_conversation_id, ai_state):
    return support_service.sync_conversation(
        db,
        organization_id=org_id,
        channel=channel,
        channel_conversation_id=channel_conversation_id,
        ai_state=ai_state,
        direction="inbound",
        sender_type=SenderType.CUSTOMER,
        event_type="whatsapp_inbound" if channel == SupportChannel.WHATSAPP else "email_inbound",
        now=NOW,
    )


def _whatsapp_setup(db, org_id, session_factory, app_settings):
    secrets = SecretManager.from_settings(app_settings)
    connection_id = seed_whatsapp_connection(session_factory, org_id, secrets)
    connection = db.get(WhatsAppConnection, connection_id)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    wa_conversation = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
    )
    db.commit()
    conversation = _sync(
        db, org_id, channel=SupportChannel.WHATSAPP, channel_conversation_id=wa_conversation.id,
        ai_state=AIHandoffState.AI_ACTIVE,
    )
    db.commit()
    return conversation, wa_conversation


def _email_setup(db, org_id, session_factory):
    seed_email_connection(session_factory, org_id)
    customer_id = seed_email_customer(session_factory, org_id)
    thread_id = seed_email_thread(session_factory, org_id, customer_id=customer_id)
    from app.db.models import EmailThread

    thread = db.get(EmailThread, thread_id)
    conversation = _sync(
        db, org_id, channel=SupportChannel.EMAIL, channel_conversation_id=thread.id,
        ai_state=AIHandoffState.AI_ACTIVE,
    )
    db.commit()
    return conversation, thread


# --- take_over -------------------------------------------------------------------------------------


def test_take_over_for_whatsapp_channel_moves_the_real_whatsapp_conversation(
    db, org_id, session_factory, app_settings
):
    conversation, wa_conversation = _whatsapp_setup(db, org_id, session_factory, app_settings)
    queue = InMemoryJobQueue()
    from tests.support.conftest import make_member

    agent_id = make_member(session_factory, org_id)

    ok = support_service.take_over(db, queue, conversation=conversation, by_user_id=agent_id, now=NOW)

    assert ok is True
    db.refresh(wa_conversation)
    assert wa_conversation.ai_state == WhatsAppAIState.HUMAN_ACTIVE
    assert conversation.ai_state == AIHandoffState.HUMAN_ACTIVE
    assert conversation.status.value == "HUMAN_ACTIVE"


def test_take_over_for_whatsapp_channel_is_a_no_op_once_already_human_active(
    db, org_id, session_factory, app_settings
):
    conversation, wa_conversation = _whatsapp_setup(db, org_id, session_factory, app_settings)
    queue = InMemoryJobQueue()
    from tests.support.conftest import make_member

    agent_id = make_member(session_factory, org_id)
    first = support_service.take_over(db, queue, conversation=conversation, by_user_id=agent_id, now=NOW)
    db.commit()

    second = support_service.take_over(db, queue, conversation=conversation, by_user_id=agent_id, now=NOW)

    assert first is True
    assert second is False


def test_take_over_for_email_channel_moves_the_real_email_thread(db, org_id, session_factory):
    conversation, thread = _email_setup(db, org_id, session_factory)
    queue = InMemoryJobQueue()
    from tests.support.conftest import make_member

    agent_id = make_member(session_factory, org_id)

    ok = support_service.take_over(db, queue, conversation=conversation, by_user_id=agent_id, now=NOW)

    assert ok is True
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.HUMAN_ACTIVE
    assert conversation.ai_state == AIHandoffState.HUMAN_ACTIVE


# --- return_to_ai ----------------------------------------------------------------------------------


def test_return_to_ai_for_whatsapp_channel_resumes_the_real_whatsapp_conversation(
    db, org_id, session_factory, app_settings
):
    conversation, wa_conversation = _whatsapp_setup(db, org_id, session_factory, app_settings)
    queue = InMemoryJobQueue()
    from tests.support.conftest import make_member

    agent_id = make_member(session_factory, org_id)
    support_service.take_over(db, queue, conversation=conversation, by_user_id=agent_id, now=NOW)
    db.commit()

    ok = support_service.return_to_ai(db, queue, conversation=conversation, now=NOW)

    assert ok is True
    db.refresh(wa_conversation)
    assert wa_conversation.ai_state == WhatsAppAIState.AI_ACTIVE
    assert conversation.ai_state == AIHandoffState.AI_ACTIVE


def test_return_to_ai_for_email_channel_resumes_the_real_email_thread(db, org_id, session_factory):
    conversation, thread = _email_setup(db, org_id, session_factory)
    queue = InMemoryJobQueue()
    from tests.support.conftest import make_member

    agent_id = make_member(session_factory, org_id)
    support_service.take_over(db, queue, conversation=conversation, by_user_id=agent_id, now=NOW)
    db.commit()

    ok = support_service.return_to_ai(db, queue, conversation=conversation, now=NOW)

    assert ok is True
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.AI_ACTIVE


# --- request_human ---------------------------------------------------------------------------------


def test_request_human_for_whatsapp_channel_flags_the_real_whatsapp_conversation(
    db, org_id, session_factory, app_settings
):
    conversation, wa_conversation = _whatsapp_setup(db, org_id, session_factory, app_settings)
    queue = InMemoryJobQueue()

    ok = support_service.request_human(
        db, queue, conversation=conversation, reason="customer_requested_human", now=NOW
    )

    assert ok is True
    db.refresh(wa_conversation)
    assert wa_conversation.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert conversation.ai_state == AIHandoffState.HUMAN_REQUIRED


def test_request_human_for_email_channel_flags_the_real_email_thread(db, org_id, session_factory):
    conversation, thread = _email_setup(db, org_id, session_factory)
    queue = InMemoryJobQueue()

    ok = support_service.request_human(
        db, queue, conversation=conversation, reason="customer_requested_human", now=NOW
    )

    assert ok is True
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert conversation.ai_state == AIHandoffState.HUMAN_REQUIRED
