import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Document,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceStatus,
)
from app.services.knowledge.crawl_runner import fail_stale_crawls, requeue_orphaned_pending
from app.services.knowledge.errors import ERROR_MESSAGES
from app.workers.queue import CrawlJob, InMemoryJobQueue
from tests.crawler.sites import FakeSite, Resp, page, redirect, text
from tests.knowledge.helpers import (
    CountingEmbedder,
    counts,
    crawl,
    make_org,
    make_source,
)

SITE = {
    "/": page("Home", "Welcome to the Brand shop. We sell soap.", ("/faq", "/shipping")),
    "/faq": page("FAQ", "Do you ship worldwide? Yes, we ship to most countries."),
    "/shipping": page("Shipping", "Orders ship within two business days. Shipping is free."),
}


@pytest.fixture()
def org(db):
    return make_org(db)


def reload(db, source):
    db.expire_all()
    return db.get(KnowledgeSource, source.id)


# --- the happy path ------------------------------------------------------------------------------


def test_successful_crawl_stores_documents_chunks_and_completes(db, org, session_factory):
    source = make_source(db, org)
    outcome, _ = crawl(session_factory, source.id, SITE)
    assert outcome == "completed"

    source = reload(db, source)
    assert source.status == KnowledgeSourceStatus.COMPLETED
    assert source.last_crawled_at is not None and source.crawl_started_at is not None
    assert source.error_code is None and source.error_message is None
    assert source.crawl_stats["documents"] == 3 and source.crawl_stats["stopped_by"] is None

    documents = list(db.scalars(select(Document).order_by(Document.url)))
    assert [d.url for d in documents] == [
        "https://shop.example/",
        "https://shop.example/faq",
        "https://shop.example/shipping",
    ]
    assert all(d.status == DocumentStatus.READY for d in documents)
    assert all(
        d.organization_id == org.id and d.knowledge_source_id == source.id for d in documents
    )
    home = documents[0]
    assert home.title == "Home" and "Welcome to the Brand shop." in home.content
    assert len(home.content_hash) == 64 and "<" not in home.content  # cleaned text, never raw HTML

    chunks = list(db.scalars(select(KnowledgeChunk)))
    assert len(chunks) >= 3
    assert {c.organization_id for c in chunks} == {org.id}
    assert {c.embedding_model for c in chunks} == {"fake:hash-v1"}
    assert all(len(c.embedding) == 1536 and c.character_count == len(c.content) for c in chunks)


def test_source_is_crawling_while_the_job_runs(db, org, session_factory):
    source = make_source(db, org)
    seen = {}

    def peek():
        with session_factory() as other:
            seen["status"] = other.get(KnowledgeSource, source.id).status

    crawl(session_factory, source.id, SITE, embedder=CountingEmbedder(hook=peek))
    assert seen["status"] == KnowledgeSourceStatus.CRAWLING


def test_each_chunk_is_embedded_with_its_page_title_for_context(db, org, session_factory):
    embedder = CountingEmbedder()
    crawl(session_factory, make_source(db, org).id, SITE, embedder=embedder)
    assert any(text.startswith("FAQ\n\n") for text in embedder.texts)
    stored = db.scalars(select(KnowledgeChunk.content)).all()
    assert not any(c.startswith("FAQ\n\n") for c in stored)  # the title is not stored in the chunk


# --- claiming / idempotency -------------------------------------------------------------------------


def test_only_one_worker_can_claim_a_source(db, org, session_factory):
    source = make_source(db, org)
    first, _ = crawl(session_factory, source.id, SITE)
    second, site = crawl(session_factory, source.id, SITE)  # duplicate delivery of the same job
    assert (first, second) == ("completed", "skipped")
    assert site.requests == []


def test_a_source_that_is_already_crawling_is_skipped(db, org, session_factory):
    source = make_source(db, org, status=KnowledgeSourceStatus.CRAWLING)
    outcome, site = crawl(session_factory, source.id, SITE)
    assert outcome == "skipped" and site.requests == []


def test_a_job_for_a_deleted_source_is_skipped(session_factory):
    outcome, site = crawl(session_factory, uuid.uuid4(), SITE)
    assert outcome == "skipped" and site.requests == []


