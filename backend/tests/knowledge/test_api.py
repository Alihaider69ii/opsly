import uuid

import pytest
from sqlalchemy import select

from app.db.models import Document, KnowledgeChunk, KnowledgeSource, KnowledgeSourceStatus
from app.workers.queue import CrawlJob
from tests.crawler.sites import Resp, page
from tests.helpers import add_member, signup_and_login
from tests.knowledge.helpers import counts, crawl

SOURCES = "/api/knowledge/sources"
SITE = {
    "/": page("Home", "Welcome to the Brand shop. We sell soap.", ("/faq", "/shipping")),
    "/faq": page("FAQ", "Do you ship worldwide? Yes, we ship to most countries."),
    "/shipping": page("Shipping", "Orders ship within two business days. Shipping is free."),
}


def add_source(client, headers, url="https://shop.example", **extra):
    return client.post(SOURCES, json={"type": "WEBSITE", "url": url, **extra}, headers=headers)


def crawled_source(client, db, session_factory, headers, url="https://shop.example", routes=None):
    """Create a source through the API, then let the worker crawl it, as in production."""
    created = add_source(client, headers, url).json()
    crawl(session_factory, uuid.UUID(created["id"]), routes or SITE)
    db.expire_all()
    return created["id"]


# --- creating ------------------------------------------------------------------------------------------


def test_create_source_returns_pending_and_queues_a_crawl(client, tenants, queue, db):
    a, _ = tenants
    r = add_source(client, a.headers, "HTTPS://Shop.Example/?utm_source=ad")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "PENDING" and body["type"] == "WEBSITE"
    assert body["url"] == "https://shop.example/"  # canonicalized
    assert body["name"] == "shop.example"
    assert body["document_count"] == 0
    assert body["last_crawled_at"] is None and body["error_code"] is None
    assert list(queue.jobs) == [CrawlJob(uuid.UUID(body["id"]))]
    stored = db.scalar(select(KnowledgeSource))
    assert str(stored.organization_id) == a.org["id"]


def test_the_api_never_performs_a_fetch_itself(client, tenants, queue, db, monkeypatch):
    """Creating a source only queues work: no HTTP request or DNS lookup happens in the request."""
    from app.services.crawler import fetcher, ssrf

    def forbidden(*args, **kwargs):
        raise AssertionError("the API request must not touch the network")

    monkeypatch.setattr(fetcher.SafeFetcher, "fetch", forbidden)
    monkeypatch.setattr(ssrf, "system_resolver", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    a, _ = tenants
    r = add_source(client, a.headers)
    assert r.status_code == 201 and r.json()["status"] == "PENDING"
    assert counts(db)["documents"] == 0 and len(queue.jobs) == 1


def test_custom_name_is_accepted(client, tenants):
    a, _ = tenants
    assert add_source(client, a.headers, name="  My Store ").json()["name"] == "My Store"


def test_www_is_dropped_from_the_default_name(client, tenants):
    a, _ = tenants
    assert (
        add_source(client, a.headers, "https://www.shop.example").json()["name"] == "shop.example"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost:8000/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/",
        "http://metadata.google.internal/",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "ftp://shop.example/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:pass@shop.example/",
        "https://shop.example:22/",
        "https://shop.example:8080/",
        "shop.example",
        "not a url",
        "",
        "https://" + "a" * 3000 + ".com",
    ],
)
def test_dangerous_or_invalid_urls_are_rejected_with_422_and_nothing_is_created(
    client, tenants, queue, db, url
):
    a, _ = tenants
    r = add_source(client, a.headers, url)
    assert r.status_code == 422
    assert db.scalar(select(KnowledgeSource)) is None and not queue.jobs
    assert "detail" in r.json()


def test_validation_errors_are_readable_messages(client, tenants):
    a, _ = tenants
    assert (
        "not a public website"
        in add_source(client, a.headers, "http://localhost/").json()["detail"]
    )
    assert "http://" in add_source(client, a.headers, "ftp://x.example/").json()["detail"]


