import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import (
    AutomationEvent,
    AutomationExecution,
    AutomationExecutionStep,
    AutomationWorkflow,
    EventStatus,
    ExecutionStatus,
    StepStatus,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppMessage,
    WorkflowStatus,
)
from app.services import automation
from app.services.automation import InvalidWorkflow
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _text_definition(content="hello"):
    return {
        "trigger": {"event": "website_lead_captured"},
        "conditions": [],
        "actions": [
            {"type": "send_whatsapp_text", "content": content, "consent_category": "SUPPORT"}
        ],
    }


def _chained_ai_definition():
    return {
        "trigger": {"event": "website_lead_captured"},
        "conditions": [],
        "actions": [
            {"type": "ai_generate", "prompt": "write a short greeting"},
            {
                "type": "send_whatsapp_text",
                "content": "{{generated_text}}",
                "consent_category": "MARKETING",
            },
        ],
    }


def _template_definition():
    return {
        "trigger": {"event": "website_lead_captured"},
        "conditions": [],
        "actions": [
            {
                "type": "send_whatsapp_template",
                "template_name": "order_shipped",
                "template_language": "en_US",
                "variables": {"1": "Jane"},
                "consent_category": "MARKETING",
            }
        ],
    }


def test_validate_definition_accepts_send_whatsapp_text():
    automation.validate_definition(_text_definition())


def test_validate_definition_accepts_send_whatsapp_template():
    automation.validate_definition(_template_definition())


def test_validate_definition_rejects_invalid_consent_category():
    bad = _text_definition()
    bad["actions"][0]["consent_category"] = "NOT_A_CATEGORY"
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_missing_consent_category():
    bad = _text_definition()
    del bad["actions"][0]["consent_category"]
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_oversized_template_variables():
    bad = _template_definition()
    bad["actions"][0]["variables"] = {str(i): "x" for i in range(21)}  # cap is 20 entries
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_unsupported_fields_on_whatsapp_actions():
    bad = _text_definition()
    bad["actions"][0]["unexpected_field"] = "nope"
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def _run_workflow(db, org_id, queue, definition, payload):
    clean = automation.validate_definition(definition)
    workflow = AutomationWorkflow(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="wf",
        status=WorkflowStatus.ACTIVE,
        version=1,
        definition=clean,
    )
    db.add(workflow)
    db.commit()
    automation.ingest_event(
        db,
        queue,
        organization_id=org_id,
        event_type="website_lead_captured",
        source="website",
        source_id=str(uuid.uuid4()),
        dedupe_key=str(uuid.uuid4()),
        payload=payload,
    )
    dispatch_job = queue.dequeue()
    created = automation.dispatch_event(db, dispatch_job.event_id, queue)
    assert created == 1
    exec_job = queue.dequeue()
    outcome = automation.run_execution(db, exec_job.execution_id, queue)
    return outcome, exec_job.execution_id


def test_unresolvable_recipient_fails_closed(db, org_id, queue, session_factory, secrets_manager):
    seed_connection(session_factory, org_id, secrets_manager)
    outcome, execution_id = _run_workflow(
        db,
        org_id,
        queue,
        _text_definition(),
        payload={"customer": {"exists": True, "id": str(uuid.uuid4())}},
    )
    assert outcome == "failed"
    from app.db.models import AutomationExecution

    execution = db.get(AutomationExecution, execution_id)
    assert execution.status == ExecutionStatus.FAILED
    assert execution.error_code == "whatsapp_invalid_recipient"


