import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import (
    WebhookEventStatus,
    WhatsAppConnection,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
    WhatsAppWebhookEvent,
)
from app.services.messaging.providers.fake import FakeMessagingProvider
from app.services.messaging.whatsapp import autoreply
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import send as send_service
from app.services.messaging.whatsapp import webhooks as wh
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import find_contact, get_or_create_contact
from app.services.messaging.whatsapp.validation import sign_meta_body, verify_handshake
from tests.chat.helpers import RecordingLLM, SpyEmbedder, StubSearch, make_hit
from tests.whatsapp.conftest import APP_SECRET, VERIFY_TOKEN, seed_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _inbound_payload(phone_number_id="PNID_TEST", wamid="wamid.ABC", frm="14155551234", text="Hi"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Jane"}, "wa_id": frm}],
                            "messages": [
                                {
                                    "from": frm,
                                    "id": wamid,
                                    "timestamp": str(int(NOW.timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _status_payload(
    wamid, status, ts, phone_number_id="PNID_TEST", recipient="14155551234", errors=None
):
    entry = {
        "id": wamid,
        "status": status,
        "timestamp": str(ts),
        "recipient_id": recipient,
    }
    if errors is not None:
        entry["errors"] = errors
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "statuses": [entry],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"x-hub-signature-256": sign_meta_body(body, APP_SECRET)}


# --- GET handshake -------------------------------------------------------------------------------


def test_get_handshake_accepts_the_correct_token():
    assert verify_handshake(mode="subscribe", token=VERIFY_TOKEN, configured_token=VERIFY_TOKEN)


def test_get_handshake_rejects_wrong_token_or_mode():
    assert not verify_handshake(mode="subscribe", token="wrong", configured_token=VERIFY_TOKEN)
    assert not verify_handshake(
        mode="unsubscribe", token=VERIFY_TOKEN, configured_token=VERIFY_TOKEN
    )
    assert not verify_handshake(mode="subscribe", token=None, configured_token=VERIFY_TOKEN)


def test_get_handshake_rejects_when_no_token_is_configured():
    assert not verify_handshake(mode="subscribe", token="anything", configured_token="")


# --- POST intake: signature, dedupe, bounds -------------------------------------------------------


def test_invalid_signature_is_rejected(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager)
    body, _ = _signed(_inbound_payload())
    with pytest.raises(wh.WebhookRejected) as excinfo:
        wh.receive_webhook(
            db,
            body=body,
            headers={"x-hub-signature-256": "sha256=deadbeef"},
            settings=whatsapp_settings,
            queue=queue,
        )
    assert excinfo.value.status_code == 401


def test_missing_signature_is_rejected(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager)
    body, _ = _signed(_inbound_payload())
    with pytest.raises(wh.WebhookRejected):
        wh.receive_webhook(db, body=body, headers={}, settings=whatsapp_settings, queue=queue)


def test_malformed_json_body_is_rejected(db, whatsapp_settings, queue):
    body = b"not json"
    headers = {"x-hub-signature-256": sign_meta_body(body, APP_SECRET)}
    with pytest.raises(wh.WebhookRejected) as excinfo:
        wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    assert excinfo.value.status_code == 400


def test_a_webhook_for_an_unknown_phone_number_is_ignored_not_stored(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager, phone_number_id="DIFFERENT_PNID")
    body, headers = _signed(_inbound_payload(phone_number_id="UNKNOWN_PNID"))
    results = wh.receive_webhook(
        db, body=body, headers=headers, settings=whatsapp_settings, queue=queue
    )
    assert results == [wh.IntakeResult("ignored")]
    assert len(queue.jobs) == 0


def test_duplicate_delivery_of_the_same_message_is_deduped(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager)
    body, headers = _signed(_inbound_payload())
    first = wh.receive_webhook(
        db, body=body, headers=headers, settings=whatsapp_settings, queue=queue
    )
    assert first[0].status == "accepted"
    second = wh.receive_webhook(
        db, body=body, headers=headers, settings=whatsapp_settings, queue=queue
    )
    assert second[0].status == "duplicate"
    # Only one job was ever enqueued.
    assert len(queue.jobs) == 1


# --- processing: inbound message ------------------------------------------------------------------


def test_inbound_message_creates_contact_conversation_message_and_automation_event(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager)
    body, headers = _signed(_inbound_payload(text="Hello there!"))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings)
    assert outcome == "processed"

    with session_factory() as fresh:
        message = fresh.query(WhatsAppMessage).filter_by(organization_id=org_id).one()
        assert message.content == "Hello there!"
        assert message.status == WhatsAppMessageStatus.RECEIVED

    contact = find_contact(db, organization_id=org_id, phone_e164="+14155551234")
    assert contact is not None
    assert contact.wa_id == "14155551234"
    assert contact.display_name == "Jane"


def test_processing_is_idempotent_for_a_resubmitted_ledger_event(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager)
    body, headers = _signed(_inbound_payload())
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    first_outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings)
    assert first_outcome == "processed"
    # Re-running the SAME already-processed ledger event is a safe no-op (claim fails: not PENDING).
    second_outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings)
    assert second_outcome == "skipped"
    with session_factory() as fresh:
        count = fresh.query(WhatsAppMessage).filter_by(organization_id=org_id).count()
        assert count == 1


# --- processing: status updates -------------------------------------------------------------------


def test_status_progression_is_applied_in_order(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    with session_factory() as fresh:
        from app.db.models import WhatsAppConnection

        connection = fresh.get(WhatsAppConnection, connection_id)
        contact_row = get_or_create_contact(
            fresh, organization_id=org_id, phone_e164="+14155551234"
        )
        fresh.commit()
        consent_service.record_consent(
            fresh,
            organization_id=org_id,
            contact_id=contact_row.id,
            category=WhatsAppConsentCategory.SUPPORT,
            status=WhatsAppConsentStatus.OPTED_IN,
            source="test",
            now=NOW,
        )
        conversation = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact_row.id, connection_id=connection.id
        )
        window_service.record_inbound(conversation, now=NOW, window_hours=24)
        fresh.commit()
        row = send_service.send_text(
            fresh,
            organization_id=org_id,
            connection=connection,
            contact=contact_row,
            outbound=send_service.OutboundText(
                content="hi", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key="exec:0",
            provider=FakeMessagingProvider(),
            access_token="test-token",
            now=NOW,
        )
        wamid = row.provider_message_id

    base_ts = int(NOW.timestamp())
    for status, ts in (("delivered", base_ts + 1), ("read", base_ts + 2)):
        body, headers = _signed(_status_payload(wamid, status, ts))
        wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    while (job := queue.dequeue()) is not None:
        wh.process_event(session_factory, job.event_id, whatsapp_settings)

    with session_factory() as fresh:
        updated = fresh.get(WhatsAppMessage, row.id)
        assert updated.status == WhatsAppMessageStatus.READ
        assert updated.delivered_at is not None
        assert updated.read_at is not None

    # An out-of-order "delivered" at an earlier timestamp than already-applied "read" must not
    # regress the message, even though it is a genuinely new (not deduped) ledger event.
    stale_body, stale_headers = _signed(_status_payload(wamid, "delivered", base_ts + 1 + 500))
    wh.receive_webhook(
        db, body=stale_body, headers=stale_headers, settings=whatsapp_settings, queue=queue
    )
    job = queue.dequeue()
    wh.process_event(session_factory, job.event_id, whatsapp_settings)
    with session_factory() as fresh:
        updated = fresh.get(WhatsAppMessage, row.id)
        assert updated.status == WhatsAppMessageStatus.READ


def test_failed_status_captures_metas_error_code_for_debugging(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    """A 'failed' status webhook carries Meta's own error code (e.g. 131026 'recipient not a
    WhatsApp user', per https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes)
    -- it must be preserved for operator debugging, not collapsed into a generic placeholder."""
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    with session_factory() as fresh:
        from app.db.models import WhatsAppConnection

        connection = fresh.get(WhatsAppConnection, connection_id)
        contact_row = get_or_create_contact(
            fresh, organization_id=org_id, phone_e164="+14155551234"
        )
        fresh.commit()
        consent_service.record_consent(
            fresh,
            organization_id=org_id,
            contact_id=contact_row.id,
            category=WhatsAppConsentCategory.SUPPORT,
            status=WhatsAppConsentStatus.OPTED_IN,
            source="test",
            now=NOW,
        )
        conversation = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact_row.id, connection_id=connection.id
        )
        window_service.record_inbound(conversation, now=NOW, window_hours=24)
        fresh.commit()
        row = send_service.send_text(
            fresh,
            organization_id=org_id,
            connection=connection,
            contact=contact_row,
            outbound=send_service.OutboundText(
                content="hi", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key="exec:failed-0",
            provider=FakeMessagingProvider(),
            access_token="test-token",
            now=NOW,
        )
        wamid = row.provider_message_id

    base_ts = int(NOW.timestamp())
    errors = [{"code": 131026, "title": "not a WhatsApp user", "message": "details"}]
    body, headers = _signed(_status_payload(wamid, "failed", base_ts + 1, errors=errors))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    wh.process_event(session_factory, job.event_id, whatsapp_settings)

    with session_factory() as fresh:
        updated = fresh.get(WhatsAppMessage, row.id)
        assert updated.status == WhatsAppMessageStatus.FAILED
        assert updated.error_code == "meta_131026"


def test_failed_status_with_no_error_code_falls_back_to_a_generic_placeholder(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    with session_factory() as fresh:
        from app.db.models import WhatsAppConnection

        connection = fresh.get(WhatsAppConnection, connection_id)
        contact_row = get_or_create_contact(
            fresh, organization_id=org_id, phone_e164="+14155551234"
        )
        fresh.commit()
        consent_service.record_consent(
            fresh,
            organization_id=org_id,
            contact_id=contact_row.id,
            category=WhatsAppConsentCategory.SUPPORT,
            status=WhatsAppConsentStatus.OPTED_IN,
            source="test",
            now=NOW,
        )
        conversation = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact_row.id, connection_id=connection.id
        )
        window_service.record_inbound(conversation, now=NOW, window_hours=24)
        fresh.commit()
        row = send_service.send_text(
            fresh,
            organization_id=org_id,
            connection=connection,
            contact=contact_row,
            outbound=send_service.OutboundText(
                content="hi", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key="exec:failed-1",
            provider=FakeMessagingProvider(),
            access_token="test-token",
            now=NOW,
        )
        wamid = row.provider_message_id

    base_ts = int(NOW.timestamp())
    body, headers = _signed(_status_payload(wamid, "failed", base_ts + 1))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    wh.process_event(session_factory, job.event_id, whatsapp_settings)

    with session_factory() as fresh:
        updated = fresh.get(WhatsAppMessage, row.id)
        assert updated.status == WhatsAppMessageStatus.FAILED
        assert updated.error_code == "provider_reported_failure"


def test_status_for_an_unmatched_message_is_retried_then_eventually_failed(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    seed_connection(session_factory, org_id, secrets_manager)
    body, headers = _signed(_status_payload("wamid.NEVER-SENT", "sent", int(NOW.timestamp())))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings)
    assert outcome == "retry"


# --- AI auto-reply end-to-end (Phase 8) ------------------------------------------------------------


def _opt_in_support(db, org_id, phone_e164="+14155551234", now=NOW):
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164=phone_e164)
    db.commit()
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.SUPPORT,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="test",
        now=now,
    )
    db.commit()


def test_ai_auto_reply_sends_a_grounded_response_end_to_end(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings, monkeypatch
):
    """The full real pipeline: a signed webhook -> intake -> worker processing -> AI auto-reply ->
    an outbound WhatsAppMessage with Phase 8 audit metadata attached. Only the AI provider/
    embedder/search are stubbed (see tests/whatsapp/test_autoreply.py for why -- pgvector search
    is Postgres-only); the messaging provider is the real "fake" one selected by settings, driven
    through the real send policy gate, exactly as it would be in production."""
    seed_connection(session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True)
    _opt_in_support(db, org_id)
    monkeypatch.setattr(
        autoreply, "get_llm_provider", lambda: RecordingLLM(["We ship within two business days."])
    )
    monkeypatch.setattr(autoreply, "get_embedding_provider", SpyEmbedder)
    monkeypatch.setattr(
        autoreply,
        "search_knowledge",
        StubSearch({org_id: [make_hit("We ship within two business days.", score=0.9)]}),
    )

    body, headers = _signed(_inbound_payload(text="How long does shipping take?"))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings, secrets_manager)
    assert outcome == "processed"

    with session_factory() as fresh:
        outbound = (
            fresh.query(WhatsAppMessage)
            .filter_by(organization_id=org_id, direction=WhatsAppMessageDirection.OUTBOUND)
            .one()
        )
        assert outbound.content == "We ship within two business days."
        assert outbound.status != WhatsAppMessageStatus.FAILED
        assert outbound.safe_metadata["ai"]["grounded"] is True
        assert outbound.safe_metadata["policy"] == {"outcome": "sent"}


def test_ai_never_auto_replies_to_its_own_outbound_status_webhook(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings, monkeypatch
):
    """Loop protection: Meta only ever sends a 'message' kind delivery for an INBOUND customer
    message. A status webhook for the bot's own outbound send (sent/delivered/read) is parsed as
    a 'status' kind event, which never reaches _process_message's auto-reply branch at all -- see
    services/messaging/whatsapp/webhooks.py:parse_webhook_payload. This proves no AI call is ever
    made for a pure status update, however AI auto-reply is configured."""
    connection_id = seed_connection(
        session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True
    )
    _opt_in_support(db, org_id)
    llm = RecordingLLM(["should never be called"])
    monkeypatch.setattr(autoreply, "get_llm_provider", lambda: llm)

    with session_factory() as fresh:
        contact = get_or_create_contact(fresh, organization_id=org_id, phone_e164="+14155551234")
        conversation = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact.id, connection_id=connection_id
        )
        window_service.record_inbound(conversation, now=NOW, window_hours=24)
        fresh.commit()
        row = send_service.send_text(
            fresh,
            organization_id=org_id,
            connection=fresh.get(WhatsAppConnection, connection_id),
            contact=contact,
            outbound=send_service.OutboundText(
                content="hi", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key="k-own-outbound",
            provider=FakeMessagingProvider(),
            access_token="tok",
            now=NOW,
        )
        wamid = row.provider_message_id

    body, headers = _signed(_status_payload(wamid, "delivered", int(NOW.timestamp()) + 5))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()
    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings, secrets_manager)
    assert outcome == "processed"
    assert llm.calls == []


