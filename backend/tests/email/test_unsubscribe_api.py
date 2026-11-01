from app.core.config import get_settings
from app.db.models import Customer
from app.services.email import unsubscribe as unsubscribe_service
from tests.email.conftest import seed_customer


def _token_for(org_id, customer_id):
    settings = get_settings()
    key = unsubscribe_service.derive_key(settings.secret_encryption_key.get_secret_value())
    return unsubscribe_service.build_unsubscribe_url(
        base_url=settings.email_unsubscribe_base_url, organization_id=org_id, customer_id=customer_id, key=key
    ).split("token=", 1)[1]


def test_a_valid_token_suppresses_the_customer(client, org_id, session_factory, db):
    customer_id = seed_customer(session_factory, org_id, email="sam@example.com")
    token = _token_for(org_id, customer_id)

    response = client.get(f"/api/email/unsubscribe?token={token}")

    assert response.status_code == 200
    db.expire_all()
    customer = db.get(Customer, customer_id)
    assert customer.email_suppressed is True
    assert customer.email_marketing_consent is False


def test_calling_the_link_twice_is_a_safe_no_op(client, org_id, session_factory, db):
    customer_id = seed_customer(session_factory, org_id, email="sam@example.com")
    token = _token_for(org_id, customer_id)

    first = client.get(f"/api/email/unsubscribe?token={token}")
    second = client.get(f"/api/email/unsubscribe?token={token}")

    assert first.status_code == 200
    assert second.status_code == 200
    db.expire_all()
    customer = db.get(Customer, customer_id)
    assert customer.email_suppressed is True


def test_an_invalid_token_is_rejected(client):
    response = client.get("/api/email/unsubscribe?token=not-a-real-token")
    assert response.status_code == 400


def test_a_tampered_token_is_rejected(client, org_id, session_factory):
    import uuid

    customer_id = seed_customer(session_factory, org_id, email="sam@example.com")
    token = _token_for(org_id, customer_id)
    payload_part, signature_part = token.split(".", 1)
    other_token = _token_for(uuid.uuid4(), customer_id)
    forged = other_token.split(".", 1)[0] + "." + signature_part

    response = client.get(f"/api/email/unsubscribe?token={forged}")
    assert response.status_code == 400


def test_a_token_for_a_customer_that_no_longer_exists_returns_404(client, org_id):
    import uuid

    token = _token_for(org_id, uuid.uuid4())
    response = client.get(f"/api/email/unsubscribe?token={token}")
    assert response.status_code == 404


def test_a_token_cannot_be_replayed_against_a_different_organizations_customer_with_the_same_id(
    client, org_id, session_factory
):
    """The token binds a specific (org, customer) pair -- a customer id alone is never enough."""
    import uuid

    from app.db.models import Organization

    other_org = uuid.uuid4()
    with session_factory() as other_db:
        other_db.add(Organization(id=other_org, name="Other", slug=f"other-{other_org.hex[:8]}"))
        other_db.commit()
    customer_id = seed_customer(session_factory, org_id, email="sam@example.com")

    token_for_other_org = _token_for(other_org, customer_id)
    response = client.get(f"/api/email/unsubscribe?token={token_for_other_org}")
    assert response.status_code == 404
