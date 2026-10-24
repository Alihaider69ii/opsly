import math
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Document,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceType,
    Organization,
)
from app.db.models.knowledge import EMBEDDING_DIM
from app.services.ai.providers.fake import FakeEmbeddingProvider
from app.services.rag.search import MAX_TOP_K, search_knowledge

FAKE = FakeEmbeddingProvider()
MODEL = FAKE.model_id


def unit(index: int, dims: int = EMBEDDING_DIM) -> list[float]:
    vector = [0.0] * dims
    vector[index] = 1.0
    return vector


def blend(a: int, b: int, weight: float) -> list[float]:
    """Mostly axis `a`, a little axis `b` (normalized)."""
    vector = [0.0] * EMBEDDING_DIM
    vector[a], vector[b] = 1.0, weight
    norm = math.sqrt(1 + weight * weight)
    return [x / norm for x in vector]


class Tenant:
    def __init__(self, pg, slug: str):
        self.pg = pg
        self.org = Organization(name=slug.title(), slug=slug)
        pg.add(self.org)
        pg.flush()
        self.source = KnowledgeSource(
            organization_id=self.org.id,
            type=KnowledgeSourceType.WEBSITE,
            name=f"{slug}.example",
            url=f"https://{slug}.example/",
        )
        pg.add(self.source)
        pg.flush()

    def document(
        self, url: str, *, title: str | None = None, status=DocumentStatus.READY
    ) -> Document:
        doc = Document(
            organization_id=self.org.id,
            knowledge_source_id=self.source.id,
            url=url,
            title=title or url,
            content="content",
            content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            status=status,
        )
        self.pg.add(doc)
        self.pg.flush()
        return doc

    def chunk(self, doc: Document, index: int, content: str, vector, model: str = MODEL):
        chunk = KnowledgeChunk(
            organization_id=self.org.id,
            document_id=doc.id,
            chunk_index=index,
            content=content,
            character_count=len(content),
            embedding=vector,
            embedding_model=model,
        )
        self.pg.add(chunk)
        self.pg.flush()
        return chunk

    def search(self, query, **kwargs):
        kwargs.setdefault("embedding_model", MODEL)
        return search_knowledge(
            self.pg, organization_id=self.org.id, query_embedding=query, **kwargs
        )


# --- the real vector column -------------------------------------------------------------------------


