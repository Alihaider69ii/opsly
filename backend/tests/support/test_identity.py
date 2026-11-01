from app.db.models import Customer
from app.services.support import identity as identity_service


def test_normalize_email_lowercases_and_trims():
    assert identity_service.normalize_email("  Sam@Example.COM ") == "sam@example.com"


def test_normalize_email_rejects_non_email_shapes():
    assert identity_service.normalize_email("not-an-email") is None
    assert identity_service.normalize_email("@example.com") is None
    assert identity_service.normalize_email(None) is None
    assert identity_service.normalize_email("") is None


def test_normalize_phone_requires_default_country_for_ambiguous_numbers():
    assert identity_service.normalize_phone_for_identity("4155551234") is None
    assert (
        identity_service.normalize_phone_for_identity("4155551234", default_country="US")
        == "+14155551234"
    )


def test_get_or_create_customer_matches_by_normalized_email_not_raw_string(db, org_id):
    first = identity_service.get_or_create_customer(db, organization_id=org_id, email="Sam@Example.com")
    db.commit()
    second = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()
    assert first.id == second.id


def test_get_or_create_customer_matches_by_phone_when_email_absent(db, org_id):
    first = identity_service.get_or_create_customer(
        db, organization_id=org_id, phone="+14155551234", name="Sam"
    )
    db.commit()
    second = identity_service.get_or_create_customer(
        db, organization_id=org_id, phone="+14155551234"
    )
    db.commit()
    assert first.id == second.id


def test_shopify_customer_id_takes_priority_over_email(db, org_id):
    """Matching order is fixed: Shopify id, then email, then phone -- never by name."""
    existing = Customer(
        organization_id=org_id,
        shopify_customer_id=4242,
        email="old@example.com",
        normalized_email="old@example.com",
    )
    db.add(existing)
    db.commit()

    found = identity_service.find_customer(
        db, organization_id=org_id, shopify_customer_id=4242, email="new@example.com"
    )
    assert found is not None
    assert found.id == existing.id  # matched by Shopify id, not the different email


def test_identity_matching_never_crosses_tenants(db, org_id, session_factory):
    import uuid

    from app.db.models import Organization

    other_org = uuid.uuid4()
    with session_factory() as other_db:
        other_db.add(Organization(id=other_org, name="Other", slug=f"other-{other_org.hex[:8]}"))
        other_db.commit()

    identity_service.get_or_create_customer(db, organization_id=org_id, email="shared@example.com")
    db.commit()

    found_in_other_org = identity_service.find_customer(
        db, organization_id=other_org, email="shared@example.com"
    )
    assert found_in_other_org is None


def test_get_or_create_backfills_previously_unknown_identity_without_overwriting_existing(db, org_id):
    customer = identity_service.get_or_create_customer(db, organization_id=org_id, email="sam@example.com")
    db.commit()
    assert customer.phone is None

    updated = identity_service.get_or_create_customer(
        db, organization_id=org_id, email="sam@example.com", phone="+14155551234"
    )
    db.commit()
    assert updated.id == customer.id
    assert updated.phone == "+14155551234"  # backfilled

    # A second, different email for the same matched row must never overwrite the first email --
    # backfill only fills genuinely-empty fields.
    again = identity_service.get_or_create_customer(
        db, organization_id=org_id, phone="+14155551234", email="different@example.com"
    )
    db.commit()
    assert again.id == customer.id
    assert again.email == "sam@example.com"
