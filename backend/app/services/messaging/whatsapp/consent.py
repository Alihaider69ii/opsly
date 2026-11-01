"""Consent and suppression: the deterministic policy state checked before every outbound send.

Consent is a per-category *current state* row; a missing row is UNKNOWN and UNKNOWN is never
treated as opted in (``get_consent_status`` returns ``None`` for "no row", and every caller must
treat ``None`` the same as ``OPTED_OUT``). Suppression is a separate, broader kill switch checked
independently of category. Both are read fresh on every send: nothing here is cached.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    WhatsAppConsent,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppSuppression,
)


def get_consent_status(
    db: Session,
    *,
    organization_id: uuid.UUID,
    contact_id: uuid.UUID,
    category: WhatsAppConsentCategory,
) -> WhatsAppConsentStatus | None:
    """``None`` means UNKNOWN (no row). Callers must never treat ``None`` as opted in."""
    row = db.scalar(
        select(WhatsAppConsent).where(
            WhatsAppConsent.organization_id == organization_id,
            WhatsAppConsent.contact_id == contact_id,
            WhatsAppConsent.category == category,
        )
    )
    return row.status if row is not None else None


def record_consent(
    db: Session,
    *,
    organization_id: uuid.UUID,
    contact_id: uuid.UUID,
    category: WhatsAppConsentCategory,
    status: WhatsAppConsentStatus,
    source: str,
    now: datetime,
    safe_metadata: dict[str, Any] | None = None,
) -> WhatsAppConsent:
    """Upsert the current-state row for one (contact, category) pair. Callers commit."""
    row = db.scalar(
        select(WhatsAppConsent)
        .where(
            WhatsAppConsent.organization_id == organization_id,
            WhatsAppConsent.contact_id == contact_id,
            WhatsAppConsent.category == category,
        )
        .with_for_update()
    )
    if row is None:
        row = WhatsAppConsent(
            id=uuid.uuid4(),
            organization_id=organization_id,
            contact_id=contact_id,
            category=category,
            status=status,
            source=source[:60],
            captured_at=now,
            safe_metadata=safe_metadata,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = db.scalar(
                select(WhatsAppConsent)
                .where(
                    WhatsAppConsent.organization_id == organization_id,
                    WhatsAppConsent.contact_id == contact_id,
                    WhatsAppConsent.category == category,
                )
                .with_for_update()
            )
            if row is None:
                raise
        else:
            return row
    row.status = status
    row.source = source[:60]
    row.captured_at = now
    row.safe_metadata = safe_metadata
    row.revoked_at = now if status == WhatsAppConsentStatus.OPTED_OUT else None
    return row


def is_suppressed(db: Session, *, organization_id: uuid.UUID, contact_id: uuid.UUID) -> bool:
    row = db.scalar(
        select(WhatsAppSuppression.id).where(
            WhatsAppSuppression.organization_id == organization_id,
            WhatsAppSuppression.contact_id == contact_id,
            WhatsAppSuppression.lifted_at.is_(None),
        )
    )
    return row is not None


def suppress(
    db: Session,
    *,
    organization_id: uuid.UUID,
    contact_id: uuid.UUID,
    reason: str,
    source: str,
    now: datetime,
) -> WhatsAppSuppression:
    row = db.scalar(
        select(WhatsAppSuppression)
        .where(
            WhatsAppSuppression.organization_id == organization_id,
            WhatsAppSuppression.contact_id == contact_id,
        )
        .with_for_update()
    )
    if row is None:
        row = WhatsAppSuppression(
            id=uuid.uuid4(),
            organization_id=organization_id,
            contact_id=contact_id,
            reason=reason[:60],
            source=source[:60],
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = db.scalar(
                select(WhatsAppSuppression)
                .where(
                    WhatsAppSuppression.organization_id == organization_id,
                    WhatsAppSuppression.contact_id == contact_id,
                )
                .with_for_update()
            )
            if row is None:
                raise
        else:
            return row
    row.reason = reason[:60]
    row.source = source[:60]
    row.lifted_at = None  # re-suppressing a previously-lifted contact
    return row


def lift_suppression(
    db: Session, *, organization_id: uuid.UUID, contact_id: uuid.UUID, now: datetime
) -> bool:
    """Support future safe re-opt-in: lift, never delete, so the record (and its history) stays."""
    row = db.scalar(
        select(WhatsAppSuppression)
        .where(
            WhatsAppSuppression.organization_id == organization_id,
            WhatsAppSuppression.contact_id == contact_id,
            WhatsAppSuppression.lifted_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        return False
    row.lifted_at = now
    return True