@pytest.mark.parametrize("source_type", ["PDF", "FAQ", "MANUAL", "PRODUCT_CATALOG", "OTHER"])
def test_reserved_source_types_are_not_implemented_yet(client, tenants, source_type):
    a, _ = tenants
    r = client.post(
        SOURCES, json={"type": source_type, "url": "https://shop.example"}, headers=a.headers
    )
    assert r.status_code == 422


def test_unknown_source_type_is_rejected(client, tenants):
    a, _ = tenants
    r = client.post(
        SOURCES, json={"type": "EMAIL", "url": "https://shop.example"}, headers=a.headers
    )
    assert r.status_code == 422


def test_type_defaults_to_website(client, tenants):
    a, _ = tenants
    r = client.post(SOURCES, json={"url": "https://shop.example"}, headers=a.headers)
    assert r.status_code == 201 and r.json()["type"] == "WEBSITE"


def test_duplicate_website_is_rejected_even_when_spelled_differently(client, tenants):
    a, _ = tenants
    assert add_source(client, a.headers, "https://shop.example").status_code == 201
    for variant in (
        "https://shop.example/",
        "HTTPS://SHOP.EXAMPLE",
        "https://shop.example/?utm_x=1",
    ):
        assert add_source(client, a.headers, variant).status_code == 409


def test_the_same_website_can_be_added_by_two_organizations(client, tenants):
    a, b = tenants
    assert add_source(client, a.headers).status_code == 201
    assert add_source(client, b.headers).status_code == 201


def test_per_organization_source_limit(client, tenants, app_settings):
    a, _ = tenants
    app_settings.knowledge_max_sources_per_org = 2
    assert add_source(client, a.headers, "https://one.example").status_code == 201
    assert add_source(client, a.headers, "https://two.example").status_code == 201
    r = add_source(client, a.headers, "https://three.example")
    assert r.status_code == 409 and "limit" in r.json()["detail"]


def test_queue_outage_fails_the_source_visibly_instead_of_leaving_it_pending(
    client, tenants, queue
):
    a, _ = tenants
    queue.fail_enqueue = True
    r = add_source(client, a.headers)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "FAILED" and body["error_code"] == "queue_unavailable"
    assert "queue" in body["error_message"] and "redis" not in str(body).lower()


def test_organization_id_in_the_body_is_ignored(client, tenants, db):
    a, b = tenants
    r = client.post(
        SOURCES,
        json={"url": "https://shop.example", "organization_id": b.org["id"], "status": "COMPLETED"},
        headers=a.headers,
    )
    assert r.status_code == 201 and r.json()["status"] == "PENDING"
    assert str(db.scalar(select(KnowledgeSource)).organization_id) == a.org["id"]