def test_worker_crash_during_ai_generation_recovers_inbound_and_sends_reply(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings, monkeypatch
):
    """Crash scenario 1: worker died during AI provider request after committing the inbound message row.
    When reprocessed (status reset to PENDING, attempts incremented):
    - Inbound message row is not duplicated.
    - AI generation runs and generates the reply.
    - Exactly one outbound message is sent.
    - Webhook event is marked PROCESSED.
    """
    connection_id = seed_connection(
        session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True
    )
    _opt_in_support(db, org_id)

    wamid = f"wamid.crash1.{uuid.uuid4().hex[:10]}"
    body, headers = _signed(_inbound_payload(wamid=wamid, text="Where do you ship?"))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()

    # Pre-insert the inbound message row into DB to simulate that attempt 1 committed it before crashing mid-AI-call
    with session_factory() as fresh:
        conn = fresh.get(WhatsAppConnection, connection_id)
        contact = get_or_create_contact(fresh, organization_id=org_id, phone_e164="+14155551234")
        conv = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact.id, connection_id=conn.id
        )
        window_service.record_inbound(conv, now=NOW, window_hours=24)
        inbound_row = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=org_id,
            conversation_id=conv.id,
            direction=WhatsAppMessageDirection.INBOUND,
            message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.RECEIVED,
            idempotency_key=f"inbound:{wamid}",
            provider_message_id=wamid,
            content="Where do you ship?",
        )
        fresh.add(inbound_row)
        # Simulate that attempt 1 already incremented attempts to 1
        event = fresh.get(WhatsAppWebhookEvent, job.event_id)
        event.attempts = 1
        fresh.commit()

    llm = RecordingLLM(["We ship worldwide!"])
    monkeypatch.setattr(autoreply, "get_llm_provider", lambda: llm)
    monkeypatch.setattr(autoreply, "get_embedding_provider", SpyEmbedder)
    monkeypatch.setattr(
        autoreply,
        "search_knowledge",
        StubSearch({org_id: [make_hit("We ship worldwide!", score=0.9)]}),
    )

    # Worker picks up the job on attempt 2
    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings, secrets_manager)
    assert outcome == "processed"

    with session_factory() as fresh:
        inbound_msgs = list(
            fresh.scalars(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.organization_id == org_id,
                    WhatsAppMessage.direction == WhatsAppMessageDirection.INBOUND,
                    WhatsAppMessage.provider_message_id == wamid,
                )
            )
        )
        assert len(inbound_msgs) == 1  # inbound message recovered, never duplicated

        outbound_msgs = list(
            fresh.scalars(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.organization_id == org_id,
                    WhatsAppMessage.direction == WhatsAppMessageDirection.OUTBOUND,
                    WhatsAppMessage.idempotency_key == f"autoreply:{wamid}",
                )
            )
        )
        assert len(outbound_msgs) == 1  # outbound reply generated and sent
        assert outbound_msgs[0].status == WhatsAppMessageStatus.SENT
        assert outbound_msgs[0].content == "We ship worldwide!"

        evt = fresh.get(WhatsAppWebhookEvent, job.event_id)
        assert evt.status == WebhookEventStatus.PROCESSED


