"""Authentication, role and tenant isolation, checked for every route of the API.

The policy table below is the single statement of who may do what. A test fails if the application
has a route that is not in the table (so a new route cannot ship unclassified), if a protected route
does not declare authentication, or if any role/tenant combination gets a different answer than the
table says.

Routes that are public by design carry their own authenticity mechanism (an HMAC signature, a
public chat key plus conversation token, or no data at all) and are tested where they are defined.
"""

import uuid
from dataclasses import dataclass

import pytest

from app.main import app
from tests.helpers import add_member, create_org, signup_and_login

PUBLIC = "public"  # no login; authenticity is established another way (see PUBLIC_REASON)
USER = "user"  # any logged-in account; not tied to one organization
ANY_MEMBER = "any-member"  # OWNER, ADMIN or MEMBER of the organization acted on
MANAGER = "manager"  # OWNER or ADMIN only

HEADER = "header"  # organization chosen with X-Organization-ID
PATH = "path"  # organization is in the URL

SOURCE = "{source_id}"
DOCUMENT = "{document_id}"
WORKFLOW = "{workflow_id}"
EXECUTION = "{execution_id}"
ORG = "{organization_id}"

# (method, path) -> (who may call, how the organization is named, request body)
POLICY: dict[tuple[str, str], tuple[str, str | None, dict | None]] = {
    ("POST", "/api/auth/login"): (PUBLIC, None, None),
    ("POST", "/api/auth/register"): (PUBLIC, None, None),
    ("GET", "/api/auth/me"): (USER, None, None),
    ("GET", "/api/organizations"): (USER, None, None),
    ("POST", "/api/organizations"): (USER, None, {"name": "Another Brand"}),
    ("GET", "/api/organizations/current"): (ANY_MEMBER, HEADER, None),
    ("GET", f"/api/organizations/{ORG}"): (ANY_MEMBER, PATH, None),
    ("GET", f"/api/organizations/{ORG}/members"): (MANAGER, PATH, None),
    ("POST", f"/api/organizations/{ORG}/members"): (
        MANAGER,
        PATH,
        {"email": "nobody@nowhere.example", "role": "MEMBER"},
    ),
    ("GET", "/api/chat/widget"): (ANY_MEMBER, HEADER, None),
    ("PATCH", "/api/chat/widget"): (MANAGER, HEADER, {"enabled": True}),
    ("GET", "/api/integrations/shopify"): (ANY_MEMBER, HEADER, None),
    ("POST", "/api/integrations/shopify/connect"): (
        MANAGER,
        HEADER,
        {"shop": "demo-store.myshopify.com"},
    ),
    ("GET", "/api/integrations/shopify/callback"): (MANAGER, HEADER, None),
    ("POST", "/api/integrations/shopify/sync"): (MANAGER, HEADER, None),
    ("POST", "/api/integrations/shopify/disconnect"): (MANAGER, HEADER, None),
    ("GET", "/api/knowledge/sources"): (ANY_MEMBER, HEADER, None),
    ("POST", "/api/knowledge/sources"): (
        MANAGER,
        HEADER,
        {"type": "WEBSITE", "url": "https://example.com", "name": "Site"},
    ),
    ("GET", f"/api/knowledge/sources/{SOURCE}"): (ANY_MEMBER, HEADER, None),
    ("DELETE", f"/api/knowledge/sources/{SOURCE}"): (MANAGER, HEADER, None),
    ("POST", f"/api/knowledge/sources/{SOURCE}/crawl"): (MANAGER, HEADER, None),
    ("GET", f"/api/knowledge/sources/{SOURCE}/documents"): (ANY_MEMBER, HEADER, None),
    ("GET", f"/api/knowledge/sources/{SOURCE}/documents/{DOCUMENT}"): (ANY_MEMBER, HEADER, None),
    ("GET", "/api/public/chat/{public_key}/config"): (PUBLIC, None, None),
    ("POST", "/api/public/chat/{public_key}/conversations"): (PUBLIC, None, None),
    ("GET", "/api/public/chat/{public_key}/conversations/{conversation_id}"): (PUBLIC, None, None),
    ("POST", "/api/public/chat/{public_key}/messages"): (PUBLIC, None, None),
    ("POST", "/api/public/chat/{public_key}/leads"): (PUBLIC, None, None),
    ("GET", "/api/automations"): (ANY_MEMBER, HEADER, None),
    ("POST", "/api/automations"): (
        MANAGER,
        HEADER,
        {
            "name": "Test",
            "definition": {
                "trigger": {"type": "customer_created"},
                "conditions": [],
                "actions": [{"type": "log"}],
            },
        },
    ),
    ("GET", f"/api/automations/{WORKFLOW}"): (ANY_MEMBER, HEADER, None),
    ("PATCH", f"/api/automations/{WORKFLOW}"): (MANAGER, HEADER, {"name": "Renamed"}),
    ("POST", f"/api/automations/{WORKFLOW}/activate"): (MANAGER, HEADER, None),
    ("POST", f"/api/automations/{WORKFLOW}/pause"): (MANAGER, HEADER, None),
    ("POST", f"/api/automations/{WORKFLOW}/archive"): (MANAGER, HEADER, None),
    ("GET", f"/api/automations/{WORKFLOW}/executions"): (ANY_MEMBER, HEADER, None),
    ("GET", f"/api/automations/executions/{EXECUTION}"): (ANY_MEMBER, HEADER, None),
    ("POST", "/api/webhooks/shopify"): (PUBLIC, None, None),
    ("GET", "/health"): (PUBLIC, None, None),
    ("GET", "/health/live"): (PUBLIC, None, None),
    ("GET", "/health/ready"): (PUBLIC, None, None),
    ("GET", "/health/worker"): (PUBLIC, None, None),
    ("GET", "/health/db"): (PUBLIC, None, None),
}

