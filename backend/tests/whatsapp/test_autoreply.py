import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    WhatsAppAIConfidence,
    WhatsAppAIState,
    WhatsAppConnection,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
)
from app.services.messaging.providers.fake import FakeMessagingProvider
from app.services.messaging.whatsapp import autoreply
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from tests.chat.helpers import RecordingLLM, SpyEmbedder, StubSearch, make_hit
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SHIPPING = make_hit(
    "Orders ship within two business days. Shipping is free over 50 dollars.", score=0.9
)


@pytest.fixture()
def connection(db, session_factory, org_id, secrets_manager):
    connection_id = seed_connection(
        session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True
    )
    return db.get(WhatsAppConnection, connection_id)


@pytest.fixture()
def contact(db, org_id):
    row = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    return row


@pytest.fixture()
def conversation(db, org_id, contact, connection):
    conv = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
    )
    window_service.record_inbound(conv, now=NOW, window_hours=24)
    db.commit()
    return conv


@pytest.fixture()
def eligible(db, org_id, contact, conversation):
    """Opt the contact into SUPPORT so the policy gate's consent/window checks pass."""
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.SUPPORT,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="test",
        now=NOW,
    )
    db.commit()
    return conversation


def _wire(monkeypatch, *, llm=None, embedder=None, search=None):
    monkeypatch.setattr(autoreply, "get_llm_provider", lambda: llm or RecordingLLM())
    monkeypatch.setattr(autoreply, "get_embedding_provider", lambda: embedder or SpyEmbedder())
    monkeypatch.setattr(autoreply, "search_knowledge", search or StubSearch())


def _reply(
    db,
    *,
    org_id,
    connection,
    contact,
    conversation,
    settings,
    text="How long does shipping take?",
    provider=None,
):
    return autoreply.maybe_auto_reply(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        conversation=conversation,
        inbound_message_id=uuid.uuid4(),
        inbound_text=text,
        inbound_wamid=f"wamid.{uuid.uuid4()}",
        provider=provider or FakeMessagingProvider(),
        access_token="test-token",
        settings=settings,
        now=NOW,
    )


def _messages(db, org_id):
    db.expire_all()
    return list(
        db.scalars(select(WhatsAppMessage).where(WhatsAppMessage.organization_id == org_id))
    )


# --- eligibility gate short-circuits (never generate) -------------------------------------------


def test_disabled_connection_never_generates(
    db, org_id, connection, contact, conversation, whatsapp_settings, monkeypatch
):
    connection.ai_auto_reply_enabled = False
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=conversation,
        settings=whatsapp_settings,
    )
    assert outcome == "disabled"
    assert llm.calls == []


def test_suppressed_contact_never_generates(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    consent_service.suppress(
        db, organization_id=org_id, contact_id=contact.id, reason="stop", source="test", now=NOW
    )
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )
    assert outcome == "suppressed"
    assert llm.calls == []


def test_no_support_consent_never_generates(
    db, org_id, connection, contact, conversation, whatsapp_settings, monkeypatch
):
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=conversation,
        settings=whatsapp_settings,
    )
    assert outcome == "opted_out"
    assert llm.calls == []


