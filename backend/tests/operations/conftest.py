import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from argon2 import PasswordHasher

from app.db.models import Customer, Order, Organization, OrganizationMember, OrganizationRole, User

_HASHER = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture()
def org_id(session_factory) -> uuid.UUID:
    oid = uuid.uuid4()
    with session_factory() as db:
        db.add(Organization(id=oid, name="Acme", slug=f"acme-{oid.hex[:8]}"))
        db.commit()
    return oid


def make_member(
    session_factory, org_id: uuid.UUID, *, role: OrganizationRole = OrganizationRole.MEMBER
) -> uuid.UUID:
    uid = uuid.uuid4()
    with session_factory() as db:
        db.add(
            User(
                id=uid, email=f"{uid.hex[:8]}@example.com", password_hash=_HASHER.hash("irrelevant")
            )
        )
        db.add(OrganizationMember(organization_id=org_id, user_id=uid, role=role))
        db.commit()
    return uid


@pytest.fixture()
def member_id(session_factory, org_id) -> uuid.UUID:
    return make_member(session_factory, org_id)


@pytest.fixture()
def owner_id(session_factory, org_id) -> uuid.UUID:
    return make_member(session_factory, org_id, role=OrganizationRole.OWNER)


def make_customer(db, org_id: uuid.UUID, *, n: int = 1) -> Customer:
    customer = Customer(id=uuid.uuid4(), organization_id=org_id, email=f"customer{n}@example.com")
    db.add(customer)
    db.flush()
    return customer


def make_order(
    db,
    org_id: uuid.UUID,
    *,
    customer: Customer | None = None,
    shopify_order_id: int = 1,
    total: Decimal = Decimal("100.00"),
    financial_status: str = "paid",
    fulfillment_status: str | None = "unfulfilled",
    created_at_shopify: datetime | None = None,
) -> Order:
    order = Order(
        id=uuid.uuid4(),
        organization_id=org_id,
        shopify_order_id=shopify_order_id,
        order_number=f"#{shopify_order_id}",
        customer_id=customer.id if customer else None,
        financial_status=financial_status,
        fulfillment_status=fulfillment_status,
        total=total,
        created_at_shopify=created_at_shopify,
        updated_at_shopify=created_at_shopify,
        synced_at=NOW,
    )
    db.add(order)
    db.flush()
    return order