PUBLIC_REASON = {
    "/api/auth/login": "credentials are the proof",
    "/api/auth/register": "creates an account; nothing to read",
    "/api/public/chat/": "the widget's public key + a per-conversation secret token",
    "/api/webhooks/shopify": "HMAC-SHA256 over the raw body with the app secret",
    "/health": "liveness/readiness carry no data about tenants",
}

PROTECTED = {key: value for key, value in POLICY.items() if value[0] != PUBLIC}
ORG_ROUTES = {key: value for key, value in POLICY.items() if value[1] in (HEADER, PATH)}
MANAGER_ROUTES = {key: value for key, value in ORG_ROUTES.items() if value[0] == MANAGER}
MEMBER_ROUTES = {key: value for key, value in ORG_ROUTES.items() if value[0] == ANY_MEMBER}


def route_id(key):
    return f"{key[0]} {key[1]}"


# --- the table covers exactly the routes the application has --------------------------------------------


def application_routes() -> set[tuple[str, str]]:
    found = set()
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            found.add((method.upper(), path))
    return found


def test_every_route_of_the_application_is_in_the_policy_table_and_vice_versa():
    actual = application_routes()
    assert actual - POLICY.keys() == set(), "unclassified routes: add them to POLICY"
    assert POLICY.keys() - actual == set(), "stale entries in POLICY"


def test_every_protected_route_declares_authentication_and_every_public_one_has_a_reason():
    paths = app.openapi()["paths"]
    for (method, path), (who, _how, _body) in POLICY.items():
        secured = bool(paths[path][method.lower()].get("security"))
        if who == PUBLIC:
            assert not secured
            assert any(path.startswith(prefix) for prefix in PUBLIC_REASON), path
        else:
            assert secured, f"{method} {path} does not require a login"


# --- the world: one business with all three roles, an outsider, and a second business ----------------------


@dataclass
class World:
    client: object
    org: str
    people: dict  # role -> auth headers
    other_org: str
    other_owner: dict
    stranger: dict  # a logged-in account that belongs to no organization
    source_id: str


@pytest.fixture()
def world(client):
    owner = signup_and_login(client, "owner@alpha.example")
    org = create_org(client, owner, "Alpha")["id"]
    people = {"owner": owner}
    for role in ("admin", "member"):
        email = f"{role}@alpha.example"
        people[role] = signup_and_login(client, email)
        assert add_member(client, owner, org, email, role.upper()).status_code == 201
    other_owner = signup_and_login(client, "owner@beta.example")
    other_org = create_org(client, other_owner, "Beta")["id"]
    stranger = signup_and_login(client, "stranger@nowhere.example")
    created = client.post(
        "/api/knowledge/sources",
        json={"type": "WEBSITE", "url": "https://alpha.example", "name": "Alpha site"},
        headers={**owner, "X-Organization-ID": org},
    )
    assert created.status_code == 201, created.text
    return World(client, org, people, other_org, other_owner, stranger, created.json()["id"])