# --- authentication and roles ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", SOURCES, {"url": "https://x.example"}),
        ("GET", SOURCES, None),
        ("GET", f"{SOURCES}/{uuid.uuid4()}", None),
        ("POST", f"{SOURCES}/{uuid.uuid4()}/crawl", None),
        ("DELETE", f"{SOURCES}/{uuid.uuid4()}", None),
        ("GET", f"{SOURCES}/{uuid.uuid4()}/documents", None),
        ("GET", f"{SOURCES}/{uuid.uuid4()}/documents/{uuid.uuid4()}", None),
    ],
)
def test_every_knowledge_endpoint_requires_authentication(client, tenants, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401


@pytest.fixture()
def team(client, tenants):
    """Brand A with an OWNER, an ADMIN and a MEMBER."""
    a, b = tenants
    people = {"owner": a.headers}
    for role in ("ADMIN", "MEMBER"):
        email = f"{role.lower()}@example.com"
        people[role.lower()] = signup_and_login(client, email)
        assert add_member(client, a.headers, a.org["id"], email, role).status_code == 201
    return people


def test_owner_and_admin_can_manage_sources_but_member_cannot(client, team, queue):
    assert add_source(client, team["member"]).status_code == 403
    assert add_source(client, team["admin"], "https://admin.example").status_code == 201
    assert add_source(client, team["owner"], "https://owner.example").status_code == 201
    source_id = add_source(client, team["owner"], "https://third.example").json()["id"]
    assert client.post(f"{SOURCES}/{source_id}/crawl", headers=team["member"]).status_code == 403
    assert client.delete(f"{SOURCES}/{source_id}", headers=team["member"]).status_code == 403
    assert client.delete(f"{SOURCES}/{source_id}", headers=team["admin"]).status_code == 204


def test_every_member_can_read_the_knowledge_base(client, team, db, session_factory):
    source_id = crawled_source(client, db, session_factory, team["owner"])
    for who in ("owner", "admin", "member"):
        assert client.get(SOURCES, headers=team[who]).status_code == 200
        assert client.get(f"{SOURCES}/{source_id}", headers=team[who]).status_code == 200
        assert client.get(f"{SOURCES}/{source_id}/documents", headers=team[who]).status_code == 200


# --- tenant isolation ------------------------------------------------------------------------------------------


def test_user_cannot_see_another_organizations_sources(client, tenants):
    a, b = tenants
    add_source(client, a.headers, "https://alpha-secret.example")
    add_source(client, b.headers, "https://beta.example")
    listing_b = client.get(SOURCES, headers=b.headers)
    assert [s["url"] for s in listing_b.json()] == ["https://beta.example/"]
    assert "alpha-secret" not in listing_b.text
    assert [s["url"] for s in client.get(SOURCES, headers=a.headers).json()] == [
        "https://alpha-secret.example/"
    ]


def test_foreign_source_ids_behave_exactly_like_missing_ones(client, tenants, db, session_factory):
    a, b = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    doc_id = db.scalar(select(Document.id))
    missing = str(uuid.uuid4())

    probes = [
        ("GET", f"{SOURCES}/{{id}}"),
        ("POST", f"{SOURCES}/{{id}}/crawl"),
        ("DELETE", f"{SOURCES}/{{id}}"),
        ("GET", f"{SOURCES}/{{id}}/documents"),
        ("GET", f"{SOURCES}/{{id}}/documents/{doc_id}"),
    ]
    for method, template in probes:
        foreign = client.request(method, template.format(id=source_id), headers=b.headers)
        absent = client.request(method, template.format(id=missing), headers=b.headers)
        assert foreign.status_code == absent.status_code == 404, (method, template)
        assert foreign.json()["detail"].split()[0] == absent.json()["detail"].split()[0]
        assert "Brand" not in foreign.text and "ship" not in foreign.text.lower()


def test_a_foreign_delete_attempt_changes_nothing(client, tenants, db, session_factory):
    a, b = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    before = counts(db)
    assert client.delete(f"{SOURCES}/{source_id}", headers=b.headers).status_code == 404
    assert client.post(f"{SOURCES}/{source_id}/crawl", headers=b.headers).status_code == 404
    assert counts(db) == before
    assert db.get(KnowledgeSource, uuid.UUID(source_id)).status == KnowledgeSourceStatus.COMPLETED


def test_document_ids_cannot_be_used_across_sources_or_organizations(
    client, tenants, db, session_factory
):
    a, b = tenants
    a_source = crawled_source(client, db, session_factory, a.headers, "https://alpha.example")
    b_source = crawled_source(client, db, session_factory, b.headers, "https://beta.example")
    a_doc = db.scalar(
        select(Document.id).where(Document.knowledge_source_id == uuid.UUID(a_source))
    )
    # right document id, but through the other organization's source and credentials
    assert (
        client.get(f"{SOURCES}/{b_source}/documents/{a_doc}", headers=b.headers).status_code == 404
    )
    assert (
        client.get(f"{SOURCES}/{a_source}/documents/{a_doc}", headers=b.headers).status_code == 404
    )
    assert (
        client.get(f"{SOURCES}/{a_source}/documents/{a_doc}", headers=a.headers).status_code == 200
    )


def test_organization_header_cannot_select_a_foreign_organization(client, tenants):
    a, b = tenants
    add_source(client, b.headers, "https://beta.example")
    r = client.get(SOURCES, headers={**a.headers, "X-Organization-ID": b.org["id"]})
    assert r.status_code == 404 and "beta" not in r.text


def test_multi_org_users_see_only_the_selected_organizations_knowledge(client, tenants):
    a, _ = tenants
    second = client.post(
        "/api/organizations", json={"name": "Brand A Two"}, headers=a.headers
    ).json()
    add_source(client, a.headers, "https://first.example")  # default = earliest organization
    add_source(client, {**a.headers, "X-Organization-ID": second["id"]}, "https://second.example")
    first = client.get(SOURCES, headers=a.headers).json()
    chosen = client.get(SOURCES, headers={**a.headers, "X-Organization-ID": second["id"]}).json()
    assert [s["url"] for s in first] == ["https://first.example/"]
    assert [s["url"] for s in chosen] == ["https://second.example/"]


# --- reading ---------------------------------------------------------------------------------------------------


def test_source_reports_counts_status_and_crawl_stats_after_a_crawl(
    client, tenants, db, session_factory
):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    body = client.get(f"{SOURCES}/{source_id}", headers=a.headers).json()
    assert body["status"] == "COMPLETED" and body["document_count"] == 3
    assert body["last_crawled_at"] is not None and body["error_code"] is None
    assert body["crawl_stats"]["documents"] == 3
    assert [s["document_count"] for s in client.get(SOURCES, headers=a.headers).json()] == [3]
    assert "password" not in str(body) and "content_hash" not in str(body)


def test_failed_crawl_shows_a_friendly_message_and_no_internals(
    client, tenants, db, session_factory
):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers, routes={"/": Resp(500)})
    body = client.get(f"{SOURCES}/{source_id}", headers=a.headers).json()
    assert body["status"] == "FAILED" and body["error_code"] == "http_error"
    assert body["error_message"] == "The website returned an error when we tried to open it."
    for leak in ("Traceback", "Exception", ".py", "500", "sqlalchemy"):
        assert leak not in str(body)


