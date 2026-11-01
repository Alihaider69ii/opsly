"""Phase 9: deterministic, tenant-scoped customer/contact identity.

Matching order (fixed by the brief, never changed per-call):

    1. Shopify customer id
    2. normalized email
    3. normalized phone

Never fuzzy, never by name alone, and never across organizations -- every lookup and write here
takes ``organization_id`` and scopes every query to it. Callers commit (mirrors the convention in
``app/services/integrations/shopify/store.py`` and ``messaging/whatsapp/contacts.py``).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Customer
from app.services.auth.service import normalize_email as _normalize_email
from app.services.messaging.phone import InvalidPhoneNumber, normalize_phone


def normalize_email(value: str | None) -> str | None:
    """Lowercased/trimmed email for matching, or ``None`` if ``value`` is absent or not even
    shaped like an email (never raises -- identity matching always fails open to "no match")."""
    if not value or not isinstance(value, str):
        return None
    cleaned = _normalize_email(value)
    return cleaned if "@" in cleaned and not cleaned.startswith("@") else None


def normalize_phone_for_identity(
    value: str | None, *, default_country: str | None = None
) -> str | None:
    """E.164 for matching, or ``None`` if ``value`` is absent or cannot be confidently normalized
    (an ambiguous number is never guessed -- see ``app.services.messaging.phone``)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return normalize_phone(value, default_country=default_country)
    except InvalidPhoneNumber:
        return None


def find_customer(
    db: Session,
    *,
    organization_id: uuid.UUID,
    shopify_customer_id: int | None = None,
    email: str | None = None,
    phone: str | None = None,
    default_country: str | None = None,
) -> Customer | None:
    """Read-only deterministic lookup. Tries each signal in the fixed order; the first match
    wins. Returns ``None`` (never creates) if nothing matches."""
    if shopify_customer_id is not None:
        found = db.scalar(
            select(Customer).where(
                Customer.organization_id == organization_id,
                Customer.shopify_customer_id == shopify_customer_id,
            )
        )
        if found is not None:
            return found
    norm_email = normalize_email(email)
    if norm_email:
        found = db.scalar(
            select(Customer).where(
                Customer.organization_id == organization_id,
                Customer.normalized_email == norm_email,
            )
        )
        if found is not None:
            return found
    norm_phone = normalize_phone_for_identity(phone, default_country=default_country)
    if norm_phone:
        found = db.scalar(
            select(Customer).where(
                Customer.organization_id == organization_id,
                Customer.normalized_phone == norm_phone,
            )
        )
        if found is not None:
            return found
    return None


def _backfill(
    customer: Customer,
    *,
    email: str | None,
    phone: str | None,
    name: str | None,
    default_country: str | None,
) -> None:
    """Best-effort fill of previously-unknown identity fields from a new channel. Never
    overwrites an existing value (a later, lower-priority channel must not clobber data a higher-
    priority one already set)."""
    if email and not customer.email:
        customer.email = email[:320]
    norm_email = normalize_email(email)
    if norm_email and not customer.normalized_email:
        customer.normalized_email = norm_email
    if phone and not customer.phone:
        customer.phone = phone[:50]
    norm_phone = normalize_phone_for_identity(phone, default_country=default_country)
    if norm_phone and not customer.normalized_phone:
        customer.normalized_phone = norm_phone
    if name and not customer.first_name:
        customer.first_name = name[:255]


def get_or_create_customer(
    db: Session,
    *,
    organization_id: uuid.UUID,
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    shopify_customer_id: int | None = None,
    default_country: str | None = None,
) -> Customer:
    """Find-or-create, racing safely on a concurrent create for the same identity (mirrors
    ``whatsapp/contacts.py:get_or_create_contact``). At least one of ``email``/``phone`` should
    be supplied -- an anonymous customer with neither is still created (e.g. a website visitor who
    has not yet shared contact details), but can never be matched by identity later."""
    existing = find_customer(
        db,
        organization_id=organization_id,
        shopify_customer_id=shopify_customer_id,
        email=email,
        phone=phone,
        default_country=default_country,
    )
    if existing is not None:
        _backfill(existing, email=email, phone=phone, name=name, default_country=default_country)
        return existing
    customer = Customer(
        id=uuid.uuid4(),
        organization_id=organization_id,
        shopify_customer_id=shopify_customer_id,
        email=email[:320] if email else None,
        phone=phone[:50] if phone else None,
        normalized_email=normalize_email(email),
        normalized_phone=normalize_phone_for_identity(phone, default_country=default_country),
        first_name=name[:255] if name else None,
    )
    db.add(customer)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = find_customer(
            db,
            organization_id=organization_id,
            shopify_customer_id=shopify_customer_id,
            email=email,
            phone=phone,
            default_country=default_country,
        )
        if existing is None:
            raise
        _backfill(existing, email=email, phone=phone, name=name, default_country=default_country)
        return existing
    return customer


def customer_summary(customer: Customer) -> dict[str, Any]:
    """A small, bounded, safe-to-display snapshot for timeline/event payloads -- never the full
    row (no raw tags, no Shopify ids beyond existence)."""
    return {
        "id": str(customer.id),
        "name": " ".join(filter(None, [customer.first_name, customer.last_name])) or None,
        "has_email": bool(customer.email),
        "has_phone": bool(customer.phone),
    }