def call(world: World, key, headers: dict, org: str, *, source_id: str | None = None):
    method, template = key
    _who, how, body = POLICY[key]
    path = (
        template.replace(ORG, org)
        .replace(SOURCE, source_id or str(uuid.uuid4()))
        .replace(DOCUMENT, str(uuid.uuid4()))
    )
    extra = {"X-Organization-ID": org} if how == HEADER else {}
    return world.client.request(method, path, json=body, headers={**headers, **extra})


def is_org_not_found(response) -> bool:
    return response.status_code == 404 and response.json().get("detail") == "Organization not found"


def is_forbidden(response) -> bool:
    return (
        response.status_code == 403 and response.json().get("detail") == "Insufficient permissions"
    )


# --- authentication ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", PROTECTED, ids=route_id)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."},  # unsigned "alg: none"
    ],
    ids=["none", "garbage", "empty", "wrong-scheme", "alg-none"],
)
def test_without_valid_credentials_every_protected_route_answers_401(world, key, headers):
    response = call(world, key, headers, world.org)
    assert response.status_code == 401, (key, response.status_code, response.text)


def test_a_deactivated_account_loses_access_immediately(world, db):
    from sqlalchemy import select

    from app.db.models import User

    headers = world.people["admin"]
    assert call(world, ("GET", "/api/organizations/current"), headers, world.org).status_code == 200
    user = db.scalar(select(User).where(User.email == "admin@alpha.example"))
    user.is_active = False
    db.commit()
    assert call(world, ("GET", "/api/organizations/current"), headers, world.org).status_code == 401


def test_a_token_cannot_be_forged_with_another_users_id(world):
    import jwt

    from app.core.config import get_settings

    me = world.client.get("/api/auth/me", headers=world.people["member"]).json()
    forged = jwt.encode({"sub": me["id"], "exp": 4102444800}, "guessed-secret", algorithm="HS256")
    r = world.client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert get_settings().jwt_secret_key.get_secret_value() not in r.text


# --- roles inside one organization ------------------------------------------------------------------------------


@pytest.mark.parametrize("key", MANAGER_ROUTES, ids=route_id)
@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owners_and_admins_pass_the_gate_of_manager_routes(world, key, role):
    response = call(world, key, world.people[role], world.org, source_id=world.source_id)
    assert not is_forbidden(response) and not is_org_not_found(response)
    assert response.status_code != 401


@pytest.mark.parametrize("key", MANAGER_ROUTES, ids=route_id)
def test_a_plain_member_is_refused_on_every_manager_route(world, key):
    response = call(world, key, world.people["member"], world.org, source_id=world.source_id)
    assert is_forbidden(response), (key, response.status_code, response.text)


@pytest.mark.parametrize("key", MEMBER_ROUTES, ids=route_id)
@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_every_role_may_use_the_read_routes(world, key, role):
    response = call(world, key, world.people[role], world.org, source_id=world.source_id)
    assert not is_forbidden(response) and not is_org_not_found(response)
    assert response.status_code != 401


# --- tenants -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ORG_ROUTES, ids=route_id)
def test_another_organizations_owner_gets_404_on_every_organization_route(world, key):
    """The same answer a non-existent organization gets: nothing confirms the tenant exists."""
    response = call(world, key, world.other_owner, world.org, source_id=world.source_id)
    assert is_org_not_found(response), (key, response.status_code, response.text)
    assert "Alpha" not in response.text


@pytest.mark.parametrize("key", ORG_ROUTES, ids=route_id)
def test_an_account_with_no_organization_cannot_reach_any_organization(world, key):
    response = call(world, key, world.stranger, world.org, source_id=world.source_id)
    assert is_org_not_found(response)


@pytest.mark.parametrize("key", ORG_ROUTES, ids=route_id)
def test_a_made_up_organization_id_is_indistinguishable_from_a_real_one_you_do_not_belong_to(
    world, key
):
    real = call(world, key, world.other_owner, world.org, source_id=world.source_id)
    made_up = call(world, key, world.other_owner, str(uuid.uuid4()), source_id=world.source_id)
    assert (real.status_code, real.json()) == (made_up.status_code, made_up.json())


