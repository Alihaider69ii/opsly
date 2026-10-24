import json
import time
import uuid

import pytest
import redis
from sqlalchemy import select

from app.db.models import Document, KnowledgeChunk, KnowledgeSource, KnowledgeSourceStatus
from app.services.ai.providers.fake import FakeEmbeddingProvider
from app.services.knowledge.crawl_runner import run_crawl
from app.services.rag.search import search_knowledge
from app.workers.queue import CrawlJob, RedisJobQueue
from tests.crawler.sites import loopback_fetcher, send, serve
from tests.knowledge.helpers import LIMITS

SOURCES = "/api/knowledge/sources"
FAKE = FakeEmbeddingProvider()


# --- Redis queue ---------------------------------------------------------------------------------------


def test_jobs_round_trip_through_real_redis_in_fifo_order(redis_queue):
    ids = [uuid.uuid4() for _ in range(3)]
    for source_id in ids:
        redis_queue.enqueue(CrawlJob(source_id))
    assert redis_queue.length() == 3
    assert [redis_queue.dequeue(timeout=1).source_id for _ in range(3)] == ids
    assert redis_queue.length() == 0


def test_dequeue_blocks_then_returns_none_when_empty(redis_queue):
    started = time.monotonic()
    assert redis_queue.dequeue(timeout=1) is None
    assert 0.8 < time.monotonic() - started < 3


def test_malformed_payloads_are_discarded_not_crashing_the_worker(redis_queue):
    for junk in (
        "not json",
        json.dumps({"type": "other"}),
        json.dumps({"type": "crawl", "source_id": "nope"}),
        "[]",
    ):
        redis_queue._client.rpush(redis_queue._key, junk)
    redis_queue.enqueue(CrawlJob(uuid.UUID(int=7)))
    results = [redis_queue.dequeue(timeout=1) for _ in range(5)]
    assert results[:4] == [None] * 4 and results[4] == CrawlJob(uuid.UUID(int=7))


def test_enqueue_fails_fast_when_redis_is_unreachable():
    dead = RedisJobQueue("redis://127.0.0.1:1/0", key="x")
    started = time.monotonic()
    with pytest.raises(redis.RedisError):
        dead.enqueue(CrawlJob(uuid.uuid4()))
    assert time.monotonic() - started < 5  # an API request can't hang on a dead queue


def test_api_marks_the_source_failed_when_the_real_queue_is_down(
    pg_client, two_tenants, integration_settings
):
    from app.main import app
    from app.workers.queue import get_job_queue

    (headers, _), _ = two_tenants
    dead = RedisJobQueue("redis://127.0.0.1:1/0", key="x")
    app.dependency_overrides[get_job_queue] = lambda: dead
    r = pg_client.post(SOURCES, json={"url": "https://shop.example"}, headers=headers)
    assert r.status_code == 201
    assert r.json()["status"] == "FAILED" and r.json()["error_code"] == "queue_unavailable"
    assert "127.0.0.1" not in r.text and "Connection" not in r.text


# --- the whole pipeline -------------------------------------------------------------------------------------


