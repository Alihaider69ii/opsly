"""Contact lookup/creation, keyed only by the canonical E.164 phone (never a raw string).

Callers commit; this only adds/flushes within the caller's transaction, matching the "callers
commit" convention used throughout ``app/services/integrations/shopify/store.py``.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import WhatsAppContact


def get_or_create_contact(
    db: Session,
    *,
    organization_id: uuid.UUID,
    phone_e164: str,
    wa_id: str | None = None,
    display_name: str | None = None,
) -> WhatsAppContact:
    contact = db.scalar(
        select(WhatsAppContact)
        .where(
            WhatsAppContact.organization_id == organization_id,
            WhatsAppContact.phone_e164 == phone_e164,
        )
        .with_for_update()
    )
    if contact is not None:
        if wa_id and contact.wa_id != wa_id:
            contact.wa_id = wa_id
        if display_name and not contact.display_name:
            contact.display_name = display_name
        return contact
    contact = WhatsAppContact(
        id=uuid.uuid4(),
        organization_id=organization_id,
        phone_e164=phone_e164,
        wa_id=wa_id,
        display_name=display_name,
    )
    db.add(contact)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        contact = db.scalar(
            select(WhatsAppContact).where(
                WhatsAppContact.organization_id == organization_id,
                WhatsAppContact.phone_e164 == phone_e164,
            )
        )
        if contact is None:
            raise
    return contact


def find_contact(
    db: Session, *, organization_id: uuid.UUID, phone_e164: str
) -> WhatsAppContact | None:
    return db.scalar(
        select(WhatsAppContact).where(
            WhatsAppContact.organization_id == organization_id,
            WhatsAppContact.phone_e164 == phone_e164,
        )
    )
