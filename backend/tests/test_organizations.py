import pytest
from sqlalchemy import func, select

from app.db.models import Organization, OrganizationMember, OrganizationRole, User
from tests.helpers import create_org, signup_and_login


def test_create_organization_makes_creator_owner(client, db):
    headers = signup_and_login(client, "ali@example.com")
    r = client.post(
        "/api/organizations",
        json={
            "name": "L'avenir Skins",
            "website": "https://example.com",
            "business_type": "beauty_skincare",
        },
        headers=headers,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "L'avenir Skins"
    assert body["slug"] == "l-avenir-skins"
    assert body["website"] == "https://example.com/"
    assert body["business_type"] == "beauty_skincare"
    assert body["role"] == "OWNER"

    # membership row created in the same transaction, linked to the right user and org
    user = db.scalar(select(User).where(User.email == "ali@example.com"))
    membership = db.scalar(select(OrganizationMember))
    assert membership.user_id == user.id
    assert str(membership.organization_id) == body["id"]
    assert membership.role == OrganizationRole.OWNER
    assert db.scalar(select(func.count()).select_from(OrganizationMember)) == 1


def test_only_name_is_required(client):
    headers = signup_and_login(client, "ali@example.com")
    org = create_org(client, headers, "Bare Minimum")
    assert org["website"] is None and org["business_type"] is None


def test_blank_optional_fields_become_null(client):
    headers = signup_and_login(client, "ali@example.com")
    org = create_org(client, headers, "Blanks", website="", business_type="  ", slug="")
    assert org["website"] is None and org["business_type"] is None
    assert org["slug"] == "blanks"


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_invalid_name_rejected(client, name):
    headers = signup_and_login(client, "ali@example.com")
    r = client.post("/api/organizations", json={"name": name}, headers=headers)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "website", ["ftp://example.com", "javascript:alert(1)", "not a url", "example.com"]
)
def test_invalid_website_rejected(client, website):
    headers = signup_and_login(client, "ali@example.com")
    r = client.post("/api/organizations", json={"name": "X", "website": website}, headers=headers)
    assert r.status_code == 422


def test_generated_slugs_are_unique(client):
    h1 = signup_and_login(client, "one@example.com")
    h2 = signup_and_login(client, "two@example.com")
    first = create_org(client, h1, "Glow Co")
    second = create_org(client, h2, "Glow Co")
    assert first["slug"] == "glow-co"
    assert second["slug"] != first["slug"]
    assert second["slug"].startswith("glow-co-")


def test_explicit_slug_is_normalized_and_must_be_unique(client):
    h1 = signup_and_login(client, "one@example.com")
    h2 = signup_and_login(client, "two@example.com")
    assert create_org(client, h1, "A", slug="My-Brand")["slug"] == "my-brand"
    r = client.post("/api/organizations", json={"name": "B", "slug": "my-brand"}, headers=h2)
    assert r.status_code == 409


@pytest.mark.parametrize("slug", ["ab", "has space", "under_score", "-lead", "trail-", "x" * 51])
def test_malformed_slug_rejected(client, slug):
    headers = signup_and_login(client, "ali@example.com")
    r = client.post("/api/organizations", json={"name": "A", "slug": slug}, headers=headers)
    assert r.status_code == 422


def test_reserved_slug_rejected(client):
    headers = signup_and_login(client, "ali@example.com")
    r = client.post("/api/organizations", json={"name": "A", "slug": "admin"}, headers=headers)
    assert r.status_code == 409


def test_name_that_is_a_reserved_word_still_gets_a_slug(client):
    headers = signup_and_login(client, "ali@example.com")
    org = create_org(client, headers, "Admin")
    assert org["slug"] != "admin" and org["slug"].startswith("admin-")


def test_slug_conflict_creates_nothing(client, db):
    h1 = signup_and_login(client, "one@example.com")
    h2 = signup_and_login(client, "two@example.com")
    create_org(client, h1, "A", slug="taken")
    client.post("/api/organizations", json={"name": "B", "slug": "taken"}, headers=h2)
    assert db.scalar(select(func.count()).select_from(Organization)) == 1
    assert db.scalar(select(func.count()).select_from(OrganizationMember)) == 1


def test_non_latin_name_gets_a_valid_slug(client):
    headers = signup_and_login(client, "ali@example.com")
    org = create_org(client, headers, "株式会社")
    assert org["slug"] == "business"


def test_create_organization_requires_authentication(client):
    r = client.post("/api/organizations", json={"name": "Nope"})
    assert r.status_code == 401


def test_user_can_belong_to_multiple_organizations(client):
    headers = signup_and_login(client, "ali@example.com")
    create_org(client, headers, "First")
    create_org(client, headers, "Second")
    names = [o["name"] for o in client.get("/api/organizations", headers=headers).json()]
    assert names == ["First", "Second"]


def test_current_org_defaults_to_earliest_membership(client):
    headers = signup_and_login(client, "ali@example.com")
    first = create_org(client, headers, "First")
    create_org(client, headers, "Second")
    r = client.get("/api/organizations/current", headers=headers)
    assert r.status_code == 200 and r.json()["id"] == first["id"]


def test_current_org_for_user_without_organization(client):
    headers = signup_and_login(client, "ali@example.com")
    assert client.get("/api/organizations", headers=headers).json() == []
    assert client.get("/api/organizations/current", headers=headers).status_code == 403
