from datetime import UTC, datetime

from app.db.models import Customer
from app.services.email import consent as consent_service

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _customer(**kwargs) -> Customer:
    return Customer(email="sam@example.com", **kwargs)


def test_unknown_consent_is_never_treated_as_opted_in():
    customer = _customer(email_marketing_consent=None)
    assert consent_service.has_marketing_consent(customer) is False


def test_explicit_opt_in_grants_marketing_consent():
    customer = _customer()
    consent_service.set_marketing_consent(customer, consent=True)
    assert consent_service.has_marketing_consent(customer) is True


def test_explicit_opt_out_revokes_marketing_consent():
    customer = _customer()
    consent_service.set_marketing_consent(customer, consent=True)
    consent_service.set_marketing_consent(customer, consent=False)
    assert consent_service.has_marketing_consent(customer) is False


def test_suppression_overrides_consent_even_if_opted_in():
    customer = _customer()
    consent_service.set_marketing_consent(customer, consent=True)
    consent_service.suppress(customer, reason="bounced", now=NOW)
    assert consent_service.has_marketing_consent(customer) is False
    assert consent_service.is_suppressed(customer) is True


def test_suppress_also_clears_marketing_consent():
    customer = _customer()
    consent_service.set_marketing_consent(customer, consent=True)
    consent_service.suppress(customer, reason="complaint", now=NOW)
    assert customer.email_marketing_consent is False


def test_opting_back_in_lifts_suppression():
    """A fresh opt-in is one of the two ways suppression can be undone (brief section 20)."""
    customer = _customer()
    consent_service.suppress(customer, reason="bounced", now=NOW)
    consent_service.set_marketing_consent(customer, consent=True)
    assert consent_service.is_suppressed(customer) is False


def test_unsuppress_clears_the_flag_and_reason_without_granting_consent():
    customer = _customer()
    consent_service.suppress(customer, reason="bounced", now=NOW)
    consent_service.unsuppress(customer)
    assert consent_service.is_suppressed(customer) is False
    assert customer.email_suppressed_reason is None
    assert customer.email_marketing_consent is not True  # unsuppress alone never implies opt-in


def test_suppression_reason_is_bounded_length():
    customer = _customer()
    consent_service.suppress(customer, reason="x" * 500, now=NOW)
    assert len(customer.email_suppressed_reason) <= 60
