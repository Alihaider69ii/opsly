import uuid
from datetime import UTC, datetime

from app.db.models import Customer, Order, Organization
from app.services.messaging.whatsapp import customer_context

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _customer(db, org_id, *, phone: str | None, **kwargs) -> Customer:
    customer = Customer(
        id=uuid.uuid4(),
        organization_id=org_id,
        shopify_customer_id=kwargs.pop("shopify_customer_id", None),
        phone=phone,
        synced_at=NOW,
        **kwargs,
    )
    db.add(customer)
    db.commit()
    return customer


def _order(db, org_id, customer_id, **kwargs) -> Order:
    order = Order(
        id=uuid.uuid4(),
        organization_id=org_id,
        shopify_order_id=kwargs.pop("shopify_order_id", 1),
        customer_id=customer_id,
        synced_at=NOW,
        **kwargs,
    )
    db.add(order)
    db.commit()
    return order


def test_no_matching_customer_returns_no_facts(db, org_id):
    assert (
        customer_context.build_customer_facts(db, organization_id=org_id, phone_e164="+14155551234")
        == []
    )


def test_matching_customer_by_exact_e164_phone_returns_their_name(db, org_id):
    _customer(db, org_id, phone="+14155551234", first_name="Jordan", last_name="Lee")
    facts = customer_context.build_customer_facts(
        db, organization_id=org_id, phone_e164="+14155551234"
    )
    assert any("Jordan Lee" in f for f in facts)


def test_matching_customer_without_the_plus_prefix_is_also_found(db, org_id):
    """Shopify-synced phone data is not guaranteed E.164 -- see module docstring."""
    _customer(db, org_id, phone="14155551234", first_name="Sam")
    facts = customer_context.build_customer_facts(
        db, organization_id=org_id, phone_e164="+14155551234"
    )
    assert any("Sam" in f for f in facts)


def test_a_deleted_customer_is_not_matched(db, org_id):
    _customer(db, org_id, phone="+14155551234", first_name="Gone", deleted_at=NOW)
    assert (
        customer_context.build_customer_facts(db, organization_id=org_id, phone_e164="+14155551234")
        == []
    )


def test_another_organizations_customer_is_never_matched(db, org_id):
    other_org = uuid.uuid4()
    db.add(Organization(id=other_org, name="Other Org", slug=f"other-{other_org.hex[:8]}"))
    db.commit()
    _customer(db, other_org, phone="+14155551234", first_name="Other Org Customer")
    assert (
        customer_context.build_customer_facts(db, organization_id=org_id, phone_e164="+14155551234")
        == []
    )


def test_most_recent_order_status_is_included(db, org_id):
    customer = _customer(db, org_id, phone="+14155551234", first_name="Jordan")
    _order(
        db,
        org_id,
        customer.id,
        shopify_order_id=1,
        order_number="#1001",
        fulfillment_status="fulfilled",
    )
    facts = customer_context.build_customer_facts(
        db, organization_id=org_id, phone_e164="+14155551234"
    )
    joined = " ".join(facts)
    assert "#1001" in joined and "fulfilled" in joined


def test_no_order_means_only_the_name_fact(db, org_id):
    _customer(db, org_id, phone="+14155551234", first_name="Jordan")
    facts = customer_context.build_customer_facts(
        db, organization_id=org_id, phone_e164="+14155551234"
    )
    assert len(facts) == 1 and "Jordan" in facts[0]


def test_facts_never_include_full_address_or_payment_details(db, org_id):
    """Read-only, minimal context only -- see module docstring "Non-Goals"."""
    customer = _customer(
        db, org_id, phone="+14155551234", first_name="Jordan", email="jordan@example.com"
    )
    _order(db, org_id, customer.id, order_number="#1001", total=199.99, currency="USD")
    facts = customer_context.build_customer_facts(
        db, organization_id=org_id, phone_e164="+14155551234"
    )
    joined = " ".join(facts)
    assert "jordan@example.com" not in joined
    assert "199.99" not in joined