def site_routes():
    def html(body: str):
        return lambda h: send(h, 200, body.encode(), {"Content-Type": "text/html; charset=utf-8"})

    home = (
        "<html><head><title>Luna Soap Co</title></head><body><nav><a href='/faq'>FAQ</a>"
        "<a href='/shipping'>Shipping</a><a href='/returns'>Returns</a></nav><main>"
        "<h1>Luna Soap Co</h1><p>We make handmade lavender and oatmeal soap in small batches.</p>"
        "<a href='/products/lavender-soap'>Lavender soap</a></main></body></html>"
    )
    return {
        "/": html(home),
        "/faq": html(
            "<html><head><title>FAQ</title></head><body><main><h1>Frequently asked questions</h1>"
            "<h2>Do you ship internationally?</h2><p>Yes, we ship to Canada, the UK and Australia.</p>"
            "<h2>Is your soap vegan?</h2><p>All of our soaps are 100 percent vegan.</p></main></body></html>"
        ),
        "/shipping": html(
            "<html><head><title>Shipping Policy</title></head><body><main><h1>Shipping Policy</h1>"
            "<p>Orders ship within two business days. Standard delivery takes five to seven days. "
            "Orders over fifty dollars ship free.</p></main></body></html>"
        ),
        "/returns": html(
            "<html><head><title>Returns Policy</title></head><body><main><h1>Returns Policy</h1>"
            "<p>Unopened soap can be returned within thirty days for a full refund.</p></main></body></html>"
        ),
        "/products/lavender-soap": html(
            "<html><head><title>Lavender Soap</title></head><body><main><h1>Lavender Soap</h1>"
            "<p>Handmade lavender soap bar, 100g. Price: 8 dollars.</p></main></body></html>"
        ),
        "/robots.txt": lambda h: send(
            h, 200, b"User-agent: *\nDisallow: /admin\n", {"Content-Type": "text/plain"}
        ),
    }


def worker_tick(pg_factory, redis_queue, integration_settings):
    """One iteration of the worker loop (what `python -m app.workers.main` does per job)."""
    job = redis_queue.dequeue(timeout=2)
    assert job is not None, "the API should have queued a crawl job in Redis"
    return job, run_crawl(
        pg_factory,
        job.source_id,
        settings=integration_settings,
        limits=LIMITS,
        fetcher_factory=loopback_fetcher,
        embedder_factory=lambda: FAKE,
        sleep=lambda seconds: None,
    )


def test_full_flow_add_website_crawl_index_search_recrawl_delete(
    pg_client, two_tenants, pg_factory, redis_queue, integration_settings
):
    (headers_a, org_a), (headers_b, org_b) = two_tenants
    with serve(site_routes()) as port:
        integration_settings.crawl_allowed_ports = (
            f"80,443,{port}"  # let the API accept the test port
        )
        url = f"http://site.test:{port}"

        # 1. API: returns at once, status PENDING, job waits in real Redis
        created = pg_client.post(SOURCES, json={"type": "WEBSITE", "url": url}, headers=headers_a)
        assert created.status_code == 201 and created.json()["status"] == "PENDING"
        source_id = created.json()["id"]
        assert redis_queue.length() == 1

        # 2. Worker: picks the job up and crawls the site over real sockets
        job, outcome = worker_tick(pg_factory, redis_queue, integration_settings)
        assert str(job.source_id) == source_id and outcome == "completed"

        # 3. The API now shows a completed source with its pages
        source = pg_client.get(f"{SOURCES}/{source_id}", headers=headers_a).json()
        assert source["status"] == "COMPLETED" and source["document_count"] == 5
        assert source["crawl_stats"]["documents"] == 5 and source["last_crawled_at"]

        listing = pg_client.get(f"{SOURCES}/{source_id}/documents", headers=headers_a).json()
        assert {d["title"] for d in listing["items"]} == {
            "Luna Soap Co",
            "FAQ",
            "Shipping Policy",
            "Returns Policy",
            "Lavender Soap",
        }
        assert all(d["status"] == "READY" and d["chunk_count"] >= 1 for d in listing["items"])

        # 4. Chunks carry real pgvector embeddings, all owned by org A
        with pg_factory() as db:
            chunks = db.scalars(select(KnowledgeChunk)).all()
            assert chunks and {str(c.organization_id) for c in chunks} == {org_a["id"]}
            assert all(
                len(c.embedding) == 1536 and c.embedding_model == FAKE.model_id for c in chunks
            )

            # 5. Vector search finds the right page for a question
            def ask(question, **kw):
                return search_knowledge(
                    db,
                    organization_id=uuid.UUID(org_a["id"]),
                    query_embedding=FAKE.embed([question])[0],
                    embedding_model=FAKE.model_id,
                    **kw,
                )

            top = ask("how long does standard delivery take and when do orders ship", top_k=3)
            assert top[0].title == "Shipping Policy" and "five to seven days" in top[0].content
            assert top[0].url == f"http://site.test:{port}/shipping"
            assert ask("is the soap vegan", top_k=1)[0].title == "FAQ"
            assert (
                ask("can I return unopened soap for a refund", top_k=1)[0].title == "Returns Policy"
            )

            # 6. Organization B asks the very same questions and gets nothing
            for question in ("shipping delivery orders", "vegan soap", "refund returns"):
                assert (
                    search_knowledge(
                        db,
                        organization_id=uuid.UUID(org_b["id"]),
                        query_embedding=FAKE.embed([question])[0],
                        embedding_model=FAKE.model_id,
                    )
                    == []
                )

        # 7. B cannot see or touch A's source through the API either
        assert pg_client.get(f"{SOURCES}/{source_id}", headers=headers_b).status_code == 404
        assert (
            pg_client.get(f"{SOURCES}/{source_id}/documents", headers=headers_b).status_code == 404
        )
        assert pg_client.delete(f"{SOURCES}/{source_id}", headers=headers_b).status_code == 404

        # 8. Re-crawl: unchanged pages are kept without re-embedding
        before = {c.id for c in chunks}
        again = pg_client.post(f"{SOURCES}/{source_id}/crawl", headers=headers_a)
        assert again.status_code == 202 and again.json()["status"] == "PENDING"
        _, outcome = worker_tick(pg_factory, redis_queue, integration_settings)
        assert outcome == "completed"
        with pg_factory() as db:
            assert {c.id for c in db.scalars(select(KnowledgeChunk))} == before  # untouched
        assert (
            pg_client.get(f"{SOURCES}/{source_id}", headers=headers_a).json()["crawl_stats"][
                "chunks_created"
            ]
            == 0
        )

        # 9. Delete: source, documents and chunks all go
        assert pg_client.delete(f"{SOURCES}/{source_id}", headers=headers_a).status_code == 204
        with pg_factory() as db:
            assert db.scalar(select(KnowledgeSource)) is None
            assert db.scalar(select(Document)) is None and db.scalar(select(KnowledgeChunk)) is None


