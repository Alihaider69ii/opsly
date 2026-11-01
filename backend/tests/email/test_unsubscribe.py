import uuid

from app.services.email import unsubscribe as unsubscribe_service

KEY = unsubscribe_service.derive_key("test-secret")
OTHER_KEY = unsubscribe_service.derive_key("a-different-secret")


def test_a_generated_token_verifies_back_to_the_same_org_and_customer():
    org_id, customer_id = uuid.uuid4(), uuid.uuid4()
    token = unsubscribe_service.generate_token(organization_id=org_id, customer_id=customer_id, key=KEY)
    resolved = unsubscribe_service.verify_token(token, key=KEY)
    assert resolved == (org_id, customer_id)


def test_a_token_signed_with_a_different_key_is_rejected():
    org_id, customer_id = uuid.uuid4(), uuid.uuid4()
    token = unsubscribe_service.generate_token(organization_id=org_id, customer_id=customer_id, key=KEY)
    assert unsubscribe_service.verify_token(token, key=OTHER_KEY) is None


def test_a_tampered_payload_is_rejected():
    org_id, customer_id = uuid.uuid4(), uuid.uuid4()
    token = unsubscribe_service.generate_token(organization_id=org_id, customer_id=customer_id, key=KEY)
    payload_part, signature_part = token.split(".", 1)
    tampered = unsubscribe_service.generate_token(
        organization_id=uuid.uuid4(), customer_id=customer_id, key=KEY
    ).split(".", 1)[0]
    forged = tampered + "." + signature_part
    assert unsubscribe_service.verify_token(forged, key=KEY) is None


def test_malformed_tokens_are_rejected_without_raising():
    assert unsubscribe_service.verify_token("not-a-token", key=KEY) is None
    assert unsubscribe_service.verify_token("", key=KEY) is None
    assert unsubscribe_service.verify_token("..", key=KEY) is None
    assert unsubscribe_service.verify_token("###.###", key=KEY) is None


def test_build_unsubscribe_url_embeds_a_verifiable_token():
    org_id, customer_id = uuid.uuid4(), uuid.uuid4()
    url = unsubscribe_service.build_unsubscribe_url(
        base_url="https://app.example.com", organization_id=org_id, customer_id=customer_id, key=KEY
    )
    assert url.startswith("https://app.example.com/api/email/unsubscribe?token=")
    token = url.split("token=", 1)[1]
    assert unsubscribe_service.verify_token(token, key=KEY) == (org_id, customer_id)


def test_build_unsubscribe_url_strips_a_trailing_slash_on_the_base_url():
    org_id, customer_id = uuid.uuid4(), uuid.uuid4()
    url = unsubscribe_service.build_unsubscribe_url(
        base_url="https://app.example.com/", organization_id=org_id, customer_id=customer_id, key=KEY
    )
    assert "//api" not in url.replace("https://", "")


def test_derive_key_is_deterministic_and_secret_specific():
    assert unsubscribe_service.derive_key("same") == unsubscribe_service.derive_key("same")
    assert unsubscribe_service.derive_key("a") != unsubscribe_service.derive_key("b")
