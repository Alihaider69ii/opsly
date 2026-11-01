import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import (
    EmailConnection,
    EmailDirection,
    EmailMessage,
    EmailMessageStatus,
    EmailThread,
    SenderType,
    WhatsAppAIConfidence,
    WhatsAppAIState,
)
from app.services.email import autoreply
from app.services.email import consent as consent_service
from app.services.email.providers.fake import FakeEmailProvider
from tests.chat.helpers import RecordingLLM, SpyEmbedder, StubSearch, make_hit
from tests.email.conftest import seed_connection, seed_customer, seed_thread

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SHIPPING = make_hit(
    "Orders ship within two business days. Shipping is free over 50 dollars.", score=0.9
)


@pytest.fixture()
def connection(db, session_factory, org_id):
    connection_id = seed_connection(session_factory, org_id, ai_auto_reply_enabled=True)
    return db.get(EmailConnection, connection_id)


@pytest.fixture()
def customer(db, session_factory, org_id):
    from app.db.models import Customer

    customer_id = seed_customer(session_factory, org_id)
    return db.get(Customer, customer_id)


@pytest.fixture()
def thread(db, session_factory, org_id, customer):
    thread_id = seed_thread(session_factory, org_id, customer_id=customer.id, customer_email=customer.email)
    return db.get(EmailThread, thread_id)


def _inbound(db, org_id, thread, text="How long does shipping take?") -> EmailMessage:
    message = EmailMessage(
        id=uuid.uuid4(),
        organization_id=org_id,
        thread_id=thread.id,
        direction=EmailDirection.INBOUND,
        status=EmailMessageStatus.RECEIVED,
        idempotency_key=f"inbound:{uuid.uuid4()}",
        from_address=thread.customer_email,
        to_addresses=["support@acme.example"],
        subject="Help",
        body_text=text,
    )
    db.add(message)
    db.commit()
    return message


def _wire(monkeypatch, *, llm=None, embedder=None, search=None):
    monkeypatch.setattr(autoreply, "get_llm_provider", lambda: llm or RecordingLLM())
    monkeypatch.setattr(autoreply, "get_embedding_provider", lambda: embedder or SpyEmbedder())
    monkeypatch.setattr(autoreply, "search_knowledge", search or StubSearch())


def _reply(db, *, org_id, connection, customer, thread, settings, text="How long does shipping take?", provider=None):
    inbound = _inbound(db, org_id, thread, text)
    return autoreply.maybe_auto_reply(
        db,
        organization_id=org_id,
        connection=connection,
        customer=customer,
        thread=thread,
        inbound_message_id=inbound.id,
        inbound_text=text,
        provider=provider or FakeEmailProvider(),
        settings=settings,
        now=NOW,
    )


def _messages(db, org_id):
    db.expire_all()
    return list(db.scalars(select(EmailMessage).where(EmailMessage.organization_id == org_id)))


# --- eligibility gate short-circuits (never generate) -------------------------------------------


def test_disabled_connection_never_generates(db, org_id, connection, customer, thread, email_settings, monkeypatch):
    connection.ai_auto_reply_enabled = False
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)
    assert outcome == "disabled"
    assert llm.calls == []


def test_suppressed_customer_never_generates(db, org_id, connection, customer, thread, email_settings, monkeypatch):
    consent_service.suppress(customer, reason="bounced", now=NOW)
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)
    assert outcome == "suppressed"
    assert llm.calls == []


def test_human_handoff_already_active_never_generates(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    thread.ai_state = WhatsAppAIState.HUMAN_ACTIVE
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)
    assert outcome == "handoff_active:human_active"
    assert llm.calls == []


# --- explicit human request: handoff, no generation ----------------------------------------------


def test_customer_requesting_a_human_triggers_handoff_without_generating(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings,
        text="I want to talk to a human",
    )
    assert outcome == "handoff_sent"
    assert llm.calls == []
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert thread.handoff_reason == "customer_requested_human"
    (sent,) = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert sent.body_text == email_settings.email_ai_handoff_message
    assert sent.safe_metadata["policy"] == {"outcome": "handoff", "reason": "customer_requested_human"}
    assert sent.sender_type == SenderType.SYSTEM  # a handoff/fallback message is never attributed to AI


# --- max replies per window -----------------------------------------------------------------------


