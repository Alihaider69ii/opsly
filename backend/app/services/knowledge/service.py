"""Knowledge source lifecycle: create, list, inspect, re-crawl, delete.

Every query starts from ``scoped_select`` or filters on ``organization_id`` taken from a verified
``OrgContext``; ids in URLs are never trusted on their own. A source id belonging to another
organization behaves exactly like one that does not exist.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    Document,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
)
from app.db.scoping import scoped_select
from app.services.crawler.urls import UrlError, url_host
from app.services.crawler.validation import validate_seed_url
from app.services.knowledge.errors import error_message
from app.services.organizations.service import OrgContext
from app.workers.queue import CrawlJob, JobQueue

logger = logging.getLogger(__name__)

IMPLEMENTED_TYPES = frozenset({KnowledgeSourceType.WEBSITE})
PREVIEW_CHARS = 300


class SourceNotFound(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class UnsupportedSourceType(Exception):
    pass


class InvalidSourceUrl(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DuplicateSource(Exception):
    pass


class TooManySources(Exception):
    pass


class CrawlInProgress(Exception):
    pass


class CrawlTooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("crawl requested too soon")
        self.retry_after = retry_after


@dataclass(frozen=True)
class SourceView:
    source: KnowledgeSource
    document_count: int


@dataclass(frozen=True)
class DocumentRow:
    id: uuid.UUID
    url: str
    title: str | None
    status: str
    character_count: int
    chunk_count: int
    preview: str
    updated_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _enqueue(db: Session, source: KnowledgeSource, queue: JobQueue) -> None:
    """Queue a crawl; if the queue is down, fail the source visibly rather than leave it PENDING."""
    try:
        queue.enqueue(CrawlJob(source.id))
    except Exception:
        logger.exception("could not enqueue crawl source=%s", source.id)
        source.status = KnowledgeSourceStatus.FAILED
        source.error_code = "queue_unavailable"
        source.error_message = error_message("queue_unavailable")
        db.commit()


def document_count(db: Session, organization_id: uuid.UUID, source_id: uuid.UUID) -> int:
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


def create_website_source(
    db: Session,
    ctx: OrgContext,
    *,
    source_type: KnowledgeSourceType,
    url: str,
    name: str | None,
    queue: JobQueue,
    settings: Settings,
) -> SourceView:
    if source_type not in IMPLEMENTED_TYPES:
        raise UnsupportedSourceType
    try:
        canonical = validate_seed_url(url, settings.crawl_allowed_port_set)
    except UrlError as exc:
        raise InvalidSourceUrl(str(exc)) from None

    org_id = ctx.organization.id
    existing = db.scalar(
        select(func.count())
        .select_from(KnowledgeSource)
        .where(KnowledgeSource.organization_id == org_id)
    )
    if (existing or 0) >= settings.knowledge_max_sources_per_org:
        raise TooManySources

    host = url_host(canonical)
    source = KnowledgeSource(
        organization_id=org_id,
        type=source_type,
        name=(name or host.removeprefix("www."))[:200],
        url=canonical,
        status=KnowledgeSourceStatus.PENDING,
    )
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DuplicateSource from None
    db.refresh(source)
    _enqueue(db, source, queue)
    logger.info("knowledge source created source=%s org=%s host=%s", source.id, org_id, host)
    return SourceView(source, 0)


def list_sources(db: Session, ctx: OrgContext) -> list[SourceView]:
    org_id = ctx.organization.id
    sources = list(
        db.scalars(
            scoped_select(KnowledgeSource, org_id).order_by(
                KnowledgeSource.created_at, KnowledgeSource.id
            )
        )
    )
    counts: dict[uuid.UUID, int] = dict(
        db.execute(
            select(Document.knowledge_source_id, func.count())
            .where(Document.organization_id == org_id)
            .group_by(Document.knowledge_source_id)
        ).all()  # type: ignore[arg-type]
    )
    return [SourceView(s, int(counts.get(s.id, 0))) for s in sources]


def get_source(db: Session, ctx: OrgContext, source_id: uuid.UUID) -> SourceView:
    org_id = ctx.organization.id
    source = db.scalar(
        scoped_select(KnowledgeSource, org_id).where(KnowledgeSource.id == source_id)
    )
    if source is None:
        raise SourceNotFound
    return SourceView(source, document_count(db, org_id, source.id))


def request_crawl(
    db: Session, ctx: OrgContext, source_id: uuid.UUID, *, queue: JobQueue, settings: Settings
) -> SourceView:
    org_id = ctx.organization.id
    view = get_source(db, ctx, source_id)
    source = view.source

    if source.status in (KnowledgeSourceStatus.PENDING, KnowledgeSourceStatus.CRAWLING):
        raise CrawlInProgress
    if source.crawl_started_at is not None:
        wait = (
            settings.crawl_min_interval_seconds
            - (_utcnow() - _as_utc(source.crawl_started_at)).total_seconds()
        )
        if wait > 0:
            raise CrawlTooSoon(int(wait) + 1)

    # Atomic transition: only a source that is still idle (COMPLETED/FAILED) can be queued.
    result = db.execute(
        update(KnowledgeSource)
        .where(
            KnowledgeSource.id == source.id,
            KnowledgeSource.organization_id == org_id,
            KnowledgeSource.status.in_(
                [KnowledgeSourceStatus.COMPLETED, KnowledgeSourceStatus.FAILED]
            ),
        )
        .values(status=KnowledgeSourceStatus.PENDING, error_code=None, error_message=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise CrawlInProgress
    db.refresh(source)
    _enqueue(db, source, queue)
    return SourceView(source, view.document_count)


def delete_source(db: Session, ctx: OrgContext, source_id: uuid.UUID) -> None:
    """Deletes the source; documents and chunks are removed by ON DELETE CASCADE."""
    result = db.execute(
        delete(KnowledgeSource).where(
            KnowledgeSource.id == source_id,
            KnowledgeSource.organization_id == ctx.organization.id,
        )
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise SourceNotFound
    logger.info("knowledge source deleted source=%s org=%s", source_id, ctx.organization.id)


def list_documents(
    db: Session, ctx: OrgContext, source_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[DocumentRow], int]:
    org_id = ctx.organization.id
    get_source(db, ctx, source_id)  # 404s for unknown or foreign sources

    chunk_counts = (
        select(KnowledgeChunk.document_id, func.count().label("n"))
        .where(KnowledgeChunk.organization_id == org_id)
        .group_by(KnowledgeChunk.document_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Document.id,
            Document.url,
            Document.title,
            Document.status,
            func.length(Document.content),
            func.coalesce(chunk_counts.c.n, 0),
            func.substr(Document.content, 1, PREVIEW_CHARS),
            Document.updated_at,
        )
        .outerjoin(chunk_counts, chunk_counts.c.document_id == Document.id)
        .where(Document.organization_id == org_id, Document.knowledge_source_id == source_id)
        .order_by(Document.url)
        .limit(limit)
        .offset(offset)
    ).all()
    total = document_count(db, org_id, source_id)
    return (
        [
            DocumentRow(
                id=r[0],
                url=r[1],
                title=r[2],
                status=str(r[3]),
                character_count=int(r[4] or 0),
                chunk_count=int(r[5]),
                preview=r[6] or "",
                updated_at=r[7],
            )
            for r in rows
        ],
        total,
    )


def get_document(
    db: Session, ctx: OrgContext, source_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[Document, int]:
    org_id = ctx.organization.id
    document = db.scalar(
        scoped_select(Document, org_id).where(
            Document.id == document_id, Document.knowledge_source_id == source_id
        )
    )
    if document is None:
        raise DocumentNotFound
    chunks = db.scalar(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.organization_id == org_id, KnowledgeChunk.document_id == document.id)
    )
    return document, int(chunks or 0)