def test_closed_window_never_generates(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = autoreply.maybe_auto_reply(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        inbound_message_id=uuid.uuid4(),
        inbound_text="hi",
        inbound_wamid=f"wamid.{uuid.uuid4()}",
        provider=FakeMessagingProvider(),
        access_token="tok",
        settings=whatsapp_settings,
        now=NOW + timedelta(hours=25),  # the 24h customer-service window has expired
    )
    assert outcome == "window_closed"
    assert llm.calls == []


def test_human_handoff_already_active_never_generates(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    eligible.ai_state = WhatsAppAIState.HUMAN_ACTIVE
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )
    assert outcome == "handoff_active:human_active"
    assert llm.calls == []


# --- explicit human request: handoff, no generation ----------------------------------------------


def test_customer_requesting_a_human_triggers_handoff_without_generating(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
        text="I want to talk to a human",
    )
    assert outcome == "handoff_sent"
    assert llm.calls == []  # no generation at all
    db.refresh(eligible)
    assert eligible.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert eligible.handoff_reason == "customer_requested_human"
    (sent,) = [m for m in _messages(db, org_id) if m.direction == WhatsAppMessageDirection.OUTBOUND]
    assert sent.content == whatsapp_settings.whatsapp_ai_handoff_message
    assert sent.safe_metadata["policy"] == {
        "outcome": "handoff",
        "reason": "customer_requested_human",
    }


# --- max replies per window -----------------------------------------------------------------------


def test_max_replies_reached_with_handoff_enabled_sends_handoff_once(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    connection.ai_max_replies_per_window = 1
    eligible.ai_replies_in_window = 1  # already at the cap
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )
    assert outcome == "handoff_sent"
    assert llm.calls == []
    db.refresh(eligible)
    assert eligible.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert eligible.handoff_reason == "max_replies_reached"


def test_max_replies_reached_with_handoff_disabled_sends_nothing(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    connection.ai_max_replies_per_window = 1
    connection.ai_handoff_enabled = False
    eligible.ai_replies_in_window = 1
    db.commit()
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )
    assert outcome == "max_replies_reached"
    assert llm.calls == []
    assert _messages(db, org_id) == []
    db.refresh(eligible)
    assert eligible.ai_state == WhatsAppAIState.AI_ACTIVE  # never escalated when handoff is off


# --- grounded answers are sent, with audit metadata attached -------------------------------------


def test_grounded_answer_is_sent_and_resets_the_fallback_counter(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    eligible.ai_consecutive_fallbacks = 2  # anything other than 0 or 1, see mutation/catalog_8.py
    db.commit()
    llm = RecordingLLM(["Orders ship within two business days, and shipping is free over $50."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [SHIPPING]}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )

    assert outcome == "sent"
    (sent,) = [m for m in _messages(db, org_id) if m.direction == WhatsAppMessageDirection.OUTBOUND]
    assert sent.content == "Orders ship within two business days, and shipping is free over $50."
    assert sent.safe_metadata["ai"]["grounded"] is True
    assert sent.safe_metadata["policy"] == {"outcome": "sent"}
    db.refresh(eligible)
    assert eligible.ai_consecutive_fallbacks == 0
    assert eligible.ai_replies_in_window == 1


# --- ungrounded / low-confidence: fallback, then handoff after repeated fallbacks ----------------


def test_no_relevant_knowledge_sends_the_fallback_message(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    llm = RecordingLLM(["I would have made this up."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: []}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
        text="Do you sell motorcycles?",
    )

    assert outcome == "fallback_sent:no_knowledge"
    assert llm.calls == []  # ungrounded fallback never consults the model at all
    (sent,) = [m for m in _messages(db, org_id) if m.direction == WhatsAppMessageDirection.OUTBOUND]
    assert sent.content == whatsapp_settings.whatsapp_ai_fallback_message
    db.refresh(eligible)
    assert eligible.ai_consecutive_fallbacks == 1


def test_repeated_fallbacks_trigger_handoff_instead_of_a_third_fallback(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    eligible.ai_consecutive_fallbacks = whatsapp_settings.whatsapp_ai_max_consecutive_fallbacks - 1
    db.commit()
    _wire(monkeypatch, search=StubSearch({org_id: []}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
        text="Do you sell motorcycles?",
    )

    assert outcome == "handoff_sent"
    db.refresh(eligible)
    assert eligible.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert eligible.handoff_reason == "repeated_fallback"


def test_low_confidence_below_the_connections_minimum_falls_back(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    connection.ai_min_confidence = WhatsAppAIConfidence.HIGH
    db.commit()
    weak_hit = make_hit(
        "Barely related shipping note.", score=whatsapp_settings.rag_similarity_threshold + 0.01
    )
    llm = RecordingLLM(["Here's a weak answer."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [weak_hit]}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )

    assert outcome == "fallback_sent:low_confidence"
    (sent,) = [m for m in _messages(db, org_id) if m.direction == WhatsAppMessageDirection.OUTBOUND]
    assert sent.content == whatsapp_settings.whatsapp_ai_fallback_message


def test_grounding_not_required_still_sends_a_weak_answer(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    """When an organization has explicitly turned grounding off, a low-confidence (but
    sufficiently confident per THEIR min_confidence) answer may still be sent."""
    connection.ai_grounding_required = False
    connection.ai_min_confidence = WhatsAppAIConfidence.LOW
    db.commit()
    weak_hit = make_hit(
        "Barely related shipping note.", score=whatsapp_settings.rag_similarity_threshold + 0.01
    )
    llm = RecordingLLM(["Here's a weak but allowed answer."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [weak_hit]}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )

    assert outcome == "sent"


def test_grounding_not_required_calls_the_llm_even_with_zero_retrieved_knowledge(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    """The real effect of turning grounding off: with NO retrieval hits at all (not just a weak
    one), the website-chat-style hard "never call the LLM without retrieved knowledge" guarantee
    is relaxed, and the model's own (still fact-constrained by the system prompt) answer is sent
    -- provided the organization also accepts NONE-level confidence. Proves this setting is not
    dead code: with grounding required (the default), the exact same inputs produce a fallback
    instead (see test_no_relevant_knowledge_sends_the_fallback_message)."""
    connection.ai_grounding_required = False
    connection.ai_min_confidence = WhatsAppAIConfidence.NONE
    db.commit()
    llm = RecordingLLM(["I'm not sure, but I'd be glad to help however I can."])
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: []}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
        text="Do you offer gift wrapping?",
    )

    assert outcome == "sent"
    assert len(llm.calls) == 1  # the model WAS consulted despite zero retrieved knowledge
    (sent,) = [m for m in _messages(db, org_id) if m.direction == WhatsAppMessageDirection.OUTBOUND]
    assert sent.content == "I'm not sure, but I'd be glad to help however I can."
    assert sent.safe_metadata["ai"]["grounded"] is False


# --- provider failure --------------------------------------------------------------------------


def test_llm_failure_is_reported_and_sends_nothing(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    from app.services.ai.provider import LLMError

    llm = RecordingLLM(fail_with=LLMError("unavailable", "boom"))
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [SHIPPING]}))

    outcome = _reply(
        db,
        org_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        settings=whatsapp_settings,
    )

    assert outcome == "generation_failed:llm_unavailable"
    assert _messages(db, org_id) == []


# --- loop / duplicate protection: the idempotency key is derived from the inbound wamid ----------
#
# In production a duplicate inbound delivery never reaches this function twice: the webhook
# ledger's unique dedupe_key rejects a resubmitted delivery outright, and even a crash-recovery
# re-run of the SAME stored event hits the inbound WhatsAppMessage's own idempotency_key
# (IntegrityError) and returns before ever calling maybe_auto_reply a second time -- see
# services/messaging/whatsapp/webhooks.py:_process_message. Calling this function directly twice
# with the same wamid (as this test does) is therefore a stricter check than the real pipeline
# ever performs: it proves the SEND side is idempotent (one outbound message, never two) even
# without that outer protection, while a second, avoidable AI generation call is an accepted,
# documented cost of that extra safety margin -- not a second idempotency system.


def test_two_calls_for_the_same_inbound_message_never_send_twice(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    llm = RecordingLLM(
        ["Orders ship within two business days.", "Orders ship within two business days."]
    )
    _wire(monkeypatch, llm=llm, search=StubSearch({org_id: [SHIPPING]}))
    wamid = f"wamid.{uuid.uuid4()}"

    def once():
        return autoreply.maybe_auto_reply(
            db,
            organization_id=org_id,
            connection=connection,
            contact=contact,
            conversation=eligible,
            inbound_message_id=uuid.uuid4(),
            inbound_text="How long does shipping take?",
            inbound_wamid=wamid,
            provider=FakeMessagingProvider(),
            access_token="tok",
            settings=whatsapp_settings,
            now=NOW,
        )

    first = once()
    second = once()
    assert first == "sent"
    assert second == "sent"  # idempotent replay, not a second send
    assert len(llm.calls) == 1  # AI generation was not re-run on already-sent message
    outbound = [
        m for m in _messages(db, org_id) if m.direction == WhatsAppMessageDirection.OUTBOUND
    ]
    assert len(outbound) == 1  # exactly one message ever reaches the customer


def test_hindi_hinglish_customer_requesting_a_human_triggers_handoff(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    llm = RecordingLLM()
    _wire(monkeypatch, llm=llm)
    for text in [
        "mujhe agent se baat karni hai",
        "kisi person se baat karni hai",
        "insaan se baat karni hai",
        "customer care se baat karni hai",
        "aadmi se baat karni hai",
        "human se baat karni hai",
        "agent chahiye",
        "बात करनी है",
    ]:
        eligible.ai_state = WhatsAppAIState.AI_ACTIVE
        db.commit()
        outcome = _reply(
            db,
            org_id=org_id,
            connection=connection,
            contact=contact,
            conversation=eligible,
            settings=whatsapp_settings,
            text=text,
        )
        assert outcome == "handoff_sent", f"Failed for text: {text}"
    assert llm.calls == []  # no generation for any handoff trigger


def test_recovering_queued_reply_completes_send_without_generating_again(
    db, org_id, connection, contact, eligible, whatsapp_settings, monkeypatch
):
    """Crash recovery: if a reply was already generated and saved in QUEUED state,
    re-running maybe_auto_reply dispatches that exact row without calling the LLM again.
    """
    wamid = f"wamid.{uuid.uuid4()}"
    queued = WhatsAppMessage(
        id=uuid.uuid4(),
        organization_id=org_id,
        conversation_id=eligible.id,
        direction=WhatsAppMessageDirection.OUTBOUND,
        message_type=WhatsAppMessageType.TEXT,
        status=WhatsAppMessageStatus.QUEUED,
        idempotency_key=f"autoreply:{wamid}",
        content="Persisted durable reply",
        safe_metadata={"ai": {"grounded": True}},
    )
    db.add(queued)
    db.commit()

    llm = RecordingLLM(["Should not be called"])
    _wire(monkeypatch, llm=llm)

    outcome = autoreply.maybe_auto_reply(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        conversation=eligible,
        inbound_message_id=uuid.uuid4(),
        inbound_text="question",
        inbound_wamid=wamid,
        provider=FakeMessagingProvider(),
        access_token="tok",
        settings=whatsapp_settings,
        now=NOW,
    )
    assert outcome == "sent"
    assert len(llm.calls) == 0  # No LLM call
    db.refresh(queued)
    assert queued.status == WhatsAppMessageStatus.SENT
    assert queued.content == "Persisted durable reply"
