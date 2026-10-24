import pytest

from tests.helpers import add_member, signup_and_login


@pytest.fixture()
def team(client, tenants):
    """Brand A with an OWNER, an ADMIN and a MEMBER, plus one unaffiliated registered user."""
    a, _ = tenants
    org_id = a.org["id"]
    people = {"owner": a.headers}
    for role in ("ADMIN", "MEMBER"):
        email = f"{role.lower()}@example.com"
        people[role.lower()] = signup_and_login(client, email)
        assert add_member(client, a.headers, org_id, email, role).status_code == 201
    signup_and_login(client, "outsider@example.com")
    return org_id, people


def test_owner_and_admin_can_list_members_but_member_cannot(client, team):
    org_id, people = team
    url = f"/api/organizations/{org_id}/members"
    owner = client.get(url, headers=people["owner"])
    assert owner.status_code == 200
    assert {(m["email"], m["role"]) for m in owner.json()} == {
        ("a@example.com", "OWNER"),
        ("admin@example.com", "ADMIN"),
        ("member@example.com", "MEMBER"),
    }
    assert client.get(url, headers=people["admin"]).status_code == 200
    assert client.get(url, headers=people["member"]).status_code == 403


def test_member_response_never_leaks_credentials(client, team):
    org_id, people = team
    body = client.get(f"/api/organizations/{org_id}/members", headers=people["owner"]).text
    assert "password" not in body


def test_owner_can_add_admin_and_member(client, team):
    org_id, people = team
    signup_and_login(client, "new-admin@example.com")
    signup_and_login(client, "new-member@example.com")
    r = add_member(client, people["owner"], org_id, "new-admin@example.com", "ADMIN")
    assert r.status_code == 201 and r.json()["role"] == "ADMIN"
    r = add_member(client, people["owner"], org_id, "new-member@example.com", "MEMBER")
    assert r.status_code == 201 and r.json()["role"] == "MEMBER"


def test_role_defaults_to_member(client, team):
    org_id, people = team
    r = client.post(
        f"/api/organizations/{org_id}/members",
        json={"email": "outsider@example.com"},
        headers=people["owner"],
    )
    assert r.status_code == 201 and r.json()["role"] == "MEMBER"


def test_admin_can_add_members_but_not_admins(client, team):
    org_id, people = team
    r = add_member(client, people["admin"], org_id, "outsider@example.com", "ADMIN")
    assert r.status_code == 403  # granting ADMIN is OWNER-only
    r = add_member(client, people["admin"], org_id, "outsider@example.com", "MEMBER")
    assert r.status_code == 201


@pytest.mark.parametrize("role", ["ADMIN", "MEMBER"])
def test_plain_member_cannot_add_anyone(client, team, role):
    org_id, people = team
    r = add_member(client, people["member"], org_id, "outsider@example.com", role)
    assert r.status_code == 403


def test_owner_role_cannot_be_granted(client, team):
    org_id, people = team
    r = add_member(client, people["owner"], org_id, "outsider@example.com", "OWNER")
    assert r.status_code == 422


def test_invalid_role_rejected(client, team):
    org_id, people = team
    r = add_member(client, people["owner"], org_id, "outsider@example.com", "SUPERUSER")
    assert r.status_code == 422


def test_duplicate_membership_rejected(client, team):
    org_id, people = team
    r = add_member(client, people["owner"], org_id, "member@example.com", "MEMBER")
    assert r.status_code == 409
    # not even a role change through the back door
    r = add_member(client, people["owner"], org_id, "member@example.com", "ADMIN")
    assert r.status_code == 409
    # the owner can't be re-added either
    assert add_member(client, people["owner"], org_id, "a@example.com", "MEMBER").status_code == 409


def test_adding_unknown_email_is_404(client, team):
    org_id, people = team
    r = add_member(client, people["owner"], org_id, "ghost@example.com", "MEMBER")
    assert r.status_code == 404


def test_unauthorized_caller_learns_nothing_about_accounts(client, team):
    org_id, people = team
    known = add_member(client, people["member"], org_id, "outsider@example.com", "MEMBER")
    unknown = add_member(client, people["member"], org_id, "ghost@example.com", "MEMBER")
    assert known.status_code == unknown.status_code == 403
    assert known.json() == unknown.json()


def test_added_email_is_normalized(client, team):
    org_id, people = team
    r = add_member(client, people["owner"], org_id, "OUTSIDER@Example.COM", "MEMBER")
    assert r.status_code == 201 and r.json()["email"] == "outsider@example.com"
