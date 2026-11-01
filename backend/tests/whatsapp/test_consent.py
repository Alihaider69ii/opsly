from datetime import UTC, datetime

from app.db.models import WhatsAppConsentCategory, WhatsAppConsentStatus
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _contact(db, org_id):
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    return contact


def test_missing_consent_row_is_unknown_not_opted_in(db, org_id):
    contact = _contact(db, org_id)
    status = consent_service.get_consent_status(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.MARKETING,
    )
    assert status is None  # UNKNOWN: callers must never treat this as opted in


def test_record_consent_then_read_it_back(db, org_id):
    contact = _contact(db, org_id)
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.MARKETING,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="dashboard_manual",
        now=NOW,
    )
    db.commit()
    status = consent_service.get_consent_status(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.MARKETING,
    )
    assert status == WhatsAppConsentStatus.OPTED_IN


def test_opting_out_of_marketing_does_not_affect_transactional_consent(db, org_id):
    contact = _contact(db, org_id)
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.TRANSACTIONAL,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="order_placed",
        now=NOW,
    )
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.MARKETING,
        status=WhatsAppConsentStatus.OPTED_OUT,
        source="stop_keyword",
        now=NOW,
    )
    db.commit()
    assert (
        consent_service.get_consent_status(
            db,
            organization_id=org_id,
            contact_id=contact.id,
            category=WhatsAppConsentCategory.TRANSACTIONAL,
        )
        == WhatsAppConsentStatus.OPTED_IN
    )
    assert (
        consent_service.get_consent_status(
            db,
            organization_id=org_id,
            contact_id=contact.id,
            category=WhatsAppConsentCategory.MARKETING,
        )
        == WhatsAppConsentStatus.OPTED_OUT
    )


def test_re_opting_in_after_opting_out_updates_the_same_row(db, org_id):
    contact = _contact(db, org_id)
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.SUPPORT,
        status=WhatsAppConsentStatus.OPTED_OUT,
        source="stop_keyword",
        now=NOW,
    )
    db.commit()
    consent_service.record_consent(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        category=WhatsAppConsentCategory.SUPPORT,
        status=WhatsAppConsentStatus.OPTED_IN,
        source="start_keyword",
        now=NOW,
    )
    db.commit()
    assert (
        consent_service.get_consent_status(
            db,
            organization_id=org_id,
            contact_id=contact.id,
            category=WhatsAppConsentCategory.SUPPORT,
        )
        == WhatsAppConsentStatus.OPTED_IN
    )


def test_suppression_is_independent_of_consent_and_blocks_regardless_of_category(db, org_id):
    contact = _contact(db, org_id)
    for category in WhatsAppConsentCategory:
        consent_service.record_consent(
            db,
            organization_id=org_id,
            contact_id=contact.id,
            category=category,
            status=WhatsAppConsentStatus.OPTED_IN,
            source="test",
            now=NOW,
        )
    db.commit()
    assert not consent_service.is_suppressed(db, organization_id=org_id, contact_id=contact.id)

    consent_service.suppress(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        reason="user_opt_out_stop",
        source="inbound",
        now=NOW,
    )
    db.commit()
    assert consent_service.is_suppressed(db, organization_id=org_id, contact_id=contact.id)
    # Consent rows are untouched by suppression.
    for category in WhatsAppConsentCategory:
        assert (
            consent_service.get_consent_status(
                db, organization_id=org_id, contact_id=contact.id, category=category
            )
            == WhatsAppConsentStatus.OPTED_IN
        )


def test_suppression_supports_safe_future_re_opt_in_by_lifting_not_deleting(db, org_id):
    contact = _contact(db, org_id)
    consent_service.suppress(
        db,
        organization_id=org_id,
        contact_id=contact.id,
        reason="manual",
        source="dashboard",
        now=NOW,
    )
    db.commit()
    assert consent_service.is_suppressed(db, organization_id=org_id, contact_id=contact.id)

    lifted = consent_service.lift_suppression(
        db, organization_id=org_id, contact_id=contact.id, now=NOW
    )
    db.commit()
    assert lifted
    assert not consent_service.is_suppressed(db, organization_id=org_id, contact_id=contact.id)

    # Lifting again (no active suppression) is a no-op, not an error.
    assert not consent_service.lift_suppression(
        db, organization_id=org_id, contact_id=contact.id, now=NOW
    )