# --- failures ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("routes", "code"),
    [
        ({"/": Resp(404)}, "http_error"),
        ({"/": Resp(500)}, "http_error"),
        ({"/": redirect("https://elsewhere.example/")}, "redirect_off_site"),
        ({"/": Resp(200, b"{}", {"content-type": "application/json"})}, "not_html"),
        (
            {"/robots.txt": text("User-agent: *\nDisallow: /"), "/": page("Home", "content here")},
            "robots_disallowed",
        ),
        (
            {
                "/": Resp(
                    200,
                    b"<html><body><script>app()</script></body></html>",
                    {"content-type": "text/html"},
                )
            },
            "no_content",
        ),
    ],
)
def test_failed_crawls_record_a_code_and_a_safe_catalogue_message(
    db, org, session_factory, routes, code
):
    source = make_source(db, org)
    outcome, _ = crawl(session_factory, source.id, routes)
    source = reload(db, source)
    assert outcome == "failed" and source.status == KnowledgeSourceStatus.FAILED
    assert source.error_code == code
    assert source.error_message == ERROR_MESSAGES[code]
    assert source.last_crawled_at is None


def test_a_source_pointing_at_an_internal_address_fails_without_any_request(
    db, org, session_factory
):
    # Even if such a row got into the database, the worker's own check still refuses it.
    source = make_source(db, org, url="http://169.254.169.254/latest/meta-data/")
    outcome, site = crawl(session_factory, source.id, SITE)
    source = reload(db, source)
    assert outcome == "failed" and source.error_code == "blocked_url"
    assert site.requests == []


def test_a_hostname_resolving_to_a_private_address_fails_as_blocked(db, org, session_factory):
    source = make_source(db, org, url="https://looks-public.example/")
    outcome, site = crawl(session_factory, source.id, SITE, resolver=lambda h, p: ["10.0.0.7"])
    assert outcome == "failed" and reload(db, source).error_code == "blocked_url"
    assert site.requests == []


def test_error_messages_never_contain_internals(db, org, session_factory):
    source = make_source(db, org)

    class Boom(CountingEmbedder):
        def embed(self, texts):
            raise RuntimeError("secret internal detail at /srv/app/crawler.py line 42")

    outcome, _ = crawl(session_factory, source.id, SITE, embedder=Boom())
    source = reload(db, source)
    assert outcome == "failed" and source.error_code == "internal_error"
    shown = f"{source.error_message} {source.error_code} {source.crawl_stats}"
    for leak in ("secret", "/srv/app", "line 42", "Traceback", "RuntimeError"):
        assert leak not in shown


def test_embedding_provider_failure_fails_the_crawl_but_keeps_indexed_pages(
    db, org, session_factory
):
    source = make_source(db, org)
    outcome, _ = crawl(session_factory, source.id, SITE, embedder=CountingEmbedder(fail_after=1))
    source = reload(db, source)
    assert outcome == "failed" and source.error_code == "embedding_failed"
    statuses = {d.url: d.status for d in db.scalars(select(Document))}
    assert DocumentStatus.READY in statuses.values()  # page 1 survived
    assert DocumentStatus.FAILED in statuses.values()  # the page that could not be embedded
    failed_ids = [d.id for d in db.scalars(select(Document)) if d.status == DocumentStatus.FAILED]
    assert not db.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(failed_ids))
    ).all()


def test_missing_embedding_configuration_fails_with_a_clear_code(db, org, session_factory):
    from app.services.ai.embeddings import EmbeddingError
    from app.services.knowledge.crawl_runner import run_crawl
    from tests.crawler.sites import fetcher_for
    from tests.knowledge.helpers import LIMITS, settings_for_runner

    source = make_source(db, org)

    def unconfigured():
        raise EmbeddingError("unconfigured", "OPENAI_API_KEY is not set")

    site = FakeSite(SITE)
    outcome = run_crawl(
        session_factory,
        source.id,
        settings=settings_for_runner(),
        limits=LIMITS,
        fetcher_factory=lambda: fetcher_for(site),
        embedder_factory=unconfigured,
    )
    source = reload(db, source)
    assert outcome == "failed" and source.error_code == "embedding_unconfigured"
    assert "OPENAI" not in (source.error_message or "")


def test_a_crashing_crawl_ends_failed_not_stuck_crawling(db, org, session_factory):
    from app.services.ai.providers.fake import FakeEmbeddingProvider
    from app.services.knowledge.crawl_runner import run_crawl
    from tests.knowledge.helpers import LIMITS, settings_for_runner

    source = make_source(db, org)

    def exploding_fetcher():
        raise MemoryError("kaboom")

    outcome = run_crawl(
        session_factory,
        source.id,
        settings=settings_for_runner(),
        limits=LIMITS,
        fetcher_factory=exploding_fetcher,
        embedder_factory=FakeEmbeddingProvider,
    )
    assert outcome == "failed" and reload(db, source).status == KnowledgeSourceStatus.FAILED


# --- re-crawling -------------------------------------------------------------------------------------


