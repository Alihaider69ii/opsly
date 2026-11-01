import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.models import (
    WhatsAppConnectionStatus,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppMessageStatus,
)
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import SendRequest
from app.services.messaging.providers.fake import FakeMessagingProvider
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import send as send_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def connection(db, session_factory, org_id, secrets_manager):
    from app.db.models import WhatsAppConnection

    # seed_connection writes through its own short-lived session; fetch the row back through the
    # test's own `db` session so later mutations in a test and `db.commit()` act on the same
    # identity map (a detached instance from a closed session cannot be saved via another one).
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    return db.get(WhatsAppConnection, connection_id)


@pytest.fixture()
def contact(db, org_id):
    row = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    return row


@pytest.fixture()
def provider():
    return FakeMessagingProvider()


def _open_window(db, org_id, contact, connection, now=NOW):
    conversation = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
    )
    window_service.record_inbound(conversation, now=now, window_hours=24)
    db.commit()
    return conversation


def _opt_in(db, org_id, contact, category, now=NOW):
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=category,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="test",
        now=now,
    )
    db.commit()


def test_text_send_blocked_without_consent(db, org_id, connection, contact, provider):
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="k1",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.OPTED_OUT.value


def test_text_send_blocked_once_the_organization_rate_limit_is_exceeded(
    db, org_id, connection, contact, provider
):
    """Checked before connection/consent/window -- a rate-limited organization must never reach
    the provider, regardless of its other policy state."""
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    settings = get_settings()
    key = f"whatsapp-send:{org_id}"
    for _ in range(settings.whatsapp_send_rate_limit):
        assert limiter.allow(
            key, settings.whatsapp_send_rate_limit, settings.whatsapp_send_rate_window_seconds
        )
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="rate-limited",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.RATE_LIMITED.value
    assert row.provider_message_id is None  # the provider was never actually called


