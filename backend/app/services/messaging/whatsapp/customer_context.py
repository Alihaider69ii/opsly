"""Phase 8 minimal, READ-ONLY customer context: when a WhatsApp contact's phone number matches
an existing Shopify-synced customer for the same organization, surface a small number of safe,
structured facts (name, latest order status) to the AI prompt -- never a transactional action.
Cancellation, refunds, address changes and payment changes are explicitly out of scope for AI
auto-reply; see docs/phase-8-progress.md "Non-Goals".

Best-effort and fail-open by design: any lookup problem here means "no customer context", never
a broken auto-reply. This is deliberately NOT a general CRM/customer-matching feature -- it is a
few literal phone-string comparisons bounded by ``organization_id``, not a table scan.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Customer, Order

logger = logging.getLogger(__name__)
MAX_FACTS = 3


def _candidate_phones(phone_e164: str) -> list[str]:
    """A few literal string forms the same number might be stored as in synced Shopify data,
    which is not guaranteed to be E.164. Deliberately not a fuzzy/normalized match."""
    candidates = [phone_e164]
    if phone_e164.startswith("+"):
        candidates.append(phone_e164[1:])
        candidates.append("00" + phone_e164[1:])
    return candidates


def _find_customer(db: Session, *, organization_id: uuid.UUID, phone_e164: str) -> Customer | None:
    return db.scalar(
        select(Customer)
        .where(
            Customer.organization_id == organization_id,
            Customer.phone.in_(_candidate_phones(phone_e164)),
            Customer.deleted_at.is_(None),
        )
        .limit(1)
    )


def _latest_order(
    db: Session, *, organization_id: uuid.UUID, customer_id: uuid.UUID
) -> Order | None:
    return db.scalar(
        select(Order)
        .where(Order.organization_id == organization_id, Order.customer_id == customer_id)
        .order_by(Order.created_at_shopify.desc(), Order.created_at.desc())
        .limit(1)
    )


def build_customer_facts_for(
    db: Session, *, organization_id: uuid.UUID, customer: Customer
) -> list[str]:
    """Same facts as ``build_customer_facts``, for a caller (Phase 9 email auto-reply) that has
    already resolved the ``Customer`` row itself (via deterministic email/phone identity
    matching, see ``app.services.support.identity``) rather than a WhatsApp phone lookup."""
    try:
        order = _latest_order(db, organization_id=organization_id, customer_id=customer.id)
    except SQLAlchemyError:
        logger.exception("customer context lookup failed")
        return []
    facts: list[str] = []
    name = " ".join(p for p in (customer.first_name, customer.last_name) if p).strip()
    if name:
        facts.append(f"The customer's name on file is {name}.")
    if order is not None:
        bits = []
        if order.order_number:
            bits.append(f"order {order.order_number}")
        if order.fulfillment_status:
            bits.append(f"fulfillment status {order.fulfillment_status}")
        elif order.financial_status:
            bits.append(f"payment status {order.financial_status}")
        if order.cancelled_at is not None:
            bits.append("cancelled")
        if bits:
            facts.append("Most recent order on file: " + ", ".join(bits) + ".")
    return facts[:MAX_FACTS]


def build_customer_facts(db: Session, *, organization_id: uuid.UUID, phone_e164: str) -> list[str]:
    """Plain-English facts, ready to go straight into the CUSTOMER CONTEXT prompt block (see
    ``app.services.ai.prompts.build_customer_context_message``). Empty when nothing matches."""
    try:
        customer = _find_customer(db, organization_id=organization_id, phone_e164=phone_e164)
        if customer is None:
            return []
    except SQLAlchemyError:
        logger.exception("whatsapp customer context lookup failed")
        return []
    return build_customer_facts_for(db, organization_id=organization_id, customer=customer)
