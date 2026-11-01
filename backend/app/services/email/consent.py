"""Email marketing consent + suppression, read fresh from ``Customer`` on every send -- never
cached (mirrors the semantics of ``app/services/messaging/whatsapp/consent.py``, but the state
lives directly on the customer row rather than a separate per-category table, since email has
exactly one consent category that matters for gating: marketing).

``None`` for ``email_marketing_consent`` means UNKNOWN (never asked) and is never treated as
opted in. Suppression is a separate, broader kill switch checked independently of consent, and is
never lifted automatically -- only an explicit dashboard action or a fresh opt-in can undo it.

Transactional/support email is deliberately NOT gated by any of this (brief section 20): those
sends go through ``email/send.py`` without a consent check, exactly as a password-reset email is
never subject to a marketing opt-in.
"""

from datetime import datetime

from app.db.models import Customer


def has_marketing_consent(customer: Customer) -> bool:
    return customer.email_marketing_consent is True and not customer.email_suppressed


def is_suppressed(customer: Customer) -> bool:
    return customer.email_suppressed


def set_marketing_consent(customer: Customer, *, consent: bool) -> None:
    customer.email_marketing_consent = consent
    if consent:
        customer.email_suppressed = False
        customer.email_suppressed_reason = None
        customer.email_suppressed_at = None


def suppress(customer: Customer, *, reason: str, now: datetime) -> None:
    customer.email_suppressed = True
    customer.email_suppressed_reason = reason[:60]
    customer.email_suppressed_at = now
    customer.email_marketing_consent = False


def unsuppress(customer: Customer) -> None:
    customer.email_suppressed = False
    customer.email_suppressed_reason = None
    customer.email_suppressed_at = None
