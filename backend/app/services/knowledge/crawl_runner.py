"""Running one crawl job: claim -> crawl -> index each page -> finish.

Everything is keyed by the source row. The worker is a trusted internal process, so it loads the
source by id; the organization id it reads from that row is then used for every document and
chunk it writes. A source deleted mid-crawl is noticed on the next heartbeat or write and the
crawl stops quietly.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import KnowledgeSource, KnowledgeSourceStatus
from app.services.ai.embeddings import EmbeddingError, EmbeddingProvider
from app.services.crawler.crawler import CrawlCancelled, CrawlError, CrawlLimits, SiteCrawler
from app.services.crawler.fetcher import SafeFetcher
from app.services.crawler.urls import url_host
from app.services.knowledge.errors import error_message
from app.services.rag.indexing import SessionFactory, delete_stale_documents, index_page
from app.workers.queue import CrawlJob, JobQueue

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def claim_source(db: Session, source_id: uuid.UUID) -> KnowledgeSource | None:
    """Atomically move PENDING -> CRAWLING. Returns None if the source is gone or already taken."""
    now = utcnow()
    result = db.execute(
        update(KnowledgeSource)
        .where(
            KnowledgeSource.id == source_id, KnowledgeSource.status == KnowledgeSourceStatus.PENDING
        )
        .values(
            status=KnowledgeSourceStatus.CRAWLING,
            crawl_started_at=now,
            crawl_heartbeat_at=now,
            error_code=None,
            error_message=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return None
    return db.get(KnowledgeSource, source_id)


def _heartbeat(session_factory: SessionFactory, source_id: uuid.UUID) -> bool:
    """Record liveness. False means the source is gone (deleted) or no longer CRAWLING."""
    with session_factory() as db:
        result = db.execute(
            update(KnowledgeSource)
            .where(
                KnowledgeSource.id == source_id,
                KnowledgeSource.status == KnowledgeSourceStatus.CRAWLING,
            )
            .values(crawl_heartbeat_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1  # type: ignore[attr-defined]


def _finish(
    session_factory: SessionFactory,
    source_id: uuid.UUID,
    *,
    error_code: str | None,
    stats: dict[str, object] | None,
) -> None:
    values: dict[str, object] = {
        "status": KnowledgeSourceStatus.FAILED if error_code else KnowledgeSourceStatus.COMPLETED,
        "error_code": error_code,
        "error_message": error_message(error_code) if error_code else None,
        "crawl_stats": stats,
    }
    if error_code is None:
        values["last_crawled_at"] = utcnow()
    with session_factory() as db:
        db.execute(
            update(KnowledgeSource)
            .where(KnowledgeSource.id == source_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        db.commit()


def run_crawl(
    session_factory: SessionFactory,
    source_id: uuid.UUID,
    *,
    settings: Settings,
    limits: CrawlLimits,
    fetcher_factory: Callable[[], SafeFetcher],
    embedder_factory: Callable[[], EmbeddingProvider],
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Execute one crawl. Returns the outcome: skipped | cancelled | completed | failed."""
    with session_factory() as db:
        source = claim_source(db, source_id)
        if source is None:
            logger.info("crawl skipped (not pending) source=%s", source_id)
            return "skipped"
        organization_id, start_url = source.organization_id, source.url or ""

    host = url_host(start_url)
    logger.info("crawl started source=%s org=%s host=%s", source_id, organization_id, host)
    stats_dict: dict[str, object] | None = None
    fetcher: SafeFetcher | None = None
    try:
        embedder = embedder_factory()
        fetcher = fetcher_factory()
        crawler_kwargs = {"sleep": sleep} if sleep is not None else {}
        crawler = SiteCrawler(
            start_url,
            fetcher,
            limits,
            robots_agent=settings.crawl_robots_agent,
            max_robots_delay=settings.crawl_max_robots_delay_seconds,
            **crawler_kwargs,  # type: ignore[arg-type]
        )

        def stop() -> bool:
            return should_stop() or not _heartbeat(session_factory, source_id)

        keep_ids: set[uuid.UUID] = set()
        chunks_total = 0
        reindexed = 0
        for page in crawler.crawl(should_stop=stop):
            result = index_page(
                session_factory,
                organization_id=organization_id,
                source_id=source_id,
                page=page,
                embedder=embedder,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )
            keep_ids.add(result.document_id)
            chunks_total += result.chunks
            reindexed += result.outcome == "reindexed"

        stats_dict = {
            **crawler.stats.as_dict(),
            "documents": len(keep_ids),
            "chunks_created": chunks_total,
            "documents_reindexed": reindexed,
        }
        with session_factory() as db:
            removed = delete_stale_documents(
                db,
                organization_id=organization_id,
                source_id=source_id,
                keep_ids=keep_ids,
                keep_urls=crawler.stats.transient_failures,
            )
        stats_dict["documents_removed"] = removed
        _finish(session_factory, source_id, error_code=None, stats=stats_dict)
        logger.info(
            "crawl completed source=%s documents=%d chunks=%d",
            source_id,
            len(keep_ids),
            chunks_total,
        )
        return "completed"
    except CrawlCancelled:
        # Either the source was deleted (nothing to update) or the worker is shutting down.
        _finish(session_factory, source_id, error_code="interrupted", stats=stats_dict)
        logger.info("crawl cancelled source=%s", source_id)
        return "cancelled"
    except CrawlError as exc:
        logger.info("crawl failed source=%s code=%s detail=%s", source_id, exc.code, exc.detail)
        _finish(session_factory, source_id, error_code=exc.code, stats=stats_dict)
        return "failed"
    except EmbeddingError as exc:
        logger.error("embedding failed source=%s code=%s", source_id, exc.code)
        unconfigured = exc.code in ("unconfigured", "misconfigured")
        _finish(
            session_factory,
            source_id,
            error_code="embedding_unconfigured" if unconfigured else "embedding_failed",
            stats=stats_dict,
        )
        return "failed"
    except IntegrityError:
        # The source was deleted while pages were being written (foreign key violation).
        logger.info("crawl stopped, source deleted source=%s", source_id)
        return "cancelled"
    except Exception:
        logger.exception("crawl crashed source=%s", source_id)  # technical detail: server log only
        _finish(session_factory, source_id, error_code="internal_error", stats=stats_dict)
        return "failed"
    finally:
        if fetcher is not None:
            fetcher.close()


