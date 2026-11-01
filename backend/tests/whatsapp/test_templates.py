import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import WhatsAppTemplate
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.providers.whatsapp_cloud import RemoteTemplate
from app.services.messaging.whatsapp import templates as template_service
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _template(components: list[dict], status: str = "APPROVED") -> WhatsAppTemplate:
    return WhatsAppTemplate(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        name="order_shipped",
        language="en_US",
        category="UTILITY",
        status=status,
        components=components,
    )


def test_template_with_no_placeholders_needs_no_variables():
    template = _template([{"type": "BODY", "text": "Thanks for your order!"}])
    assert template_service.required_variable_keys(template) == []
    assert template_service.build_body_component(template, {}) is None


def test_positional_placeholders_build_an_ordered_body_component():
    template = _template([{"type": "BODY", "text": "Hi {{1}}, your order {{2}} shipped."}])
    assert template_service.required_variable_keys(template) == ["1", "2"]
    component = template_service.build_body_component(template, {"1": "Jane", "2": "#123"})
    assert component is not None
    assert component.type == "body"
    assert list(component.parameters) == [
        {"type": "text", "text": "Jane"},
        {"type": "text", "text": "#123"},
    ]


def test_named_placeholders_are_rejected_not_guessed():
    template = _template([{"type": "BODY", "text": "Hi {{customer_name}}, welcome!"}])
    with pytest.raises(MessagingError) as excinfo:
        template_service.required_variable_keys(template)
    assert excinfo.value.code == MessagingErrorCode.INVALID_REQUEST


def test_non_sequential_placeholders_are_rejected():
    template = _template([{"type": "BODY", "text": "{{1}} and {{3}}"}])
    with pytest.raises(MessagingError):
        template_service.required_variable_keys(template)


@pytest.mark.parametrize(
    "variables",
    [
        {},  # missing required variable
        {"1": "a", "2": "b", "3": "c"},  # extra variable not in the template
        {"1": ""},  # empty value
        {"1": "x" * 501},  # oversized value
    ],
)
def test_malformed_variables_fail_closed(variables):
    template = _template([{"type": "BODY", "text": "Hi {{1}}"}])
    with pytest.raises(MessagingError) as excinfo:
        template_service.build_body_component(template, variables)
    assert excinfo.value.code == MessagingErrorCode.INVALID_REQUEST


def test_providing_variables_to_a_template_with_none_is_rejected():
    template = _template([{"type": "BODY", "text": "No variables here."}])
    with pytest.raises(MessagingError):
        template_service.build_body_component(template, {"1": "unexpected"})


@pytest.mark.parametrize(
    "status,expected",
    [
        ("APPROVED", True),
        ("approved", True),
        ("PENDING_REVIEW", False),
        ("REJECTED", False),
        ("PAUSED", False),
        ("DISABLED", False),
    ],
)
def test_only_approved_templates_are_usable(status, expected):
    template = _template([{"type": "BODY", "text": "Hi there"}], status=status)
    assert template_service.is_usable(template) is expected


def test_sync_templates_upserts_by_name_and_language(db, session_factory, org_id, secrets_manager):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    remote = [
        RemoteTemplate(
            name="order_shipped",
            language="en_US",
            category="UTILITY",
            status="APPROVED",
            provider_template_id="123",
            components=[{"type": "BODY", "text": "Hi {{1}}"}],
        )
    ]
    count = template_service.sync_templates(
        db, organization_id=org_id, connection_id=connection_id, remote=remote, now=NOW
    )
    db.commit()
    assert count == 1
    stored = template_service.get_template(
        db,
        organization_id=org_id,
        connection_id=connection_id,
        name="order_shipped",
        language="en_US",
    )
    assert stored is not None
    assert stored.provider_template_id == "123"
    # SQLite drops timezone metadata on round-trip; compare the naive value.
    assert stored.last_synced_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)

    # A second sync with an updated status upserts the same row, not a duplicate.
    remote[0] = RemoteTemplate(
        name="order_shipped",
        language="en_US",
        category="UTILITY",
        status="PAUSED",
        provider_template_id="123",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    template_service.sync_templates(
        db, organization_id=org_id, connection_id=connection_id, remote=remote, now=NOW
    )
    db.commit()
    all_rows = list(
        db.scalars(select(WhatsAppTemplate).where(WhatsAppTemplate.organization_id == org_id))
    )
    assert len(all_rows) == 1
    assert all_rows[0].status == "PAUSED"
