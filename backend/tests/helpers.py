from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"


def signup_and_login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def create_org(client: TestClient, headers: dict[str, str], name: str, **extra) -> dict:
    r = client.post("/api/organizations", json={"name": name, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def add_member(
    client: TestClient, owner_headers: dict[str, str], org_id: str, email: str, role: str
):
    return client.post(
        f"/api/organizations/{org_id}/members",
        json={"email": email, "role": role},
        headers=owner_headers,
    )