def test_ai_generate_chains_into_send_whatsapp_text_via_generated_text_placeholder(
    db, org_id, queue, session_factory, secrets_manager, whatsapp_settings, monkeypatch
):
    from app.services.ai.provider import AIResponse
    from app.services.messaging.providers.fake import FakeMessagingProvider

    class RecordingProvider:
        name = "fake"
        model = "test"

        def generate(self, request):
            return AIResponse(text="Hello from BizFlow", provider="fake", model="test")

    # automation.py resolves the LLM/messaging provider and the secret manager from the
    # process-wide cached settings (it is plain worker code, not a FastAPI endpoint, so test
    # dependency overrides don't reach it); monkeypatch the names it imported, matching the
    # existing ai_generate test precedent, so a test's encryption key and provider choice apply.
    monkeypatch.setattr(automation, "get_llm_provider", lambda: RecordingProvider())
    monkeypatch.setattr(automation, "get_messaging_provider", lambda: FakeMessagingProvider())
    monkeypatch.setattr(automation, "get_settings", lambda: whatsapp_settings)

    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    with session_factory() as fresh:
        from app.db.models import WhatsAppConnection

        connection = fresh.get(WhatsAppConnection, connection_id)
        contact = get_or_create_contact(fresh, organization_id=org_id, phone_e164="+14155551234")
        fresh.commit()
        # The real send inside automation.run_execution checks the window against real wall-clock
        # time (it passes no `now=` override), so the window must be opened relative to that, not
        # the fixed `NOW` used elsewhere in this file for consent timestamps.
        real_now = datetime.now(UTC)
        consent_service.record_consent(
            fresh,
            organization_id=org_id,
            contact_id=contact.id,
            category=WhatsAppConsentCategory.MARKETING,
            status=WhatsAppConsentStatus.OPTED_IN,
            source="test",
            now=NOW,
        )
        conversation = window_service.get_or_create_conversation(
            fresh, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
        )
        window_service.record_inbound(conversation, now=real_now, window_hours=24)
        fresh.commit()
        contact_id = contact.id

    outcome, execution_id = _run_workflow(
        db,
        org_id,
        queue,
        _chained_ai_definition(),
        payload={"whatsapp": {"contact_id": str(contact_id)}},
    )
    execution = db.get(AutomationExecution, execution_id)
    assert outcome == "completed", f"unexpected failure: {execution.error_code}"
    assert execution.status == ExecutionStatus.COMPLETED

    message = (
        db.query(WhatsAppMessage)
        .filter_by(organization_id=org_id)
        .order_by(WhatsAppMessage.created_at.desc())
        .first()
    )
    assert message.content
    assert message.content != "{{generated_text}}"


def test_worker_crash_during_a_whatsapp_send_fails_the_execution_rather_than_retrying(
    db, org_id, queue
):
    """Mirrors the pre-existing ai_generate crash-recovery test: a send_whatsapp_* step stuck
    RUNNING past the heartbeat cutoff must fail the execution outright (the provider call may
    already have gone through), never be silently retried."""
    workflow = AutomationWorkflow(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="WA crash",
        status=WorkflowStatus.ACTIVE,
        version=1,
        definition={
            "trigger": {"event": "website_lead_captured"},
            "conditions": [],
            "actions": [
                {"type": "send_whatsapp_text", "content": "hi", "consent_category": "SUPPORT"}
            ],
        },
    )
    event = AutomationEvent(
        id=uuid.uuid4(),
        organization_id=org_id,
        event_type="website_lead_captured",
        source="website",
        dedupe_key="wa-crash",
        occurred_at=datetime.now(UTC),
        payload={},
        depth=0,
        status=EventStatus.PROCESSED,
        created_at=datetime.now(UTC),
    )
    db.add_all([workflow, event])
    db.commit()
    execution = AutomationExecution(
        id=uuid.uuid4(),
        organization_id=org_id,
        workflow_id=workflow.id,
        event_id=event.id,
        workflow_version=1,
        workflow_snapshot={"name": workflow.name, "definition": workflow.definition},
        event_snapshot={},
        status=ExecutionStatus.RUNNING,
        current_step=0,
        attempts=1,
        max_attempts=5,
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        created_at=datetime.now(UTC),
    )
    db.add(execution)
    db.flush()
    db.add(
        AutomationExecutionStep(
            id=uuid.uuid4(),
            organization_id=org_id,
            execution_id=execution.id,
            step_index=0,
            action_snapshot=workflow.definition["actions"][0],
            status=StepStatus.RUNNING,
            attempts=1,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()

    assert automation.recover_due(db, queue, stale_seconds=60) == 0  # not re-queued
    db.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED
    assert execution.error_code == "worker_lost_during_whatsapp_send"