def test_duplicate_job_delivery_is_harmless_with_the_real_database(
    pg_client, two_tenants, pg_factory, redis_queue, integration_settings
):
    (headers, _), _ = two_tenants
    with serve(site_routes()) as port:
        integration_settings.crawl_allowed_ports = f"80,443,{port}"
        source_id = pg_client.post(
            SOURCES, json={"url": f"http://site.test:{port}"}, headers=headers
        ).json()["id"]
        redis_queue.enqueue(CrawlJob(uuid.UUID(source_id)))  # the same job delivered twice
        outcomes = [worker_tick(pg_factory, redis_queue, integration_settings)[1] for _ in range(2)]
        assert sorted(outcomes) == ["completed", "skipped"]
        with pg_factory() as db:
            assert len(db.scalars(select(Document)).all()) == 5  # crawled once, not twice


def test_failed_crawl_on_real_infrastructure_reports_a_safe_message(
    pg_client, two_tenants, pg_factory, redis_queue, integration_settings
):
    (headers, _), _ = two_tenants
    routes = {
        "/": lambda h: send(
            h, 500, b"<html>Internal Server Error: stack trace at /srv/app.py</html>"
        )
    }
    with serve(routes) as port:
        integration_settings.crawl_allowed_ports = f"80,443,{port}"
        source_id = pg_client.post(
            SOURCES, json={"url": f"http://site.test:{port}"}, headers=headers
        ).json()["id"]
        _, outcome = worker_tick(pg_factory, redis_queue, integration_settings)
    assert outcome == "failed"
    body = pg_client.get(f"{SOURCES}/{source_id}", headers=headers).json()
    assert body["status"] == KnowledgeSourceStatus.FAILED and body["error_code"] == "http_error"
    assert "stack trace" not in str(body) and "/srv/app.py" not in str(body)