def test_worker_crash_after_ai_queued_completes_send_without_regenerating(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings, monkeypatch
):
    """Crash scenario 2: AI reply was already generated and persisted as QUEUED, but worker died
    before external send completed.
    When reprocessed:
    - AI provider is NOT called again.
    - Outbound row is claimed and sent to provider.
    - No duplicate outbound row is created.
    """
    connection_id = seed_connection(
        session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True
    )
    _opt_in_support(db, org_id)

    wamid = f"wamid.crash2.{uuid.uuid4().hex[:10]}"
    body, headers = _signed(_inbound_payload(wamid=wamid, text="Where do you ship?"))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()

    with session_factory() as fresh:
        conn = fresh.get(WhatsAppConnection, connection_id)
        contact = get_or_create_contact(fresh, organization_id=org_id, phone_e164="+14155551234")
        conv = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact.id, connection_id=conn.id
        )
        window_service.record_inbound(conv, now=NOW, window_hours=24)
        inbound_row = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=org_id,
            conversation_id=conv.id,
            direction=WhatsAppMessageDirection.INBOUND,
            message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.RECEIVED,
            idempotency_key=f"inbound:{wamid}",
            provider_message_id=wamid,
            content="Where do you ship?",
        )
        fresh.add(inbound_row)
        # Durable AI result already saved as QUEUED before crash
        queued_outbound = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=org_id,
            conversation_id=conv.id,
            direction=WhatsAppMessageDirection.OUTBOUND,
            message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.QUEUED,
            idempotency_key=f"autoreply:{wamid}",
            content="Pre-generated reply text",
            safe_metadata={"ai": {"grounded": True}},
        )
        fresh.add(queued_outbound)
        fresh.commit()

    llm = RecordingLLM(["Should never be called"])
    monkeypatch.setattr(autoreply, "get_llm_provider", lambda: llm)

    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings, secrets_manager)
    assert outcome == "processed"
    assert len(llm.calls) == 0  # No LLM re-generation

    with session_factory() as fresh:
        outbound_msgs = list(
            fresh.scalars(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.organization_id == org_id,
                    WhatsAppMessage.direction == WhatsAppMessageDirection.OUTBOUND,
                    WhatsAppMessage.idempotency_key == f"autoreply:{wamid}",
                )
            )
        )
        assert len(outbound_msgs) == 1
        assert outbound_msgs[0].status == WhatsAppMessageStatus.SENT
        assert outbound_msgs[0].content == "Pre-generated reply text"