def recrawl(db, session_factory, source, routes, **kwargs):
    db.execute(
        update(KnowledgeSource)
        .where(KnowledgeSource.id == source.id)
        .values(status=KnowledgeSourceStatus.PENDING)
    )
    db.commit()
    return crawl(session_factory, source.id, routes, **kwargs)


def test_unchanged_pages_are_not_re_embedded_on_a_recrawl(db, org, session_factory):
    source = make_source(db, org)
    first = CountingEmbedder()
    crawl(session_factory, source.id, SITE, embedder=first)
    before = counts(db)

    second = CountingEmbedder()
    outcome, _ = recrawl(db, session_factory, source, SITE, embedder=second)
    assert outcome == "completed" and second.calls == 0 and second.texts == []
    assert counts(db) == before
    assert reload(db, source).crawl_stats["chunks_created"] == 0


def test_changed_pages_are_re_chunked_and_re_embedded(db, org, session_factory):
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)
    old_chunks = {c.id for c in db.scalars(select(KnowledgeChunk))}

    changed = {**SITE, "/faq": page("FAQ", "Updated answer: we now ship everywhere in the world.")}
    embedder = CountingEmbedder()
    recrawl(db, session_factory, source, changed, embedder=embedder)

    db.expire_all()
    faq = db.scalar(select(Document).where(Document.url.endswith("/faq")))
    assert "Updated answer" in faq.content and faq.status == DocumentStatus.READY
    assert len(embedder.texts) == 1 and embedder.texts[0].startswith("FAQ")  # only that page
    faq_chunks = db.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.document_id == faq.id)
    ).all()
    assert faq_chunks and all(
        "Updated answer" in c.content or c.chunk_index >= 0 for c in faq_chunks
    )
    assert not {c.id for c in faq_chunks} & old_chunks  # old chunks replaced, not duplicated
    assert (
        db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.content.contains("Do you ship")))
        is None
    )


def test_pages_removed_from_the_site_are_deleted_with_their_chunks(db, org, session_factory):
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)
    shrunk = {
        "/": page("Home", "Welcome to the Brand shop. We sell soap.", ("/faq",)),
        "/faq": SITE["/faq"],
    }
    recrawl(db, session_factory, source, shrunk)
    db.expire_all()
    assert {d.url for d in db.scalars(select(Document))} == {
        "https://shop.example/",
        "https://shop.example/faq",
    }
    assert (
        db.scalar(
            select(KnowledgeChunk).where(KnowledgeChunk.content.contains("two business days"))
        )
        is None
    )
    assert reload(db, source).crawl_stats["documents_removed"] == 1


def test_a_404_removes_the_document_but_a_server_error_keeps_it(db, org, session_factory):
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)
    flaky = {**SITE, "/faq": Resp(503), "/shipping": Resp(404)}
    recrawl(db, session_factory, source, flaky)
    db.expire_all()
    urls = {d.url for d in db.scalars(select(Document))}
    assert "https://shop.example/faq" in urls  # transient failure: keep the last good copy
    assert "https://shop.example/shipping" not in urls  # genuinely gone


def test_a_failed_recrawl_never_wipes_existing_knowledge(db, org, session_factory):
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)
    before = counts(db)
    outcome, _ = recrawl(db, session_factory, source, {"/": Resp(503)})
    assert outcome == "failed" and counts(db) == before


def test_recrawl_clears_the_previous_error_and_updates_the_timestamps(db, org, session_factory):
    source = make_source(db, org)
    crawl(session_factory, source.id, {"/": Resp(500)})
    assert reload(db, source).error_code == "http_error"
    recrawl(db, session_factory, source, SITE)
    source = reload(db, source)
    assert source.status == KnowledgeSourceStatus.COMPLETED and source.error_code is None
    assert source.last_crawled_at is not None


# --- deletion / cancellation --------------------------------------------------------------------------


def test_deleting_a_source_mid_crawl_stops_the_crawl_and_leaves_no_orphans(
    db, org, session_factory
):
    source = make_source(db, org)
    state = {"deleted": False}

    def delete_it():
        if not state["deleted"]:
            state["deleted"] = True
            with session_factory() as other:
                other.delete(other.get(KnowledgeSource, source.id))
                other.commit()

    outcome, _ = crawl(session_factory, source.id, SITE, embedder=CountingEmbedder(hook=delete_it))
    assert outcome in ("cancelled", "failed")
    db.expire_all()
    assert counts(db) == {"sources": 0, "documents": 0, "chunks": 0}


def test_should_stop_interrupts_the_crawl_and_marks_it(db, org, session_factory):
    source = make_source(db, org)
    pages = {"n": 0}

    def stop_after_first_page():
        pages["n"] += 1
        return pages["n"] > 1

    outcome, _ = crawl(session_factory, source.id, SITE, should_stop=stop_after_first_page)
    source = reload(db, source)
    assert outcome == "cancelled" and source.status == KnowledgeSourceStatus.FAILED
    assert source.error_code == "interrupted"