def test_document_listing_pagination_and_content(client, tenants, db, session_factory):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    page1 = client.get(f"{SOURCES}/{source_id}/documents?limit=2", headers=a.headers).json()
    assert page1["total"] == 3 and page1["limit"] == 2 and page1["offset"] == 0
    assert [d["url"] for d in page1["items"]] == [
        "https://shop.example/",
        "https://shop.example/faq",
    ]
    page2 = client.get(
        f"{SOURCES}/{source_id}/documents?limit=2&offset=2", headers=a.headers
    ).json()
    assert [d["url"] for d in page2["items"]] == ["https://shop.example/shipping"]

    first = page1["items"][0]
    assert first["title"] == "Home" and first["status"] == "READY"
    assert first["chunk_count"] >= 1 and first["character_count"] > 20
    assert "Welcome to the Brand shop." in first["content_preview"]
    assert set(first) == {
        "id",
        "url",
        "title",
        "status",
        "character_count",
        "chunk_count",
        "content_preview",
        "updated_at",
    }


def test_document_detail_returns_full_text_but_no_internals(client, tenants, db, session_factory):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    doc = db.scalar(select(Document).where(Document.url.endswith("/faq")))
    body = client.get(f"{SOURCES}/{source_id}/documents/{doc.id}", headers=a.headers).json()
    assert "Do you ship worldwide?" in body["content"] and body["chunk_count"] >= 1
    assert (
        "embedding" not in str(body)
        and "organization_id" not in body
        and "content_hash" not in body
    )


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "limit=abc"])
def test_document_listing_validates_pagination(client, tenants, db, session_factory, query):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    assert (
        client.get(f"{SOURCES}/{source_id}/documents?{query}", headers=a.headers).status_code == 422
    )