def test_worker_crash_while_outbound_sending_handles_uncertainty_conservatively(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings, monkeypatch
):
    """Crash scenario 3: worker died mid-flight while outbound was SENDING.
    When reprocessed:
    - Conservatively assumes Meta may have accepted the message.
    - Does NOT dispatch a duplicate send to provider.
    - Webhook event completes without creating duplicate outbound row.
    """
    connection_id = seed_connection(
        session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True
    )
    _opt_in_support(db, org_id)

    wamid = f"wamid.crash3.{uuid.uuid4().hex[:10]}"
    body, headers = _signed(_inbound_payload(wamid=wamid, text="Where do you ship?"))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()

    with session_factory() as fresh:
        conn = fresh.get(WhatsAppConnection, connection_id)
        contact = get_or_create_contact(fresh, organization_id=org_id, phone_e164="+14155551234")
        conv = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact.id, connection_id=conn.id
        )
        window_service.record_inbound(conv, now=NOW, window_hours=24)
        inbound_row = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=org_id,
            conversation_id=conv.id,
            direction=WhatsAppMessageDirection.INBOUND,
            message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.RECEIVED,
            idempotency_key=f"inbound:{wamid}",
            provider_message_id=wamid,
            content="Where do you ship?",
        )
        fresh.add(inbound_row)
        sending_outbound = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=org_id,
            conversation_id=conv.id,
            direction=WhatsAppMessageDirection.OUTBOUND,
            message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.SENDING,
            idempotency_key=f"autoreply:{wamid}",
            content="In-flight reply text",
            attempts=1,
        )
        fresh.add(sending_outbound)
        fresh.commit()

    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings, secrets_manager)
    assert outcome == "processed"

    with session_factory() as fresh:
        outbound_msgs = list(
            fresh.scalars(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.organization_id == org_id,
                    WhatsAppMessage.direction == WhatsAppMessageDirection.OUTBOUND,
                    WhatsAppMessage.idempotency_key == f"autoreply:{wamid}",
                )
            )
        )
        assert len(outbound_msgs) == 1
        # Status remained SENDING (handled conservatively, no duplicate send triggered)
        assert outbound_msgs[0].status == WhatsAppMessageStatus.SENDING