def test_max_replies_reached_with_handoff_enabled_sends_handoff_once(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    connection.ai_max_replies_per_window = 1
    thread.ai_replies_in_window = 1
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)
    assert outcome == "handoff_sent"
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert thread.handoff_reason == "max_replies_reached"


def test_max_replies_reached_with_handoff_disabled_sends_nothing(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    connection.ai_max_replies_per_window = 1
    connection.ai_handoff_enabled = False
    thread.ai_replies_in_window = 1
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)
    assert outcome == "max_replies_reached"
    assert llm.calls == []
    outbound = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert outbound == []
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.AI_ACTIVE


# --- grounded answers are sent, with audit metadata attached -------------------------------------


def test_grounded_answer_is_sent_and_resets_the_fallback_counter(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    thread.ai_consecutive_fallbacks = 2
    db.commit()
    llm = RecordingLLM(["Orders ship within two business days, and shipping is free over $50."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [SHIPPING]}))

    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)

    assert outcome == "sent"
    (sent,) = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert sent.body_text == "Orders ship within two business days, and shipping is free over $50."
    assert sent.safe_metadata["ai"]["grounded"] is True
    assert sent.safe_metadata["policy"] == {"outcome": "sent"}
    assert sent.sender_type == SenderType.AI
    db.refresh(thread)
    assert thread.ai_consecutive_fallbacks == 0
    assert thread.ai_replies_in_window == 1


# --- ungrounded / low-confidence: fallback, then handoff after repeated fallbacks ----------------


def test_no_relevant_knowledge_sends_the_fallback_message(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    llm = RecordingLLM(["I would have made this up."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: []}))

    outcome = _reply(
        db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings,
        text="Do you sell motorcycles?",
    )

    assert outcome == "fallback_sent:no_knowledge"
    assert llm.calls == []
    (sent,) = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert sent.body_text == email_settings.email_ai_fallback_message
    db.refresh(thread)
    assert thread.ai_consecutive_fallbacks == 1


def test_repeated_fallbacks_trigger_handoff_instead_of_a_third_fallback(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    thread.ai_consecutive_fallbacks = email_settings.email_ai_max_consecutive_fallbacks - 1
    db.commit()
    _wire(monkeypatch, search=StubSearch({org_id: []}))

    outcome = _reply(
        db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings,
        text="Do you sell motorcycles?",
    )

    assert outcome == "handoff_sent"
    db.refresh(thread)
    assert thread.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert thread.handoff_reason == "repeated_fallback"


def test_low_confidence_below_the_connections_minimum_falls_back(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    connection.ai_min_confidence = WhatsAppAIConfidence.HIGH
    db.commit()
    weak_hit = make_hit("Barely related shipping note.", score=email_settings.rag_similarity_threshold + 0.01)
    llm = RecordingLLM(["Here's a weak answer."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [weak_hit]}))

    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)

    assert outcome == "fallback_sent:low_confidence"
    (sent,) = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert sent.body_text == email_settings.email_ai_fallback_message


# --- provider / generation failures ----------------------------------------------------------------


def test_llm_failure_is_reported_and_sends_nothing(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    from app.services.ai.provider import LLMError

    llm = RecordingLLM(fail_with=LLMError("unavailable", "boom"))
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [SHIPPING]}))

    outcome = _reply(db, org_id=org_id, connection=connection, customer=customer, thread=thread, settings=email_settings)

    assert outcome == "generation_failed:llm_unavailable"
    outbound = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert outbound == []  # the inbound message exists, but nothing was ever sent back


# --- loop / duplicate protection: idempotency key is derived from the inbound message id --------


def test_two_calls_for_the_same_inbound_message_never_send_twice(
    db, org_id, connection, customer, thread, email_settings, monkeypatch
):
    llm = RecordingLLM(["Orders ship within two business days.", "Orders ship within two business days."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [SHIPPING]}))
    inbound = _inbound(db, org_id, thread, "How long does shipping take?")

    def once():
        return autoreply.maybe_auto_reply(
            db,
            organization_id=org_id,
            connection=connection,
            customer=customer,
            thread=thread,
            inbound_message_id=inbound.id,
            inbound_text="How long does shipping take?",
            provider=FakeEmailProvider(),
            settings=email_settings,
            now=NOW,
        )

    first = once()
    second = once()
    assert first == "sent"
    assert second == "sent"
    assert len(llm.calls) == 1
    outbound = [m for m in _messages(db, org_id) if m.direction == EmailDirection.OUTBOUND]
    assert len(outbound) == 1
