"""Organization-scoped vector search over the knowledge base.

``search_knowledge`` is the single entry point for retrieval. It always starts from
``scoped_select(KnowledgeChunk, organization_id)``, so a query can never return another
organization's chunks, and it only compares vectors produced by the same embedding model.
Search is exact (no ANN index): see docs/knowledge-base.md for why.

PostgreSQL + pgvector only (the ``<=>`` cosine-distance operator); covered by the integration
test tier.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import Document, DocumentStatus, KnowledgeChunk
from app.db.models.knowledge import EMBEDDING_DIM
from app.db.scoping import scoped_select

logger = logging.getLogger(__name__)

MAX_TOP_K = 50


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    source_id: uuid.UUID
    url: str
    title: str | None
    content: str
    chunk_index: int
    score: float  # cosine similarity, 1.0 = identical direction


def count_stale_chunks(db: Session, organization_id: uuid.UUID, model_id: str) -> int:
    """Chunks of this organization that search cannot see because another model embedded them."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(
                KnowledgeChunk.organization_id == organization_id,
                KnowledgeChunk.embedding_model != model_id,
            )
        )
        or 0
    )


def search_knowledge(
    db: Session,
    *,
    organization_id: uuid.UUID,
    query_embedding: Sequence[float],
    embedding_model: str,
    top_k: int = 5,
    min_score: float | None = None,
    source_id: uuid.UUID | None = None,
) -> list[SearchHit]:
    if len(query_embedding) != EMBEDDING_DIM:
        raise ValueError(f"query embedding must have {EMBEDDING_DIM} dimensions")
    top_k = max(1, min(top_k, MAX_TOP_K))

    distance = KnowledgeChunk.embedding.cosine_distance(list(query_embedding))
    stmt = (
        scoped_select(KnowledgeChunk, organization_id)
        .join(
            Document,
            (Document.id == KnowledgeChunk.document_id)
            & (Document.organization_id == KnowledgeChunk.organization_id),
        )
        .where(
            Document.organization_id == organization_id,
            Document.status == DocumentStatus.READY,
            KnowledgeChunk.embedding_model == embedding_model,
        )
        .add_columns(Document.url, Document.title, Document.knowledge_source_id, distance)
        .order_by(distance, KnowledgeChunk.id)
        .limit(top_k)
    )
    if source_id is not None:
        stmt = stmt.where(Document.knowledge_source_id == source_id)
    if min_score is not None:
        stmt = stmt.where(distance <= 1.0 - min_score)

    hits = []
    for chunk, url, title, doc_source_id, dist in db.execute(stmt):
        hits.append(
            SearchHit(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                source_id=doc_source_id,
                url=url,
                title=title,
                content=chunk.content,
                chunk_index=chunk.chunk_index,
                score=1.0 - float(dist),
            )
        )
    if not hits:
        # "Nothing found" can mean no relevant knowledge, or that the embedding model changed and
        # the stored vectors belong to another one (invisible by design, never mixed). Say which.
        stale = count_stale_chunks(db, organization_id, embedding_model)
        if stale:
            log_event(
                logger,
                "rag.stale_vectors",
                level=logging.WARNING,
                organization=organization_id,
                stale_chunks=stale,
                embedding_model=embedding_model,
            )
    return hits