@pytest.mark.parametrize(
    "value", ["not-a-uuid", "1", "' OR 1=1 --", "../../etc/passwd", "a" * 5000]
)
def test_a_malformed_organization_header_is_a_validation_error_not_a_server_error(world, value):
    r = world.client.get(
        "/api/organizations/current",
        headers={**world.people["owner"], "X-Organization-ID": value},
    )
    assert r.status_code == 422 and "Traceback" not in r.text


def test_the_default_organization_is_always_one_the_caller_belongs_to(world):
    """No header: an account sees its own organization, never anyone else's."""
    r = world.client.get("/api/knowledge/sources", headers=world.other_owner)
    assert r.status_code == 200 and r.json() == []  # Beta's list, not Alpha's one source
    r = world.client.get("/api/organizations/current", headers=world.other_owner)
    assert r.json()["id"] == world.other_org
    r = world.client.get("/api/organizations/current", headers=world.stranger)
    assert r.status_code == 403


def test_a_role_in_one_organization_gives_no_power_in_another(world):
    """Beta's owner is only a MEMBER of Alpha: owner rights must not carry over."""
    alpha_owner = world.people["owner"]
    email = "owner@beta.example"
    assert add_member(world.client, alpha_owner, world.org, email, "MEMBER").status_code == 201
    manager = ("POST", "/api/integrations/shopify/sync")
    reader = ("GET", "/api/integrations/shopify")
    assert is_forbidden(call(world, manager, world.other_owner, world.org))  # member in Alpha
    assert not is_forbidden(
        call(world, manager, world.other_owner, world.other_org)
    )  # owner in Beta
    assert call(world, reader, world.other_owner, world.org).status_code == 200


def test_roles_cannot_be_escalated_through_the_membership_api(world):
    owner, admin = world.people["owner"], world.people["admin"]
    new = signup_and_login(world.client, "new@alpha.example")
    assert new
    # an admin may add members but cannot hand out ADMIN or OWNER
    assert (
        add_member(world.client, admin, world.org, "new@alpha.example", "ADMIN").status_code == 403
    )
    assert add_member(world.client, admin, world.org, "new@alpha.example", "OWNER").status_code in (
        403,
        422,
    )
    # nobody can mint a second OWNER
    assert add_member(world.client, owner, world.org, "new@alpha.example", "OWNER").status_code in (
        403,
        422,
    )


# --- data ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        ("GET", f"/api/knowledge/sources/{SOURCE}"),
        ("DELETE", f"/api/knowledge/sources/{SOURCE}"),
        ("POST", f"/api/knowledge/sources/{SOURCE}/crawl"),
        ("GET", f"/api/knowledge/sources/{SOURCE}/documents"),
    ],
    ids=route_id,
)
def test_another_tenants_resource_id_behaves_exactly_like_one_that_does_not_exist(world, key):
    """Beta's owner, acting inside Beta, names Alpha's real source id."""
    real_id = call(world, key, world.other_owner, world.other_org, source_id=world.source_id)
    random_id = call(world, key, world.other_owner, world.other_org, source_id=str(uuid.uuid4()))
    assert real_id.status_code == random_id.status_code == 404
    assert real_id.json() == random_id.json()


def test_a_cross_tenant_attempt_changes_nothing(world):
    beta = {**world.other_owner, "X-Organization-ID": world.other_org}
    world.client.delete(f"/api/knowledge/sources/{world.source_id}", headers=beta)
    world.client.post(f"/api/knowledge/sources/{world.source_id}/crawl", headers=beta)
    alpha = {**world.people["owner"], "X-Organization-ID": world.org}
    still = world.client.get(f"/api/knowledge/sources/{world.source_id}", headers=alpha)
    assert still.status_code == 200 and still.json()["name"] == "Alpha site"


def test_listings_never_include_another_tenants_rows(world):
    beta = {**world.other_owner, "X-Organization-ID": world.other_org}
    assert world.client.get("/api/knowledge/sources", headers=beta).json() == []
    mine = world.client.get("/api/organizations", headers=world.other_owner).json()
    assert [o["id"] for o in mine] == [world.other_org]
    members = world.client.get(
        f"/api/organizations/{world.other_org}/members", headers=world.other_owner
    ).json()
    assert {m["user"]["email"] if "user" in m else m.get("email") for m in members} == {
        "owner@beta.example"
    }
