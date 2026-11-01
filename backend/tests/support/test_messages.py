import uuid
from datetime import UTC, datetime

from app.core.secrets import SecretManager
from app.db.models import (
    Conversation,
    EmailDirection,
    EmailMessage,
    EmailMessageStatus,
    Message,
    MessageRole,
    SenderType,
    SupportChannel,
    SupportConversation,
    WhatsAppConnection,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
)
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from app.services.support import messages as messages_service
from tests.email.conftest import seed_connection as seed_email_connection
from tests.email.conftest import seed_customer as seed_email_customer
from tests.email.conftest import seed_thread as seed_email_thread
from tests.whatsapp.conftest import seed_connection as seed_whatsapp_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _conversation(org_id, *, channel, channel_conversation_id):
    return SupportConversation(
        id=uuid.uuid4(), organization_id=org_id, channel=channel, channel_conversation_id=channel_conversation_id
    )


# --- website --------------------------------------------------------------------------------------


def test_website_messages_are_normalized_and_system_role_is_excluded(db, org_id):
    web_conv = Conversation(id=uuid.uuid4(), organization_id=org_id, token_hash="x" * 64)
    db.add(web_conv)
    db.flush()
    db.add_all(
        [
            Message(
                organization_id=org_id, conversation_id=web_conv.id, position=1,
                role=MessageRole.USER, content="Hi", sender_type=SenderType.CUSTOMER,
            ),
            Message(
                organization_id=org_id, conversation_id=web_conv.id, position=2,
                role=MessageRole.ASSISTANT, content="Hello!", sender_type=SenderType.AI,
                meta={"ai": {"grounded": True}, "policy": {"outcome": "sent"}},
            ),
            Message(
                organization_id=org_id, conversation_id=web_conv.id, position=3,
                role=MessageRole.SYSTEM, content="internal note",
            ),
        ]
    )
    db.commit()
    conversation = _conversation(org_id, channel=SupportChannel.WEBSITE, channel_conversation_id=web_conv.id)

    result = messages_service.list_messages(db, conversation)

    assert len(result) == 2  # the SYSTEM row is never shown
    assert result[0].direction == "INBOUND" and result[0].content == "Hi"
    assert result[1].direction == "OUTBOUND" and result[1].content == "Hello!"
    assert result[1].ai == {"grounded": True}
    assert result[1].policy == {"outcome": "sent"}


def test_website_messages_are_ordered_by_position(db, org_id):
    web_conv = Conversation(id=uuid.uuid4(), organization_id=org_id, token_hash="x" * 64)
    db.add(web_conv)
    db.flush()
    db.add_all(
        [
            Message(organization_id=org_id, conversation_id=web_conv.id, position=2, role=MessageRole.ASSISTANT, content="second"),
            Message(organization_id=org_id, conversation_id=web_conv.id, position=1, role=MessageRole.USER, content="first"),
        ]
    )
    db.commit()
    conversation = _conversation(org_id, channel=SupportChannel.WEBSITE, channel_conversation_id=web_conv.id)

    result = messages_service.list_messages(db, conversation)
    assert [m.content for m in result] == ["first", "second"]


# --- whatsapp --------------------------------------------------------------------------------------


def test_whatsapp_messages_are_normalized(db, org_id, session_factory, app_settings):
    secrets = SecretManager.from_settings(app_settings)
    connection_id = seed_whatsapp_connection(session_factory, org_id, secrets)
    connection = db.get(WhatsAppConnection, connection_id)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    wa_conv = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
    )
    db.commit()
    db.add(
        WhatsAppMessage(
            organization_id=org_id, conversation_id=wa_conv.id,
            direction=WhatsAppMessageDirection.INBOUND, message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.RECEIVED, content="Hi there", idempotency_key=str(uuid.uuid4()),
            sender_type=SenderType.CUSTOMER,
        )
    )
    db.commit()
    conversation = _conversation(org_id, channel=SupportChannel.WHATSAPP, channel_conversation_id=wa_conv.id)

    result = messages_service.list_messages(db, conversation)

    assert len(result) == 1
    assert result[0].content == "Hi there"
    assert result[0].direction == "INBOUND"
    assert result[0].sender_type == SenderType.CUSTOMER


# --- email -----------------------------------------------------------------------------------------


def test_email_messages_are_normalized_and_fall_back_to_html_when_text_is_absent(
    db, org_id, session_factory
):
    seed_email_connection(session_factory, org_id)
    customer_id = seed_email_customer(session_factory, org_id)
    thread_id = seed_email_thread(session_factory, org_id, customer_id=customer_id)
    db.add(
        EmailMessage(
            organization_id=org_id, thread_id=thread_id, direction=EmailDirection.INBOUND,
            status=EmailMessageStatus.RECEIVED, idempotency_key=str(uuid.uuid4()),
            from_address="sam@example.com", to_addresses=["support@acme.example"],
            body_text=None, body_html_sanitized="<p>hello</p>", sender_type=SenderType.CUSTOMER,
        )
    )
    db.commit()
    conversation = _conversation(org_id, channel=SupportChannel.EMAIL, channel_conversation_id=thread_id)

    result = messages_service.list_messages(db, conversation)

    assert len(result) == 1
    assert result[0].content == "<p>hello</p>"
    assert result[0].direction == "INBOUND"


def test_messages_never_cross_tenants(db, org_id, session_factory):
    import uuid as _uuid

    from app.db.models import Organization

    other_org = _uuid.uuid4()
    with session_factory() as other_db:
        other_db.add(Organization(id=other_org, name="Other", slug=f"other-{other_org.hex[:8]}"))
        other_db.commit()

    seed_email_connection(session_factory, org_id)
    customer_id = seed_email_customer(session_factory, org_id)
    thread_id = seed_email_thread(session_factory, org_id, customer_id=customer_id)
    db.add(
        EmailMessage(
            organization_id=org_id, thread_id=thread_id, direction=EmailDirection.INBOUND,
            status=EmailMessageStatus.RECEIVED, idempotency_key=str(uuid.uuid4()),
            from_address="sam@example.com", to_addresses=["support@acme.example"], body_text="hi",
        )
    )
    db.commit()
    other_conversation = _conversation(other_org, channel=SupportChannel.EMAIL, channel_conversation_id=thread_id)

    result = messages_service.list_messages(db, other_conversation)
    assert result == []


def test_limit_is_respected(db, org_id):
    web_conv = Conversation(id=uuid.uuid4(), organization_id=org_id, token_hash="x" * 64)
    db.add(web_conv)
    db.flush()
    db.add_all(
        [
            Message(organization_id=org_id, conversation_id=web_conv.id, position=i, role=MessageRole.USER, content=str(i))
            for i in range(1, 6)
        ]
    )
    db.commit()
    conversation = _conversation(org_id, channel=SupportChannel.WEBSITE, channel_conversation_id=web_conv.id)

    result = messages_service.list_messages(db, conversation, limit=2)
    assert len(result) == 2
