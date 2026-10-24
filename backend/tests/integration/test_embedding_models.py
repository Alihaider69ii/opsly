"""Changing the embedding model on real pgvector.

Vectors from two models must never be compared. After a switch the old vectors stay in the table
(nothing is destroyed) but are invisible to search until a re-crawl re-embeds the page, and the
operator is told why search came back empty.
"""

import logging

import pytest
from sqlalchemy import select, update

from app.db.models import (
    Document,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceStatus,
)
from app.services.ai.providers.fake import FakeEmbeddingProvider
from app.services.rag.search import count_stale_chunks, search_knowledge
from tests.crawler.sites import page
from tests.knowledge.helpers import CountingEmbedder, crawl, make_org, make_source

SITE = {
    "/": page("Home", "Welcome to the Brand shop. We sell handmade soap.", ("/faq", "/shipping")),
    "/faq": page("FAQ", "Do you ship worldwide? Yes, we ship to most countries."),
    "/shipping": page("Shipping", "Orders ship within two business days. Shipping is free."),
}
OLD = FakeEmbeddingProvider()
NEW_MODEL = "other:model-2"


class OtherModel(CountingEmbedder):
    @property
    def model_id(self) -> str:
        return NEW_MODEL


@pytest.fixture()
def org(pg):
    return make_org(pg)


def recrawl(pg, pg_factory, source, embedder):
    pg.execute(
        update(KnowledgeSource)
        .where(KnowledgeSource.id == source.id)
        .values(status=KnowledgeSourceStatus.PENDING)
    )
    pg.commit()
    return crawl(pg_factory, source.id, SITE, embedder=embedder)


def hits(pg, org, embedder, text):
    return search_knowledge(
        pg,
        organization_id=org.id,
        query_embedding=embedder.embed([text])[0],
        embedding_model=embedder.model_id,
        top_k=5,
    )


def test_old_vectors_are_invisible_to_a_new_model_then_visible_after_a_recrawl(
    pg, pg_factory, org, caplog
):
    source = make_source(pg, org)
    crawl(pg_factory, source.id, SITE, embedder=CountingEmbedder())
    pg.expire_all()
    chunks_before = len(pg.scalars(select(KnowledgeChunk)).all())
    assert chunks_before >= 3

    new = OtherModel()
    assert hits(
        pg, org, CountingEmbedder(), "do you ship worldwide"
    )  # the old model still finds it
    caplog.set_level(logging.WARNING)
    assert hits(pg, org, new, "do you ship worldwide") == []  # never compared across models
    assert any("rag.stale_vectors" in r.getMessage() for r in caplog.records)
    assert count_stale_chunks(pg, org.id, NEW_MODEL) == chunks_before
    pg.expire_all()
    assert len(pg.scalars(select(KnowledgeChunk)).all()) == chunks_before  # nothing was destroyed

    queries_so_far = new.calls
    outcome, _ = recrawl(pg, pg_factory, source, new)
    assert outcome == "completed" and new.calls - queries_so_far == 3  # one call per page

    pg.expire_all()
    models = set(pg.scalars(select(KnowledgeChunk.embedding_model)))
    assert models == {NEW_MODEL}  # replaced page by page, not mixed
    assert len(pg.scalars(select(KnowledgeChunk)).all()) == chunks_before
    found = hits(pg, org, new, "do you ship worldwide")
    assert found and any("ship" in h.content.lower() for h in found)
    assert count_stale_chunks(pg, org.id, NEW_MODEL) == 0
    assert all(d.status.value == "READY" for d in pg.scalars(select(Document)))


def test_a_failed_reembedding_keeps_the_old_vectors_searchable(pg, pg_factory, org):
    source = make_source(pg, org)
    crawl(pg_factory, source.id, SITE, embedder=CountingEmbedder())
    failing = OtherModel(fail_after=0)  # the new provider is down from the first call

    recrawl(pg, pg_factory, source, failing)

    pg.expire_all()
    assert set(pg.scalars(select(KnowledgeChunk.embedding_model))) == {OLD.model_id}
    assert hits(pg, org, CountingEmbedder(), "do you ship worldwide")  # the old index still answers


def test_two_organizations_with_different_models_do_not_see_each_others_vectors(pg, pg_factory):
    one, two = make_org(pg, "one"), make_org(pg, "two")
    s1, s2 = make_source(pg, one), make_source(pg, two)
    crawl(pg_factory, s1.id, SITE, embedder=CountingEmbedder())
    crawl(pg_factory, s2.id, SITE, embedder=OtherModel())

    assert hits(pg, one, CountingEmbedder(), "free shipping") != []
    assert hits(pg, one, OtherModel(), "free shipping") == []
    assert hits(pg, two, OtherModel(), "free shipping") != []
    assert hits(pg, two, CountingEmbedder(), "free shipping") == []
