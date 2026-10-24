"""Changing the embedding model must never mix incompatible vectors and never destroy data.

Search only compares vectors tagged with the current ``model_id``. A page whose vectors came from a
different model is invisible to search until it is re-embedded; the re-embedding happens on the next
crawl (never automatically) and replaces a page's chunks only after the new vectors exist.
"""

import pytest
from sqlalchemy import select, update

from app.db.models import (
    Document,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceStatus,
)
from app.services.rag.search import count_stale_chunks
from tests.crawler.sites import page
from tests.knowledge.helpers import CountingEmbedder, crawl, make_org, make_source

SITE = {
    "/": page("Home", "Welcome to the Brand shop. We sell soap.", ("/faq", "/shipping")),
    "/faq": page("FAQ", "Do you ship worldwide? Yes, we ship to most countries."),
    "/shipping": page("Shipping", "Orders ship within two business days. Shipping is free."),
}
OLD_MODEL = "fake:hash-v1"
NEW_MODEL = "other:model-2"


class OtherModelEmbedder(CountingEmbedder):
    @property
    def model_id(self) -> str:
        return NEW_MODEL


@pytest.fixture()
def org(db):
    return make_org(db)


def recrawl(db, session_factory, source, **kwargs):
    db.execute(
        update(KnowledgeSource)
        .where(KnowledgeSource.id == source.id)
        .values(status=KnowledgeSourceStatus.PENDING)
    )
    db.commit()
    return crawl(session_factory, source.id, SITE, **kwargs)


def models_by_document(db) -> dict[str, set[str]]:
    db.expire_all()
    result: dict[str, set[str]] = {}
    for url, model in db.execute(
        select(Document.url, KnowledgeChunk.embedding_model).join(
            KnowledgeChunk, KnowledgeChunk.document_id == Document.id
        )
    ):
        result.setdefault(url, set()).add(model)
    return result


def seeded(db, session_factory, org):
    source = make_source(db, org)
    crawl(session_factory, source.id, SITE)  # embedded with the default (fake) model
    return source


def test_switching_models_reembeds_unchanged_pages_on_the_next_crawl(db, org, session_factory):
    source = seeded(db, session_factory, org)
    chunk_count = len(db.scalars(select(KnowledgeChunk)).all())
    assert (
        count_stale_chunks(db, org.id, NEW_MODEL) == chunk_count
    )  # all invisible to the new model

    embedder = OtherModelEmbedder()
    outcome, _ = recrawl(db, session_factory, source, embedder=embedder)

    assert outcome == "completed" and embedder.calls == 3  # one embedding call per page
    assert set(map(frozenset, models_by_document(db).values())) == {frozenset({NEW_MODEL})}
    assert count_stale_chunks(db, org.id, NEW_MODEL) == 0
    assert len(db.scalars(select(KnowledgeChunk)).all()) == chunk_count  # replaced, not duplicated
    db.expire_all()
    stats = db.get(KnowledgeSource, source.id).crawl_stats
    assert stats["documents_reindexed"] == 3 and stats["documents"] == 3
    assert all(d.status == DocumentStatus.READY for d in db.scalars(select(Document)))


def test_a_crawl_with_the_same_model_does_not_reembed_anything(db, org, session_factory):
    source = seeded(db, session_factory, org)
    embedder = CountingEmbedder()
    recrawl(db, session_factory, source, embedder=embedder)
    assert embedder.calls == 0
    db.expire_all()
    assert db.get(KnowledgeSource, source.id).crawl_stats["documents_reindexed"] == 0


def test_a_failed_reembedding_leaves_the_previous_vectors_untouched(db, org, session_factory):
    source = seeded(db, session_factory, org)
    before = {c.id: c.embedding_model for c in db.scalars(select(KnowledgeChunk))}

    outcome, _ = recrawl(db, session_factory, source, embedder=OtherModelEmbedder(fail_after=0))

    assert outcome == "failed"
    db.expire_all()
    after = {c.id: c.embedding_model for c in db.scalars(select(KnowledgeChunk))}
    assert after == before  # not a single chunk was deleted or rewritten
    assert all(m == OLD_MODEL for m in after.values())
    assert all(d.status == DocumentStatus.READY for d in db.scalars(select(Document)))
    assert db.get(KnowledgeSource, source.id).error_code == "embedding_failed"


def test_an_interrupted_reembedding_never_leaves_a_page_half_converted(db, org, session_factory):
    source = seeded(db, session_factory, org)
    recrawl(db, session_factory, source, embedder=OtherModelEmbedder(fail_after=1))

    per_document = models_by_document(db)
    assert len(per_document) == 3
    assert all(len(models) == 1 for models in per_document.values())  # one model per page, always
    assert {m for models in per_document.values() for m in models} == {OLD_MODEL, NEW_MODEL}

    # the next crawl finishes the job
    recrawl(db, session_factory, source, embedder=OtherModelEmbedder())
    assert {m for models in models_by_document(db).values() for m in models} == {NEW_MODEL}


def test_switching_back_to_the_old_model_converts_again(db, org, session_factory):
    source = seeded(db, session_factory, org)
    recrawl(db, session_factory, source, embedder=OtherModelEmbedder())
    recrawl(db, session_factory, source, embedder=CountingEmbedder())  # back to fake:hash-v1
    assert {m for models in models_by_document(db).values() for m in models} == {OLD_MODEL}


def test_a_page_that_changed_and_whose_model_changed_takes_the_normal_update_path(
    db, org, session_factory
):
    source = seeded(db, session_factory, org)
    changed = {**SITE, "/faq": page("FAQ", "A brand new answer about shipping to everywhere.")}
    db.execute(
        update(KnowledgeSource)
        .where(KnowledgeSource.id == source.id)
        .values(status=KnowledgeSourceStatus.PENDING)
    )
    db.commit()
    crawl(session_factory, source.id, changed, embedder=OtherModelEmbedder())
    db.expire_all()
    faq = db.scalar(select(Document).where(Document.url.endswith("/faq")))
    chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == faq.id)).all()
    assert "brand new answer" in faq.content
    assert chunks and {c.embedding_model for c in chunks} == {NEW_MODEL}


def test_staleness_is_counted_per_organization(db, org, session_factory):
    seeded(db, session_factory, org)
    other = make_org(db, "other")
    assert count_stale_chunks(db, other.id, NEW_MODEL) == 0  # another tenant's vectors are not mine
    assert count_stale_chunks(db, org.id, OLD_MODEL) == 0
    assert count_stale_chunks(db, org.id, NEW_MODEL) > 0