def test_rate_limit_is_scoped_per_organization(
    db, session_factory, secrets_manager, org_id, connection, contact, provider
):
    """Exhausting one organization's bucket must never block another, separate organization's
    send: each has its own connection/contact, as two real tenants would."""
    from app.db.models import Organization, WhatsAppConnection

    settings = get_settings()
    for _ in range(settings.whatsapp_send_rate_limit):
        assert limiter.allow(
            f"whatsapp-send:{org_id}",
            settings.whatsapp_send_rate_limit,
            settings.whatsapp_send_rate_window_seconds,
        )

    other_org_id = uuid.uuid4()
    with session_factory() as setup_db:
        setup_db.add(
            Organization(id=other_org_id, name="Other", slug=f"other-{other_org_id.hex[:8]}")
        )
        setup_db.commit()
    other_connection_id = seed_connection(
        session_factory, other_org_id, secrets_manager, phone_number_id="PNID_OTHER"
    )
    other_connection = db.get(WhatsAppConnection, other_connection_id)
    other_contact = get_or_create_contact(
        db, organization_id=other_org_id, phone_e164="+14155559999"
    )
    db.commit()
    _opt_in(db, other_org_id, other_contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, other_org_id, other_contact, other_connection)

    row = send_service.send_text(
        db,
        organization_id=other_org_id,
        connection=other_connection,
        contact=other_contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="other-org-not-limited",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.SENT


def test_text_send_blocked_with_window_closed(db, org_id, connection, contact, provider):
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="k2",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.WINDOW_CLOSED.value


def test_text_send_succeeds_with_consent_and_open_window(db, org_id, connection, contact, provider):
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="k3",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.SENT
    assert row.provider_message_id


def test_suppression_overrides_consent_and_open_window(db, org_id, connection, contact, provider):
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    consent_service.suppress(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        reason="user_opt_out_stop",
        source="inbound",
        now=NOW,
    )
    db.commit()
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="k4",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.POLICY_BLOCKED.value


def test_duplicate_idempotency_key_never_calls_the_provider_twice(
    db, org_id, connection, contact, provider
):
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    outbound = send_service.OutboundText(
        content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
    )
    first = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=outbound,
        idempotency_key="same-key",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    # A retry with the same key and DIFFERENT content must not resend; it must return the
    # original, already-resolved row untouched.
    second = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="a different message", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="same-key",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert first.id == second.id
    assert second.content == "hello"
    assert second.status == WhatsAppMessageStatus.SENT


def test_retryable_provider_error_is_requeued_to_queued_not_failed(db, org_id, connection, contact):
    class FlakyProvider:
        name = "flaky"
        calls = 0

        def send_text(self, request: SendRequest):
            FlakyProvider.calls += 1
            raise MessagingError(MessagingErrorCode.RATE_LIMITED, provider="flaky")

        def send_template(self, request):  # pragma: no cover
            raise NotImplementedError

        def mark_read(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    flaky = FlakyProvider()
    with pytest.raises(MessagingError) as excinfo:
        send_service.send_text(
            db,
            organization_id=org_id,
            connection=connection,
            contact=contact,
            outbound=send_service.OutboundText(
                content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key="retry-key",
            provider=flaky,
            access_token="tok",
            now=NOW,
        )
    assert excinfo.value.retryable
    row = send_service._existing(db, org_id, "retry-key")
    # Put back to QUEUED, never left stuck SENDING or marked FAILED: the next retry can claim it.
    assert row.status == WhatsAppMessageStatus.QUEUED

    # A second, successful attempt with the SAME key completes the same row.
    good_provider = FakeMessagingProvider()
    row2 = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="retry-key",
        provider=good_provider,
        access_token="tok",
        now=NOW,
    )
    assert row2.id == row.id
    assert row2.status == WhatsAppMessageStatus.SENT
    assert FlakyProvider.calls == 1  # the provider was called exactly once for the failed attempt


def test_non_retryable_provider_error_fails_the_row_closed(db, org_id, connection, contact):
    class RejectingProvider:
        name = "rejecting"

        def send_text(self, request):
            raise MessagingError(MessagingErrorCode.INVALID_REQUEST, provider="rejecting")

        def send_template(self, request):  # pragma: no cover
            raise NotImplementedError

        def mark_read(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    with pytest.raises(MessagingError):
        send_service.send_text(
            db,
            organization_id=org_id,
            connection=connection,
            contact=contact,
            outbound=send_service.OutboundText(
                content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
            ),
            idempotency_key="bad-key",
            provider=RejectingProvider(),
            access_token="tok",
            now=NOW,
        )
    row = send_service._existing(db, org_id, "bad-key")
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.INVALID_REQUEST.value


def test_template_send_requires_approved_template(db, org_id, connection, contact, provider):
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.MARKETING)
    row = send_service.send_template(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundTemplate(
            template_name="does_not_exist",
            template_language="en_US",
        ),
        idempotency_key="tmpl-key",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.TEMPLATE_NOT_APPROVED.value


def test_template_send_works_outside_the_window(db, org_id, connection, contact, provider):
    """Unlike free-form text, an approved template may be sent whether or not the window is open."""
    from app.services.messaging.providers.whatsapp_cloud import RemoteTemplate
    from app.services.messaging.whatsapp.templates import sync_templates

    _opt_in(db, org_id, contact, WhatsAppConsentCategory.MARKETING)
    sync_templates(
        db,
        organization_id=org_id,
        connection_id=connection.id,
        remote=[
            RemoteTemplate(
                name="order_shipped",
                language="en_US",
                category="UTILITY",
                status="APPROVED",
                provider_template_id="1",
                components=[{"type": "BODY", "text": "Hi {{1}}"}],
            )
        ],
        now=NOW,
    )
    db.commit()
    # Note: no inbound message was ever recorded -- the window is closed.
    row = send_service.send_template(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundTemplate(
            template_name="order_shipped",
            template_language="en_US",
            variables={"1": "Jane"},
        ),
        idempotency_key="tmpl-key-2",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.SENT


def test_malformed_template_variables_fail_closed_before_any_provider_call(
    db, org_id, connection, contact
):
    from app.services.messaging.providers.whatsapp_cloud import RemoteTemplate
    from app.services.messaging.whatsapp.templates import sync_templates

    class ExplodingProvider:
        name = "exploding"

        def send_text(self, request):  # pragma: no cover
            raise AssertionError("must never be called")

        def send_template(self, request):
            raise AssertionError("must never be called: validation must fail before this")

        def mark_read(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

    _opt_in(db, org_id, contact, WhatsAppConsentCategory.MARKETING)
    sync_templates(
        db,
        organization_id=org_id,
        connection_id=connection.id,
        remote=[
            RemoteTemplate(
                name="order_shipped",
                language="en_US",
                category="UTILITY",
                status="APPROVED",
                provider_template_id="1",
                components=[{"type": "BODY", "text": "Hi {{1}}"}],
            )
        ],
        now=NOW,
    )
    db.commit()
    row = send_service.send_template(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundTemplate(
            template_name="order_shipped",
            template_language="en_US",
            variables={},  # missing {{1}}
        ),
        idempotency_key="tmpl-bad",
        provider=ExplodingProvider(),
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.INVALID_REQUEST.value


def test_disconnected_connection_blocks_sending(db, org_id, connection, contact, provider):
    connection.status = WhatsAppConnectionStatus.DISCONNECTED
    db.commit()
    _opt_in(db, org_id, contact, WhatsAppConsentCategory.SUPPORT)
    _open_window(db, org_id, contact, connection)
    row = send_service.send_text(
        db,
        organization_id=org_id,
        connection=connection,
        contact=contact,
        outbound=send_service.OutboundText(
            content="hello", consent_category=WhatsAppConsentCategory.SUPPORT
        ),
        idempotency_key="disc-key",
        provider=provider,
        access_token="tok",
        now=NOW,
    )
    assert row.status == WhatsAppMessageStatus.FAILED
    assert row.error_code == MessagingErrorCode.UNCONFIGURED.value


def test_resolve_generated_text_only_substitutes_the_exact_whole_field_token():
    assert send_service.resolve_generated_text("{{generated_text}}", "AI output") == "AI output"
    assert send_service.resolve_generated_text("{{generated_text}}", None) == ""
    # Not a whole-field match: left as a literal string, never partially substituted.
    assert (
        send_service.resolve_generated_text("prefix {{generated_text}} suffix", "AI output")
        == "prefix {{generated_text}} suffix"
    )
    assert send_service.resolve_generated_text("plain text", "AI output") == "plain text"


def test_sanitize_text_strips_control_characters_and_bounds_length():
    assert send_service.sanitize_text("hello\x00world\x07") == "helloworld"
    assert send_service.sanitize_text("  padded  ") == "padded"
    long_text = "x" * 5000
    assert len(send_service.sanitize_text(long_text)) == send_service.MAX_TEXT_LENGTH


def test_decrypt_access_token_round_trips(db, org_id, connection, secrets_manager):
    token = send_service.decrypt_access_token(connection, secrets_manager)
    assert token == "test-token"


def test_decrypt_access_token_fails_closed_with_no_stored_token(connection, secrets_manager):
    connection.access_token_encrypted = None
    with pytest.raises(MessagingError) as excinfo:
        send_service.decrypt_access_token(connection, secrets_manager)
    assert excinfo.value.code == MessagingErrorCode.UNCONFIGURED


def test_claim_queued_succeeds_exactly_once(db, org_id, connection, contact):
    """The atomic claim is the idempotency boundary a concurrent duplicate relies on: once a row
    leaves QUEUED, a second claim attempt must not also succeed."""
    import uuid as uuid_module

    from app.db.models import (
        WhatsAppMessage,
        WhatsAppMessageDirection,
        WhatsAppMessageStatus,
        WhatsAppMessageType,
    )

    conversation = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection.id
    )
    row = WhatsAppMessage(
        id=uuid_module.uuid4(),
        organization_id=org_id,
        conversation_id=conversation.id,
        direction=WhatsAppMessageDirection.OUTBOUND,
        message_type=WhatsAppMessageType.TEXT,
        status=WhatsAppMessageStatus.QUEUED,
        idempotency_key="claim-once-key",
        content="hi",
    )
    db.add(row)
    db.commit()

    assert send_service._claim_queued(db, row) is True
    assert send_service._claim_queued(db, row) is False  # already claimed; must not re-succeed
