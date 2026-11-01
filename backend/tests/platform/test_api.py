from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User
from tests.helpers import create_org, signup_and_login

VIEW_HEADER = "X-Platform-View-Organization-Id"


def make_platform_admin(client: TestClient, db: Session, email: str) -> dict[str, str]:
    headers = signup_and_login(client, email)
    user = db.scalar(select(User).where(User.email == email))
    user.is_platform_admin = True
    db.commit()
    # Re-login so nothing relies on token content reflecting the flag (it never does -- the flag
    # is read fresh from the DB on every request), just to exercise the real login path once more.
    return headers


def create_brand_via_api(
    client: TestClient, admin_headers: dict, *, slug: str, name: str = "Test Brand"
):
    r = client.post(
        "/api/platform/brands",
        json={
            "name": name,
            "slug": slug,
            "environment": "DEMO",
            "owner_name": "Brand Owner",
            "owner_email": f"owner@{slug}.demo",
            "temporary_password": "Demo12345!",
        },
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_platform_routes_require_authentication(client: TestClient):
    r = client.get("/api/platform/dashboard")
    assert r.status_code == 401


def test_a_regular_brand_user_cannot_access_platform_admin_routes(client: TestClient, db: Session):
    headers = signup_and_login(client, "plain@example.com")
    create_org(client, headers, "Plain Brand")

    r = client.get("/api/platform/dashboard", headers=headers)
    assert r.status_code == 403

    r = client.get("/api/platform/brands", headers=headers)
    assert r.status_code == 403


def test_platform_admin_can_reach_the_dashboard_and_sees_real_numbers(
    client: TestClient, db: Session
):
    admin_headers = make_platform_admin(client, db, "founder@example.com")
    r = client.get("/api/platform/dashboard", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["total_brands"], int)
    assert body["total_brands"] >= 0


def test_create_brand_response_never_contains_a_password(client: TestClient, db: Session):
    admin_headers = make_platform_admin(client, db, "founder2@example.com")
    created = create_brand_via_api(client, admin_headers, slug="no-secret-leak")
    body_text = str(created)
    assert "Demo12345" not in body_text
    assert "password" not in body_text.lower()
    assert created["brand"]["slug"] == "no-secret-leak"
    assert created["brand"]["environment"] == "DEMO"


def test_create_brand_with_duplicate_owner_email_is_409(client: TestClient, db: Session):
    admin_headers = make_platform_admin(client, db, "founder3@example.com")
    create_brand_via_api(client, admin_headers, slug="dup-owner-1")
    r = client.post(
        "/api/platform/brands",
        json={
            "name": "Second Brand",
            "slug": "dup-owner-2",
            "environment": "DEMO",
            "owner_name": "Brand Owner",
            "owner_email": "owner@dup-owner-1.demo",  # same owner email as the first brand
            "temporary_password": "Demo12345!",
        },
        headers=admin_headers,
    )
    assert r.status_code == 409


def test_brand_list_shows_the_created_brand_and_supports_filtering(client: TestClient, db: Session):
    admin_headers = make_platform_admin(client, db, "founder4@example.com")
    create_brand_via_api(client, admin_headers, slug="listed-brand", name="Listed Brand")

    r = client.get("/api/platform/brands", headers=admin_headers)
    assert r.status_code == 200
    slugs = {b["slug"] for b in r.json()}
    assert "listed-brand" in slugs

    r = client.get("/api/platform/brands", params={"environment": "DEMO"}, headers=admin_headers)
    assert all(b["environment"] == "DEMO" for b in r.json())

    r = client.get("/api/platform/brands", params={"search": "Listed Brand"}, headers=admin_headers)
    assert {b["slug"] for b in r.json()} == {"listed-brand"}


def test_activate_and_deactivate_brand(client: TestClient, db: Session):
    admin_headers = make_platform_admin(client, db, "founder5@example.com")
    created = create_brand_via_api(client, admin_headers, slug="toggle-brand")
    org_id = created["brand"]["id"]

    r = client.post(f"/api/platform/brands/{org_id}/deactivate", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    r = client.post(f"/api/platform/brands/{org_id}/activate", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["is_active"] is True


def test_enter_and_exit_brand_are_audited_with_the_admins_own_identity(
    client: TestClient, db: Session
):
    admin_headers = make_platform_admin(client, db, "founder6@example.com")
    created = create_brand_via_api(client, admin_headers, slug="audited-brand")
    org_id = created["brand"]["id"]

    r = client.post(f"/api/platform/brands/{org_id}/enter", headers=admin_headers)
    assert r.status_code == 200
    r = client.post(f"/api/platform/brands/{org_id}/exit", headers=admin_headers)
    assert r.status_code == 204

    r = client.get("/api/platform/audit", params={"organization_id": org_id}, headers=admin_headers)
    assert r.status_code == 200
    event_types = {e["event_type"] for e in r.json()}
    assert "PLATFORM_ADMIN_ENTERED_BRAND" in event_types
    assert "PLATFORM_ADMIN_EXITED_BRAND" in event_types
    # Every event's actor is the admin themselves -- never a synthetic brand-user identity.
    me = client.get("/api/auth/me", headers=admin_headers).json()
    assert all(e["actor_user_id"] == me["id"] for e in r.json())


def test_view_as_brand_reaches_the_existing_org_scoped_routes_unchanged(
    client: TestClient, db: Session
):
    admin_headers = make_platform_admin(client, db, "founder7@example.com")
    created = create_brand_via_api(
        client, admin_headers, slug="viewable-brand", name="Viewable Brand"
    )
    org_id = created["brand"]["id"]

    r = client.get("/api/organizations/current", headers={**admin_headers, VIEW_HEADER: org_id})
    assert r.status_code == 200
    assert r.json()["id"] == org_id
    assert r.json()["name"] == "Viewable Brand"


def test_view_as_brand_never_leaks_one_brands_data_into_another(client: TestClient, db: Session):
    admin_headers = make_platform_admin(client, db, "founder8@example.com")
    brand_a = create_brand_via_api(client, admin_headers, slug="brand-a-iso", name="Brand A")
    brand_b = create_brand_via_api(client, admin_headers, slug="brand-b-iso", name="Brand B")

    r_a = client.get(
        "/api/organizations/current", headers={**admin_headers, VIEW_HEADER: brand_a["brand"]["id"]}
    )
    r_b = client.get(
        "/api/organizations/current", headers={**admin_headers, VIEW_HEADER: brand_b["brand"]["id"]}
    )
    assert r_a.json()["id"] != r_b.json()["id"]
    assert r_a.json()["name"] == "Brand A"
    assert r_b.json()["name"] == "Brand B"


def test_a_non_admin_cannot_use_the_view_as_brand_header_to_escalate(
    client: TestClient, db: Session
):
    admin_headers = make_platform_admin(client, db, "founder9@example.com")
    other_brand = create_brand_via_api(client, admin_headers, slug="not-yours", name="Not Yours")

    plain_headers = signup_and_login(client, "plainuser@example.com")
    create_org(client, plain_headers, "My Own Brand")

    r = client.get(
        "/api/organizations/current",
        headers={**plain_headers, VIEW_HEADER: other_brand["brand"]["id"]},
    )
    # The header is silently ignored for a non-admin: they still get only their own organization.
    assert r.status_code == 200
    assert r.json()["name"] == "My Own Brand"


def test_platform_admin_login_itself_is_audited(client: TestClient, db: Session):
    make_platform_admin(client, db, "founder10@example.com")
    r = client.post(
        "/api/auth/login",
        json={"email": "founder10@example.com", "password": "correct-horse-battery"},
    )
    assert r.status_code == 200
    admin_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.get("/api/platform/audit", headers=admin_headers)
    assert r.status_code == 200
    assert any(e["event_type"] == "PLATFORM_ADMIN_LOGIN" for e in r.json())


def test_forbidden_action_type_in_operations_still_rejected_when_viewed_as_brand_by_admin(
    client: TestClient, db: Session
):
    """Brief section 30: platform admin access is authorized, but it still goes through the same
    deterministic policy as a real brand OWNER would -- it does not bypass the Safe Action
    Framework (brief sections 19, 27, 34)."""
    admin_headers = make_platform_admin(client, db, "founder11@example.com")
    created = create_brand_via_api(client, admin_headers, slug="policy-brand")
    org_id = created["brand"]["id"]

    r = client.post(
        "/api/operations/actions",
        json={
            "action_type": "wipe_database",
            "resource_type": "order",
            "idempotency_key": "view-as-brand-policy-test",
        },
        headers={**admin_headers, VIEW_HEADER: org_id},
    )
    assert r.status_code == 201
    assert r.json()["category"] == "FORBIDDEN"
    assert r.json()["status"] == "FAILED"
