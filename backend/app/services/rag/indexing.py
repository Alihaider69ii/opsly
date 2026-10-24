"""Turning a crawled page into a stored document plus embedded chunks.

Each step runs in its own short transaction and no database transaction is held open while the
embedding provider is called over the network. A document is only READY once its chunks exist, and
a document that is not READY never has chunks, so retrieval can never see half-indexed content.
Unchanged pages (same content hash) are not re-chunked or re-embedded, *unless* their stored vectors
came from a different embedding model than the one now configured. Those pages are re-embedded and
their chunks swapped in a single transaction, so the old vectors are only replaced once the new ones
exist (a failed or interrupted re-embed leaves the previous vectors untouched). Nothing is ever
re-embedded or deleted automatically outside a crawl.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentStatus, KnowledgeChunk
from app.db.scoping import scoped_select
from app.services.ai.embeddings import EmbeddingError, EmbeddingProvider, embedding_input
from app.services.crawler.crawler import CrawledPage
from app.services.rag.chunking import chunk_text

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class IndexResult:
    document_id: uuid.UUID
    outcome: str  # "created" | "updated" | "unchanged" | "reindexed"
    chunks: int


def has_stale_chunks(
    db: Session, organization_id: uuid.UUID, document_id: uuid.UUID, model_id: str
) -> bool:
    """True when the document has chunks embedded by a model other than ``model_id``."""
    return bool(
        db.scalar(
            select(
                exists().where(
                    KnowledgeChunk.organization_id == organization_id,
                    KnowledgeChunk.document_id == document_id,
                    KnowledgeChunk.embedding_model != model_id,
                )
            )
        )
    )


def index_page(
    session_factory: SessionFactory,
    *,
    organization_id: uuid.UUID,
    source_id: uuid.UUID,
    page: CrawledPage,
    embedder: EmbeddingProvider,
    chunk_size: int,
    chunk_overlap: int,
) -> IndexResult:
    reindex: tuple[uuid.UUID, str | None, str] | None = None
    with session_factory() as db:
        document = db.scalar(
            scoped_select(Document, organization_id).where(
                Document.knowledge_source_id == source_id, Document.url == page.url
            )
        )
        if (
            document is not None
            and document.content_hash == page.content_hash
            and document.status == DocumentStatus.READY
        ):
            if not has_stale_chunks(db, organization_id, document.id, embedder.model_id):
                return IndexResult(document.id, "unchanged", 0)
            reindex = (document.id, document.title, document.content)

    if reindex is not None:
        return _reindex_document(
            session_factory,
            organization_id=organization_id,
            document_id=reindex[0],
            title=reindex[1],
            content=reindex[2],
            embedder=embedder,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    with session_factory() as db:
        document = db.scalar(
            scoped_select(Document, organization_id).where(
                Document.knowledge_source_id == source_id, Document.url == page.url
            )
        )
        outcome = "updated" if document is not None else "created"
        if document is None:
            document = Document(
                organization_id=organization_id,
                knowledge_source_id=source_id,
                url=page.url,
                title=page.title,
                content=page.text,
                content_hash=page.content_hash,
                status=DocumentStatus.PENDING,
            )
            db.add(document)
        else:
            document.title = page.title
            document.content = page.text
            document.content_hash = page.content_hash
            document.status = DocumentStatus.PENDING
            db.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.organization_id == organization_id,
                    KnowledgeChunk.document_id == document.id,
                )
            )
        db.commit()
        document_id = document.id

    chunks = chunk_text(page.text, chunk_size=chunk_size, overlap=chunk_overlap)
    try:
        vectors = embedder.embed([embedding_input(page.title, c.content) for c in chunks])
    except EmbeddingError:
        with session_factory() as db:
            failed = db.get(Document, document_id)
            if failed is not None:
                failed.status = DocumentStatus.FAILED
                db.commit()
        raise

    with session_factory() as db:
        db.add_all(
            KnowledgeChunk(
                organization_id=organization_id,
                document_id=document_id,
                chunk_index=chunk.index,
                content=chunk.content,
                character_count=len(chunk.content),
                embedding=vector,
                embedding_model=embedder.model_id,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        ready = db.get(Document, document_id)
        if ready is not None:
            ready.status = DocumentStatus.READY
        db.commit()
    return IndexResult(document_id, outcome, len(chunks))


def _reindex_document(
    session_factory: SessionFactory,
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str | None,
    content: str,
    embedder: EmbeddingProvider,
    chunk_size: int,
    chunk_overlap: int,
) -> IndexResult:
    """Re-embed an unchanged document with the current model. The embedding call happens with no
    transaction open and, if it fails, raises before anything is modified."""
    chunks = chunk_text(content, chunk_size=chunk_size, overlap=chunk_overlap)
    if not chunks:
        return IndexResult(document_id, "unchanged", 0)
    vectors = embedder.embed([embedding_input(title, c.content) for c in chunks])
    with session_factory() as db:  # delete + insert commit together or not at all
        db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.organization_id == organization_id,
                KnowledgeChunk.document_id == document_id,
            )
        )
        db.add_all(
            KnowledgeChunk(
                organization_id=organization_id,
                document_id=document_id,
                chunk_index=chunk.index,
                content=chunk.content,
                character_count=len(chunk.content),
                embedding=vector,
                embedding_model=embedder.model_id,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        db.commit()
    return IndexResult(document_id, "reindexed", len(chunks))


def delete_stale_documents(
    db: Session,
    *,
    organization_id: uuid.UUID,
    source_id: uuid.UUID,
    keep_ids: set[uuid.UUID],
    keep_urls: set[str],
) -> int:
    """Remove documents the latest crawl no longer found, but keep pages that merely failed
    transiently this time (``keep_urls``), so a flaky crawl cannot erase good knowledge."""
    stmt = delete(Document).where(
        Document.organization_id == organization_id,
        Document.knowledge_source_id == source_id,
    )
    if keep_ids:
        stmt = stmt.where(Document.id.not_in(keep_ids))
    if keep_urls:
        stmt = stmt.where(Document.url.not_in(keep_urls))
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def count_documents(db: Session, organization_id: uuid.UUID, source_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.organization_id == organization_id,
                Document.knowledge_source_id == source_id,
            )
        )
        or 0
    )