def test_malformed_ids_are_rejected(client, tenants):
    a, _ = tenants
    assert client.get(f"{SOURCES}/not-a-uuid", headers=a.headers).status_code == 422


# --- re-crawl and delete -------------------------------------------------------------------------------------------


def test_recrawl_queues_a_new_job_and_moves_to_pending(client, tenants, db, session_factory, queue):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    queue.jobs.clear()
    r = client.post(f"{SOURCES}/{source_id}/crawl", headers=a.headers)
    assert r.status_code == 202
    assert (
        r.json()["status"] == "PENDING" and r.json()["document_count"] == 3
    )  # data kept meanwhile
    assert list(queue.jobs) == [CrawlJob(uuid.UUID(source_id))]


def test_recrawl_is_refused_while_a_crawl_is_in_progress(client, tenants, queue):
    a, _ = tenants
    source_id = add_source(client, a.headers).json()["id"]  # PENDING
    queue.jobs.clear()
    r = client.post(f"{SOURCES}/{source_id}/crawl", headers=a.headers)
    assert r.status_code == 409 and not queue.jobs


def test_recrawl_of_a_failed_source_is_allowed(client, tenants, db, session_factory, queue):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers, routes={"/": Resp(500)})
    queue.jobs.clear()
    assert client.post(f"{SOURCES}/{source_id}/crawl", headers=a.headers).status_code == 202
    assert len(queue.jobs) == 1


def test_recrawl_cooldown_returns_429_with_retry_after(
    client, tenants, db, session_factory, app_settings
):
    a, _ = tenants
    app_settings.crawl_min_interval_seconds = 60
    source_id = crawled_source(client, db, session_factory, a.headers)
    r = client.post(f"{SOURCES}/{source_id}/crawl", headers=a.headers)
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 61
    assert db.get(KnowledgeSource, uuid.UUID(source_id)).status == KnowledgeSourceStatus.COMPLETED


def test_recrawl_when_the_queue_is_down_fails_visibly(client, tenants, db, session_factory, queue):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    queue.fail_enqueue = True
    r = client.post(f"{SOURCES}/{source_id}/crawl", headers=a.headers)
    assert r.status_code == 202 and r.json()["error_code"] == "queue_unavailable"


def test_delete_removes_the_source_its_documents_and_chunks(client, tenants, db, session_factory):
    a, _ = tenants
    source_id = crawled_source(client, db, session_factory, a.headers)
    assert counts(db)["chunks"] > 0
    assert client.delete(f"{SOURCES}/{source_id}", headers=a.headers).status_code == 204
    db.expire_all()
    assert counts(db) == {"sources": 0, "documents": 0, "chunks": 0}
    assert client.get(f"{SOURCES}/{source_id}", headers=a.headers).status_code == 404
    assert client.delete(f"{SOURCES}/{source_id}", headers=a.headers).status_code == 404


def test_deleting_one_organizations_source_leaves_the_others_knowledge_intact(
    client, tenants, db, session_factory
):
    a, b = tenants
    a_source = crawled_source(client, db, session_factory, a.headers, "https://alpha.example")
    crawled_source(client, db, session_factory, b.headers, "https://beta.example")
    b_chunks = db.scalar(
        select(KnowledgeChunk.id).where(KnowledgeChunk.organization_id == uuid.UUID(b.org["id"]))
    )
    client.delete(f"{SOURCES}/{a_source}", headers=a.headers)
    db.expire_all()
    assert db.get(KnowledgeChunk, b_chunks) is not None
    assert [s["url"] for s in client.get(SOURCES, headers=b.headers).json()] == [
        "https://beta.example/"
    ]
