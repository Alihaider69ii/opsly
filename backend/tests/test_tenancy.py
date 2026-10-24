import uuid

import pytest
from sqlalchemy import ForeignKey, String, Uuid, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models import OrganizationMember, OrganizationRole, User
from app.db.scoping import scoped_select
from tests.helpers import add_member, create_org, signup_and_login

# --- reading ------------------------------------------------------------------------------


def test_each_user_can_access_their_own_organization(client, tenants):
    a, b = tenants
    for tenant in (a, b):
        r = client.get(f"/api/organizations/{tenant.org['id']}", headers=tenant.headers)
        assert r.status_code == 200
        assert r.json()["id"] == tenant.org["id"]
        assert r.json()["role"] == "OWNER"


def test_user_cannot_read_another_organization(client, tenants):
    a, b = tenants
    r = client.get(f"/api/organizations/{b.org['id']}", headers=a.headers)
    assert r.status_code == 404
    assert b.org["name"] not in r.text


def test_foreign_and_nonexistent_organizations_are_indistinguishable(client, tenants):
    a, b = tenants
    foreign = client.get(f"/api/organizations/{b.org['id']}", headers=a.headers)
    missing = client.get(f"/api/organizations/{uuid.uuid4()}", headers=a.headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_list_only_returns_own_organizations(client, tenants):
    a, b = tenants
    ids = [o["id"] for o in client.get("/api/organizations", headers=a.headers).json()]
    assert ids == [a.org["id"]]


def test_members_listing_is_scoped_to_the_organization(client, tenants):
    a, b = tenants
    emails = [
        m["email"]
        for m in client.get(f"/api/organizations/{a.org['id']}/members", headers=a.headers).json()
    ]
    assert emails == [a.email]


def test_cannot_list_members_of_another_organization(client, tenants):
    a, b = tenants
    r = client.get(f"/api/organizations/{b.org['id']}/members", headers=a.headers)
    assert r.status_code == 404
    assert b.email not in r.text


# --- impersonation / tampering ------------------------------------------------------------


def test_cannot_add_membership_to_an_organization_you_do_not_control(client, db, tenants):
    a, b = tenants
    signup_and_login(client, "victim@example.com")
    before = db.scalar(select(func.count()).select_from(OrganizationMember))

    # a tries to add themselves and a third party into b's org
    for email in (a.email, "victim@example.com"):
        r = add_member(client, a.headers, b.org["id"], email, "MEMBER")
        assert r.status_code == 404
    r = add_member(client, a.headers, b.org["id"], a.email, "ADMIN")
    assert r.status_code == 404

    assert db.scalar(select(func.count()).select_from(OrganizationMember)) == before
    a_user = db.scalar(select(User).where(User.email == a.email))
    in_b = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == a_user.id,
            OrganizationMember.organization_id == uuid.UUID(b.org["id"]),
        )
    )
    assert in_b is None


def test_organization_id_header_cannot_select_a_foreign_organization(client, tenants):
    a, b = tenants
    r = client.get(
        "/api/organizations/current", headers={**a.headers, "X-Organization-ID": b.org["id"]}
    )
    assert r.status_code == 404
    assert b.org["name"] not in r.text


def test_organization_id_header_can_select_among_own_organizations(client, tenants):
    a, _ = tenants
    second = create_org(client, a.headers, "Brand A Two")
    default = client.get("/api/organizations/current", headers=a.headers).json()
    chosen = client.get(
        "/api/organizations/current", headers={**a.headers, "X-Organization-ID": second["id"]}
    ).json()
    assert default["id"] == a.org["id"]
    assert chosen["id"] == second["id"]


def test_malformed_organization_id_header_rejected(client, tenants):
    a, _ = tenants
    r = client.get(
        "/api/organizations/current", headers={**a.headers, "X-Organization-ID": "not-a-uuid"}
    )
    assert r.status_code == 422


def test_membership_in_one_org_grants_nothing_in_another(client, tenants):
    a, b = tenants
    assert add_member(client, b.headers, b.org["id"], a.email, "MEMBER").status_code == 201
    # a now belongs to B as MEMBER, and still has no access to anything else of B's beyond that
    assert client.get(f"/api/organizations/{b.org['id']}", headers=a.headers).json()["role"] == (
        "MEMBER"
    )
    assert (
        client.get(f"/api/organizations/{b.org['id']}/members", headers=a.headers).status_code
        == 403
    )
    # ...and B's owner still cannot see A's own org
    assert client.get(f"/api/organizations/{a.org['id']}", headers=b.headers).status_code == 404


def test_roles_are_per_organization(client, tenants):
    a, b = tenants
    add_member(client, b.headers, b.org["id"], a.email, "MEMBER")
    as_owner = client.get(f"/api/organizations/{a.org['id']}/members", headers=a.headers)
    as_member = client.get(f"/api/organizations/{b.org['id']}/members", headers=a.headers)
    assert as_owner.status_code == 200
    assert as_member.status_code == 403


# --- unauthenticated ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/auth/me"),
        ("POST", "/api/organizations"),
        ("GET", "/api/organizations"),
        ("GET", "/api/organizations/current"),
        ("GET", "/api/organizations/{org}"),
        ("GET", "/api/organizations/{org}/members"),
        ("POST", "/api/organizations/{org}/members"),
    ],
)
def test_every_protected_endpoint_rejects_unauthenticated_requests(client, tenants, method, path):
    a, _ = tenants
    r = client.request(method, path.format(org=a.org["id"]), json={})
    assert r.status_code == 401
    assert a.email not in r.text


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


# --- the scoped-query helper --------------------------------------------------------------


class TenantNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Stand-in for a future business table (products, conversations, ...)."""

    __tablename__ = "test_tenant_notes"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(String(100))


def test_scoped_select_only_returns_rows_of_that_organization(client, db, tenants):
    a, b = tenants
    org_a, org_b = uuid.UUID(a.org["id"]), uuid.UUID(b.org["id"])
    note_a = TenantNote(organization_id=org_a, body="a-secret")
    note_b = TenantNote(organization_id=org_b, body="b-secret")
    db.add_all([note_a, note_b])
    db.commit()

    assert [n.body for n in db.scalars(scoped_select(TenantNote, org_a))] == ["a-secret"]
    assert [n.body for n in db.scalars(scoped_select(TenantNote, org_b))] == ["b-secret"]


def test_scoped_select_blocks_lookup_by_id_across_organizations(client, db, tenants):
    a, b = tenants
    org_a, org_b = uuid.UUID(a.org["id"]), uuid.UUID(b.org["id"])
    note_b = TenantNote(organization_id=org_b, body="b-secret")
    db.add(note_b)
    db.commit()

    # Knowing B's row id is not enough when querying through A's scope (IDOR protection).
    stolen = db.scalar(scoped_select(TenantNote, org_a).where(TenantNote.id == note_b.id))
    legit = db.scalar(scoped_select(TenantNote, org_b).where(TenantNote.id == note_b.id))
    assert stolen is None
    assert legit is not None


def test_scoped_select_rejects_models_without_organization_id():
    with pytest.raises(TypeError, match="not tenant-scoped"):
        scoped_select(User, uuid.uuid4())


def test_organization_creator_is_the_only_owner(client, db, tenants):
    a, _ = tenants
    owners = db.scalars(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == uuid.UUID(a.org["id"]),
            OrganizationMember.role == OrganizationRole.OWNER,
        )
    ).all()
    assert len(owners) == 1
    assert owners[0].user.email == a.email
