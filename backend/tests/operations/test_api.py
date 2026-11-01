import uuid
from datetime import UTC, datetime, timedelta

from tests.helpers import add_member, signup_and_login
from tests.operations.conftest import make_customer, make_order


def test_create_and_list_issue(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers

    r = client.post(
        "/api/operations/issues",
        json={"category": "ORDER", "title": "Manual issue from support"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    issue_id = r.json()["id"]
    assert r.json()["detected_by"] == "HUMAN"
    assert r.json()["priority_source"] == "HUMAN"

    r = client.get("/api/operations/issues", headers=headers)
    assert r.status_code == 200
    assert any(row["id"] == issue_id for row in r.json())


def test_cross_tenant_issue_access_is_404(client, db, tenants):
    tenant_a, tenant_b = tenants
    r = client.post(
        "/api/operations/issues",
        json={"category": "ORDER", "title": "A's issue"},
        headers=tenant_a.headers,
    )
    issue_id = r.json()["id"]

    r = client.get(f"/api/operations/issues/{issue_id}", headers=tenant_b.headers)
    assert r.status_code == 404


def test_assign_resolve_reopen_issue_flow(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers
    r = client.post(
        "/api/operations/issues",
        json={"category": "INVENTORY", "title": "Low stock"},
        headers=headers,
    )
    issue_id = r.json()["id"]
    user_id = str(uuid.UUID(client.get("/api/auth/me", headers=headers).json()["id"]))

    r = client.post(
        f"/api/operations/issues/{issue_id}/assign",
        json={"assigned_user_id": user_id},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["assigned_user_id"] == user_id
    assert r.json()["status"] == "ACKNOWLEDGED"

    r = client.post(f"/api/operations/issues/{issue_id}/resolve", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "RESOLVED"

    r = client.post(f"/api/operations/issues/{issue_id}/reopen", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "OPEN"


def test_patch_issue_priority_marks_it_human_sourced(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers
    r = client.post(
        "/api/operations/issues", json={"category": "ORDER", "title": "x"}, headers=headers
    )
    issue_id = r.json()["id"]

    r = client.patch(
        f"/api/operations/issues/{issue_id}", json={"priority": "URGENT"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["priority"] == "URGENT"
    assert r.json()["priority_source"] == "HUMAN"


def test_task_create_claim_resolve(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers

    r = client.post(
        "/api/operations/tasks",
        json={"title": "Call the customer", "category": "SHIPPING"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    assert r.json()["status"] == "OPEN"

    r = client.post(f"/api/operations/tasks/{task_id}/claim", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "ASSIGNED"

    r = client.post(f"/api/operations/tasks/{task_id}/claim", headers=headers)
    assert r.status_code == 409  # already claimed

    r = client.post(
        f"/api/operations/tasks/{task_id}/resolve", json={"resolution": "done"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["status"] == "RESOLVED"


def test_task_claim_on_nonexistent_task_is_404(client, db, tenants):
    tenant_a, _ = tenants
    r = client.post(f"/api/operations/tasks/{uuid.uuid4()}/claim", headers=tenant_a.headers)
    assert r.status_code == 404


def test_action_requires_approval_and_only_admin_can_approve(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers

    r = client.post(
        "/api/operations/actions",
        json={
            "action_type": "refund",
            "resource_type": "order",
            "resource_id": str(uuid.uuid4()),
            "idempotency_key": "api-test-1",
            "payload": {"amount": "10.00"},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    action_id = r.json()["id"]
    assert r.json()["category"] == "REQUIRES_APPROVAL"
    assert r.json()["status"] == "PENDING"

    # Cannot execute before approval.
    r = client.post(f"/api/operations/actions/{action_id}/execute", headers=headers)
    assert r.status_code == 409

    r = client.post(f"/api/operations/actions/{action_id}/approve", headers=headers)
    assert r.status_code == 200  # tenant creator is OWNER, which is authorized to approve
    assert r.json()["status"] == "APPROVED"

    r = client.post(f"/api/operations/actions/{action_id}/execute", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "EXECUTED"


def test_a_plain_member_cannot_approve_an_action(client, db, tenants):
    tenant_a, _ = tenants
    org_id = tenant_a.org["id"]

    r = client.post(
        "/api/operations/actions",
        json={
            "action_type": "cancel_order",
            "resource_type": "order",
            "idempotency_key": "api-test-role",
        },
        headers=tenant_a.headers,
    )
    action_id = r.json()["id"]

    member_headers = signup_and_login(client, "member@example.com")
    add_member(client, tenant_a.headers, org_id, "member@example.com", "MEMBER")
    member_headers = {**member_headers, "X-Organization-ID": org_id}

    r = client.post(f"/api/operations/actions/{action_id}/approve", headers=member_headers)
    assert r.status_code == 403


def test_forbidden_action_type_is_rejected_by_the_api(client, db, tenants):
    tenant_a, _ = tenants
    r = client.post(
        "/api/operations/actions",
        json={
            "action_type": "wipe_database",
            "resource_type": "order",
            "resource_id": None,
            "idempotency_key": "api-test-2",
        },
        headers=tenant_a.headers,
    )
    assert r.status_code == 201
    assert r.json()["category"] == "FORBIDDEN"
    assert r.json()["status"] == "FAILED"


def test_safe_action_create_task_executes_without_approval(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers
    r = client.post(
        "/api/operations/actions",
        json={
            "action_type": "create_task",
            "resource_type": "operational_issue",
            "idempotency_key": "api-test-3",
            "payload": {"title": "AI-suggested follow up", "category": "ORDER"},
        },
        headers=headers,
    )
    assert r.status_code == 201
    action_id = r.json()["id"]
    assert r.json()["status"] == "APPROVED"

    r = client.post(f"/api/operations/actions/{action_id}/execute", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "EXECUTED"
    assert r.json()["result"]["task_id"]


def test_dashboard_and_analytics_endpoints_respond(client, db, tenants):
    tenant_a, _ = tenants
    headers = tenant_a.headers
    client.post("/api/operations/issues", json={"category": "ORDER", "title": "x"}, headers=headers)

    r = client.get("/api/operations/dashboard", headers=headers)
    assert r.status_code == 200
    assert r.json()["kpis"]["open_issues"] >= 1

    r = client.get("/api/operations/analytics", headers=headers)
    assert r.status_code == 200
    assert "issues_by_category" in r.json()


def test_sweep_endpoint_detects_a_seeded_unfulfilled_order(client, db, tenants):
    tenant_a, _ = tenants
    org_id = uuid.UUID(tenant_a.org["id"])
    customer = make_customer(db, org_id)
    make_order(
        db,
        org_id,
        customer=customer,
        shopify_order_id=777,
        fulfillment_status="unfulfilled",
        created_at_shopify=datetime.now(UTC) - timedelta(days=5),
    )
    db.commit()

    r = client.post("/api/operations/sweep", headers=tenant_a.headers)
    assert r.status_code == 200
    assert r.json()["created"] >= 1

    r = client.get("/api/operations/issues", headers=tenant_a.headers)
    assert any(row["category"] == "ORDER" for row in r.json())


def test_ai_query_insufficient_data_for_unrelated_question(client, db, tenants):
    tenant_a, _ = tenants
    r = client.post(
        "/api/operations/ai/query",
        json={"question": "What's the weather like?"},
        headers=tenant_a.headers,
    )
    assert r.status_code == 200
    assert r.json()["grounded"] is False