def test_worker_crash_loop_is_bounded_by_max_attempts(
    db, org_id, session_factory, secrets_manager, queue, whatsapp_settings
):
    """Crash scenario 4: worker crashed repeatedly on the same event and exceeded MAX_ATTEMPTS.
    Fails safely instead of looping forever.
    """
    seed_connection(session_factory, org_id, secrets_manager, ai_auto_reply_enabled=True)
    _opt_in_support(db, org_id)

    wamid = f"wamid.crashloop.{uuid.uuid4().hex[:10]}"
    body, headers = _signed(_inbound_payload(wamid=wamid, text="Crash loop test"))
    wh.receive_webhook(db, body=body, headers=headers, settings=whatsapp_settings, queue=queue)
    job = queue.dequeue()

    with session_factory() as fresh:
        evt = fresh.get(WhatsAppWebhookEvent, job.event_id)
        evt.attempts = 5  # At MAX_ATTEMPTS, next claim makes it 6 (> MAX_ATTEMPTS)
        fresh.commit()

    outcome = wh.process_event(session_factory, job.event_id, whatsapp_settings, secrets_manager)
    assert outcome == "failed"

    with session_factory() as fresh:
        evt = fresh.get(WhatsAppWebhookEvent, job.event_id)
        assert evt.status == WebhookEventStatus.FAILED
        assert evt.error_code == "max_attempts_exceeded"
