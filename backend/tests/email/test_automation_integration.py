import uuid
from datetime import UTC, datetime

import pytest

from app.db.models import (
    AutomationExecution,
    AutomationWorkflow,
    EmailClassification,
    EmailTemplate,
    EmailTemplateStatus,
    ExecutionStatus,
    WorkflowStatus,
)
from app.services import automation
from app.services.automation import InvalidWorkflow
from app.services.email.providers.fake import FakeEmailProvider
from tests.email.conftest import seed_connection, seed_customer

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _email_definition(**overrides):
    action = {
        "type": "send_email",
        "subject": "An update on your order",
        "content": "Thanks for your order!",
        "classification": "TRANSACTIONAL",
        **overrides,
    }
    return {"trigger": {"event": "website_lead_captured"}, "conditions": [], "actions": [action]}


def _email_template_definition(*, template_id: str, variables: dict | None = None):
    action = {"type": "send_email_template", "template_id": template_id, "variables": variables or {}}
    return {"trigger": {"event": "website_lead_captured"}, "conditions": [], "actions": [action]}


# --- validate_definition: send_email ------------------------------------------------------------


def test_validate_definition_accepts_send_email():
    automation.validate_definition(_email_definition())


def test_validate_definition_rejects_oversized_subject():
    bad = _email_definition(subject="x" * 201)
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_empty_content():
    bad = _email_definition(content="")
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_oversized_content():
    bad = _email_definition(content="x" * 5001)
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_invalid_classification():
    bad = _email_definition(classification="NOT_A_REAL_CLASSIFICATION")
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


def test_validate_definition_rejects_unsupported_fields_on_send_email():
    bad = _email_definition()
    bad["actions"][0]["unexpected_field"] = "nope"
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


# --- validate_definition: send_email_template --------------------------------------------------


def test_validate_definition_accepts_send_email_template():
    automation.validate_definition(_email_template_definition(template_id=str(uuid.uuid4())))


def test_validate_definition_rejects_a_non_uuid_template_id():
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(_email_template_definition(template_id="not-a-uuid"))


def test_validate_definition_rejects_oversized_template_variables():
    bad = _email_template_definition(
        template_id=str(uuid.uuid4()), variables={str(i): "x" for i in range(21)}
    )
    with pytest.raises(InvalidWorkflow):
        automation.validate_definition(bad)


# --- end-to-end dispatch ---------------------------------------------------------------------------


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


def test_send_email_action_dispatches_a_real_email_end_to_end(
    db, org_id, queue, session_factory, monkeypatch
):
    monkeypatch.setattr(automation, "get_email_provider", lambda: FakeEmailProvider())
    seed_connection(session_factory, org_id)
    customer_id = seed_customer(session_factory, org_id, email="sam@example.com")

    outcome, execution_id = _run_workflow(
        db, org_id, queue, _email_definition(), payload={"customer": {"id": str(customer_id)}}
    )

    execution = db.get(AutomationExecution, execution_id)
    assert outcome == "completed", f"unexpected failure: {execution.error_code}"
    assert execution.status == ExecutionStatus.COMPLETED

    from app.db.models import EmailMessage

    message = db.query(EmailMessage).filter_by(organization_id=org_id).one()
    assert message.body_text == "Thanks for your order!"
    assert message.classification == EmailClassification.TRANSACTIONAL


def test_send_email_template_action_renders_and_dispatches(
    db, org_id, queue, session_factory, monkeypatch
):
    monkeypatch.setattr(automation, "get_email_provider", lambda: FakeEmailProvider())
    seed_connection(session_factory, org_id)
    customer_id = seed_customer(session_factory, org_id, email="sam@example.com", first_name="Sam")

    template = EmailTemplate(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="Welcome",
        type=EmailClassification.LIFECYCLE,
        subject="Hi {{customer.first_name}}",
        body_html="<p>Hi {{customer.first_name}}</p>",
        body_text="Hi {{customer.first_name}}",
        variables=["customer.first_name"],
        status=EmailTemplateStatus.ACTIVE,
    )
    db.add(template)
    db.commit()

    outcome, execution_id = _run_workflow(
        db,
        org_id,
        queue,
        _email_template_definition(template_id=str(template.id)),
        payload={"customer": {"id": str(customer_id)}},
    )

    execution = db.get(AutomationExecution, execution_id)
    assert outcome == "completed", f"unexpected failure: {execution.error_code}"

    from app.db.models import EmailMessage

    message = db.query(EmailMessage).filter_by(organization_id=org_id).one()
    assert message.subject == "Hi Sam"
    assert message.body_text == "Hi Sam"
    assert message.template_id == template.id


def test_unresolvable_email_recipient_fails_closed(db, org_id, queue, session_factory, monkeypatch):
    monkeypatch.setattr(automation, "get_email_provider", lambda: FakeEmailProvider())
    seed_connection(session_factory, org_id)

    outcome, execution_id = _run_workflow(
        db, org_id, queue, _email_definition(), payload={"customer": {"id": str(uuid.uuid4())}}
    )

    assert outcome == "failed"
    execution = db.get(AutomationExecution, execution_id)
    assert execution.status == ExecutionStatus.FAILED
    assert execution.error_code == "email_invalid_recipient"


def test_dispatch_fails_closed_when_no_email_connection_exists(db, org_id, queue, session_factory, monkeypatch):
    monkeypatch.setattr(automation, "get_email_provider", lambda: FakeEmailProvider())
    customer_id = seed_customer(session_factory, org_id, email="sam@example.com")

    outcome, execution_id = _run_workflow(
        db, org_id, queue, _email_definition(), payload={"customer": {"id": str(customer_id)}}
    )

    assert outcome == "failed"
    execution = db.get(AutomationExecution, execution_id)
    assert execution.error_code == "email_unconfigured"