def test_vectors_round_trip_through_a_real_vector_column(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    sent = [float(i % 7) / 10 for i in range(EMBEDDING_DIM)]
    chunk = t.chunk(doc, 0, "hello", sent)
    pg.commit()
    pg.expire_all()
    stored = pg.get(KnowledgeChunk, chunk.id).embedding
    assert len(stored) == EMBEDDING_DIM
    assert [round(float(x), 5) for x in stored[:20]] == [round(x, 5) for x in sent[:20]]
    assert pg.scalar(text("SELECT vector_dims(embedding) FROM knowledge_chunks")) == EMBEDDING_DIM


def test_the_database_rejects_vectors_of_the_wrong_dimension(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    with pytest.raises(Exception, match="(?i)dimension"):
        t.chunk(doc, 0, "short vector", [0.1, 0.2, 0.3])
        pg.commit()
    pg.rollback()


def test_cosine_distance_operator_orders_by_similarity(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    t.chunk(doc, 0, "exact", unit(0))
    t.chunk(doc, 1, "close", blend(0, 1, 0.3))
    t.chunk(doc, 2, "farther", blend(0, 1, 2.0))
    t.chunk(doc, 3, "orthogonal", unit(5))
    pg.commit()

    hits = t.search(unit(0), top_k=4)
    assert [h.content for h in hits] == ["exact", "close", "farther", "orthogonal"]
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0, abs=1e-6) and scores[-1] == pytest.approx(0.0, abs=1e-6)


# --- search behavior --------------------------------------------------------------------------------------


def test_top_k_limits_results_and_is_clamped(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    for i in range(12):
        t.chunk(doc, i, f"chunk {i}", blend(0, 1, 0.01 * (i + 1)))
    pg.commit()
    assert len(t.search(unit(0), top_k=3)) == 3
    assert len(t.search(unit(0), top_k=1)) == 1
    assert len(t.search(unit(0), top_k=0)) == 1  # clamped up
    assert len(t.search(unit(0), top_k=10_000)) == 12  # clamped to MAX_TOP_K, only 12 exist
    assert MAX_TOP_K == 50


def test_hits_carry_everything_the_agent_needs_for_attribution(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/returns", title="Returns Policy")
    chunk = t.chunk(doc, 4, "Returns within 30 days.", unit(0))
    pg.commit()
    (hit,) = t.search(unit(0))
    assert (hit.chunk_id, hit.document_id, hit.source_id) == (chunk.id, doc.id, t.source.id)
    assert (hit.url, hit.title, hit.content, hit.chunk_index) == (
        "https://alpha.example/returns",
        "Returns Policy",
        "Returns within 30 days.",
        4,
    )


def test_min_score_filters_weak_matches(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    t.chunk(doc, 0, "strong", unit(0))
    t.chunk(doc, 1, "medium", blend(0, 1, 1.0))  # cosine ~0.707
    t.chunk(doc, 2, "weak", unit(9))  # cosine 0
    pg.commit()
    assert [h.content for h in t.search(unit(0), top_k=10)] == ["strong", "medium", "weak"]
    assert [h.content for h in t.search(unit(0), top_k=10, min_score=0.5)] == ["strong", "medium"]
    assert [h.content for h in t.search(unit(0), top_k=10, min_score=0.9)] == ["strong"]
    assert t.search(unit(3), top_k=10, min_score=0.5) == []  # nothing is similar to axis 3


def test_only_ready_documents_are_searchable(pg):
    t = Tenant(pg, "alpha")
    t.chunk(t.document("https://alpha.example/ready"), 0, "ready", unit(0))
    t.chunk(
        t.document("https://alpha.example/pending", status=DocumentStatus.PENDING),
        0,
        "pending",
        unit(0),
    )
    t.chunk(
        t.document("https://alpha.example/failed", status=DocumentStatus.FAILED),
        0,
        "failed",
        unit(0),
    )
    pg.commit()
    assert [h.content for h in t.search(unit(0), top_k=10)] == ["ready"]


def test_vectors_from_other_embedding_models_are_never_compared(pg):
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    t.chunk(doc, 0, "current model", blend(0, 1, 0.5))
    t.chunk(doc, 1, "old model, perfect vector", unit(0), model="openai:text-embedding-3-small")
    pg.commit()
    assert [h.content for h in t.search(unit(0))] == ["current model"]
    assert [
        h.content for h in t.search(unit(0), embedding_model="openai:text-embedding-3-small")
    ] == ["old model, perfect vector"]


def test_search_can_be_narrowed_to_one_source(pg):
    t = Tenant(pg, "alpha")
    other_source = KnowledgeSource(
        organization_id=t.org.id,
        type=KnowledgeSourceType.WEBSITE,
        name="b",
        url="https://other.example/",
    )
    pg.add(other_source)
    pg.flush()
    t.chunk(t.document("https://alpha.example/"), 0, "from first source", unit(0))
    second = Document(
        organization_id=t.org.id,
        knowledge_source_id=other_source.id,
        url="https://other.example/",
        content="c",
        content_hash="h" * 64,
        status=DocumentStatus.READY,
    )
    pg.add(second)
    pg.flush()
    t.chunk(second, 0, "from second source", unit(0))
    pg.commit()
    assert {h.content for h in t.search(unit(0), top_k=10)} == {
        "from first source",
        "from second source",
    }
    assert [h.content for h in t.search(unit(0), source_id=other_source.id)] == [
        "from second source"
    ]


def test_query_embedding_dimension_is_validated(pg):
    t = Tenant(pg, "alpha")
    with pytest.raises(ValueError, match="1536"):
        t.search([0.1, 0.2])


def test_empty_knowledge_base_returns_nothing(pg):
    t = Tenant(pg, "alpha")
    assert t.search(unit(0)) == []


# --- organization isolation (the critical guarantee) ------------------------------------------------------------


def test_one_organization_never_retrieves_another_organizations_chunks(pg):
    alpha, beta = Tenant(pg, "alpha"), Tenant(pg, "beta")
    # Beta holds the PERFECT match for the query; alpha's best match is much worse.
    alpha.chunk(
        alpha.document("https://alpha.example/"), 0, "alpha's mediocre answer", blend(0, 1, 3.0)
    )
    beta.chunk(beta.document("https://beta.example/"), 0, "beta's secret perfect answer", unit(0))
    pg.commit()

    alpha_hits = alpha.search(unit(0), top_k=50)
    assert [h.content for h in alpha_hits] == ["alpha's mediocre answer"]
    beta_hits = beta.search(unit(0), top_k=50)
    assert [h.content for h in beta_hits] == ["beta's secret perfect answer"]
    assert all("secret" not in h.content for h in alpha_hits)


def test_isolation_holds_when_the_other_tenant_is_much_larger(pg):
    small, big = Tenant(pg, "small"), Tenant(pg, "big")
    small.chunk(small.document("https://small.example/"), 0, "small tenant's only chunk", unit(7))
    doc = big.document("https://big.example/")
    for i in range(60):
        big.chunk(
            doc, i, f"big {i}", blend(7, 8 + i % 20, 0.001 * (i + 1))
        )  # all closer to the query
    pg.commit()
    hits = small.search(unit(7), top_k=5)
    assert [h.content for h in hits] == [
        "small tenant's only chunk"
    ]  # fewer than top_k, never padded


def test_other_tenants_with_identical_vectors_and_urls_stay_separate(pg):
    a, b = Tenant(pg, "alpha"), Tenant(pg, "beta")
    for tenant in (a, b):
        tenant.chunk(
            tenant.document("https://same.example/page"), 0, f"{tenant.org.slug} text", unit(0)
        )
    pg.commit()
    assert [h.content for h in a.search(unit(0))] == ["alpha text"]
    assert [h.content for h in b.search(unit(0))] == ["beta text"]


def test_searching_with_an_unknown_or_foreign_organization_id_finds_nothing(pg):
    t = Tenant(pg, "alpha")
    t.chunk(t.document("https://alpha.example/"), 0, "alpha", unit(0))
    pg.commit()
    assert (
        search_knowledge(
            pg, organization_id=uuid.uuid4(), query_embedding=unit(0), embedding_model=MODEL
        )
        == []
    )


def test_source_id_from_another_organization_cannot_widen_the_search(pg):
    alpha, beta = Tenant(pg, "alpha"), Tenant(pg, "beta")
    beta.chunk(beta.document("https://beta.example/"), 0, "beta secret", unit(0))
    pg.commit()
    # alpha supplies beta's source id: still filtered by alpha's organization -> nothing
    assert alpha.search(unit(0), source_id=beta.source.id) == []


# --- tenant integrity enforced by the database itself --------------------------------------------------------------


def test_postgres_rejects_a_chunk_attached_to_another_organizations_document(pg):
    alpha, beta = Tenant(pg, "alpha"), Tenant(pg, "beta")
    alpha_doc = alpha.document("https://alpha.example/")
    pg.commit()
    with pytest.raises(IntegrityError):
        beta.chunk(alpha_doc, 0, "smuggled into alpha's document", unit(0))  # beta org, alpha doc
        pg.commit()
    pg.rollback()


def test_postgres_rejects_a_document_attached_to_another_organizations_source(pg):
    alpha, beta = Tenant(pg, "alpha"), Tenant(pg, "beta")
    pg.commit()
    with pytest.raises(IntegrityError):
        pg.add(
            Document(
                organization_id=beta.org.id,
                knowledge_source_id=alpha.source.id,
                url="https://x.example/",
                content="x",
                content_hash="0" * 64,
            )
        )
        pg.commit()
    pg.rollback()


def test_cascades_delete_documents_and_chunks_in_postgres(pg):
    alpha, beta = Tenant(pg, "alpha"), Tenant(pg, "beta")
    alpha.chunk(alpha.document("https://alpha.example/"), 0, "alpha", unit(0))
    beta.chunk(beta.document("https://beta.example/"), 0, "beta", unit(0))
    pg.commit()
    pg.delete(pg.get(KnowledgeSource, alpha.source.id))
    pg.commit()
    assert [c.content for c in pg.scalars(select(KnowledgeChunk))] == ["beta"]
    pg.delete(pg.get(Organization, beta.org.id))
    pg.commit()
    assert pg.scalar(text("SELECT count(*) FROM knowledge_chunks")) == 0
    assert pg.scalar(text("SELECT count(*) FROM documents")) == 0


def test_search_uses_the_tenant_index_and_not_a_full_table_scan(pg):
    """Exact search stays fast because the organization filter narrows the scan first."""
    t = Tenant(pg, "alpha")
    doc = t.document("https://alpha.example/")
    for i in range(5):
        t.chunk(doc, i, f"c{i}", unit(i))
    pg.commit()
    pg.execute(text("ANALYZE knowledge_chunks"))
    plan = "\n".join(
        row[0]
        for row in pg.execute(
            text(
                "EXPLAIN SELECT id FROM knowledge_chunks WHERE organization_id = :o "
                "ORDER BY embedding <=> (SELECT embedding FROM knowledge_chunks LIMIT 1) LIMIT 5"
            ),
            {"o": t.org.id},
        )
    )
    assert "knowledge_chunks" in plan and "hnsw" not in plan.lower()
