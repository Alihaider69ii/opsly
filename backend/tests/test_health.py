def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_db(client):
    r = client.get("/health/db")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


def test_cors_allows_configured_origin(client):
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