def fail_stale_crawls(session_factory: SessionFactory, stale_after_seconds: int) -> int:
    """Fail CRAWLING sources whose worker stopped heart-beating (crash, kill, lost connection)."""
    cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
    with session_factory() as db:
        ids = list(
            db.scalars(
                select(KnowledgeSource.id).where(
                    KnowledgeSource.status == KnowledgeSourceStatus.CRAWLING,
                    KnowledgeSource.crawl_heartbeat_at < cutoff,
                )
            )
        )
        if ids:
            db.execute(
                update(KnowledgeSource)
                .where(
                    KnowledgeSource.id.in_(ids),
                    KnowledgeSource.status == KnowledgeSourceStatus.CRAWLING,
                )
                .values(
                    status=KnowledgeSourceStatus.FAILED,
                    error_code="interrupted",
                    error_message=error_message("interrupted"),
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
    if ids:
        logger.warning("failed %d stale crawl(s)", len(ids))
    return len(ids)


def requeue_orphaned_pending(
    session_factory: SessionFactory, queue: JobQueue, older_than_seconds: int
) -> int:
    """Re-enqueue PENDING sources that have waited suspiciously long (job lost with Redis).
    Duplicates are harmless: claiming is atomic."""
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    with session_factory() as db:
        ids = list(
            db.scalars(
                select(KnowledgeSource.id).where(
                    KnowledgeSource.status == KnowledgeSourceStatus.PENDING,
                    KnowledgeSource.updated_at < cutoff,
                )
            )
        )
    for source_id in ids:
        queue.enqueue(CrawlJob(source_id))
    if ids:
        logger.warning("re-queued %d orphaned pending crawl(s)", len(ids))
    return len(ids)