# --- reaper -----------------------------------------------------------------------------------------------


def test_stale_crawls_are_failed_and_fresh_ones_are_left_alone(db, org):
    now = datetime.now(UTC)
    stale = make_source(db, org, "https://a.example/", status=KnowledgeSourceStatus.CRAWLING)
    fresh = make_source(db, org, "https://b.example/", status=KnowledgeSourceStatus.CRAWLING)
    done = make_source(db, org, "https://c.example/", status=KnowledgeSourceStatus.COMPLETED)
    stale.crawl_heartbeat_at = now - timedelta(minutes=30)
    fresh.crawl_heartbeat_at = now - timedelta(seconds=10)
    db.commit()

    class Factory:
        def __call__(self):
            from sqlalchemy.orm import Session

            return Session(bind=db.get_bind(), expire_on_commit=False)

    assert fail_stale_crawls(Factory(), stale_after_seconds=900) == 1
    for source, expected in ((stale, "FAILED"), (fresh, "CRAWLING"), (done, "COMPLETED")):
        db.refresh(source)
        assert source.status == expected
    assert (
        stale.error_code == "interrupted" and stale.error_message == ERROR_MESSAGES["interrupted"]
    )


def test_orphaned_pending_sources_are_requeued_and_recent_ones_are_not(db, org):
    old = make_source(db, org, "https://a.example/")
    recent = make_source(db, org, "https://b.example/")
    running = make_source(db, org, "https://c.example/", status=KnowledgeSourceStatus.CRAWLING)
    db.execute(
        update(KnowledgeSource)
        .where(KnowledgeSource.id.in_([old.id, running.id]))
        .values(updated_at=datetime.now(UTC) - timedelta(hours=1))
    )
    db.commit()

    queue = InMemoryJobQueue()

    class Factory:
        def __call__(self):
            from sqlalchemy.orm import Session

            return Session(bind=db.get_bind(), expire_on_commit=False)

    assert requeue_orphaned_pending(Factory(), queue, older_than_seconds=300) == 1
    assert list(queue.jobs) == [CrawlJob(old.id)]
    assert CrawlJob(recent.id) not in queue.jobs and CrawlJob(running.id) not in queue.jobs


# --- tenant integrity at the database level ------------------------------------------------------------------


def test_chunks_and_documents_inherit_the_sources_organization(db, session_factory):
    org_a, org_b = make_org(db, "alpha"), make_org(db, "beta")
    a, b = (
        make_source(db, org_a, "https://a.example/"),
        make_source(db, org_b, "https://b.example/"),
    )
    crawl(session_factory, a.id, {"/": page("A", "alpha knowledge text here")})
    crawl(session_factory, b.id, {"/": page("B", "beta knowledge text here")})
    db.expire_all()
    for org, source, word in ((org_a, a, "alpha"), (org_b, b, "beta")):
        docs = db.scalars(select(Document).where(Document.knowledge_source_id == source.id)).all()
        assert docs and all(d.organization_id == org.id and word in d.content for d in docs)
        chunks = db.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.organization_id == org.id)
        ).all()
        assert chunks and all(word in c.content for c in chunks)


def test_a_chunk_cannot_point_at_another_organizations_document(db, org, session_factory):
    other = make_org(db, "intruder")
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)
    document = db.scalars(select(Document)).first()
    db.add(
        KnowledgeChunk(
            organization_id=other.id,  # wrong tenant for this document
            document_id=document.id,
            chunk_index=99,
            content="smuggled",
            character_count=8,
            embedding=[0.0] * 1536,
            embedding_model="fake:hash-v1",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_document_cannot_point_at_another_organizations_source(db, org):
    other = make_org(db, "intruder")
    source = make_source(db, org)
    db.add(
        Document(
            organization_id=other.id,
            knowledge_source_id=source.id,
            url="https://x.example/",
            content="x",
            content_hash="0" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_deleting_a_source_cascades_to_documents_and_chunks(db, org, session_factory):
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)
    assert counts(db)["chunks"] > 0
    db.delete(db.get(KnowledgeSource, source.id))
    db.commit()
    assert counts(db) == {"sources": 0, "documents": 0, "chunks": 0}


def test_deleting_an_organization_cascades_to_all_its_knowledge(db, org, session_factory):
    crawl(session_factory, make_source(db, org).id, SITE)
    db.delete(db.get(type(org), org.id))
    db.commit()
    assert counts(db) == {"sources": 0, "documents": 0, "chunks": 0}
