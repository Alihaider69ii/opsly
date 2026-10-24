import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    Document,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
    Organization,
)
from app.services.ai.embeddings import EmbeddingError
from app.services.ai.providers.fake import FakeEmbeddingProvider
from app.services.crawler.crawler import CrawlLimits
from app.services.knowledge.crawl_runner import run_crawl
from tests.crawler.sites import FakeSite, Route, fetcher_for

SEED = "https://shop.example/"
LIMITS = CrawlLimits(max_pages=50, max_depth=3, delay=0, min_content_chars=10)


class CountingEmbedder(FakeEmbeddingProvider):
    """Records what was embedded; can fail on demand or run a hook at the first call."""

    def __init__(self, *, fail_after: int | None = None, hook: Callable[[], None] | None = None):
        super().__init__()
        self.calls = 0
        self.texts: list[str] = []
        self.fail_after = fail_after
        self.hook = hook

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.hook is not None:
            self.hook()
        if self.fail_after is not None and self.calls > self.fail_after:
            raise EmbeddingError("unavailable", "boom")
        self.texts.extend(texts)
        return super().embed(texts)


def make_org(db: Session, slug: str = "brand") -> Organization:
    org = Organization(name=slug.title(), slug=slug)
    db.add(org)
    db.commit()
    return org


def make_source(
    db: Session,
    org: Organization,
    url: str = SEED,
    *,
    status: KnowledgeSourceStatus = KnowledgeSourceStatus.PENDING,
) -> KnowledgeSource:
    source = KnowledgeSource(
        organization_id=org.id,
        type=KnowledgeSourceType.WEBSITE,
        name="shop.example",
        url=url,
        status=status,
    )
    db.add(source)
    db.commit()
    return source


def settings_for_runner(**overrides) -> Settings:
    return get_settings().model_copy(update=overrides)


def crawl(
    session_factory,
    source_id: uuid.UUID,
    routes: dict[str, Route] | FakeSite,
    *,
    embedder: FakeEmbeddingProvider | None = None,
    limits: CrawlLimits = LIMITS,
    should_stop: Callable[[], bool] = lambda: False,
    resolver=None,
    **setting_overrides,
) -> tuple[str, FakeSite]:
    site = routes if isinstance(routes, FakeSite) else FakeSite(routes)
    fetcher_kwargs = {"resolver": resolver} if resolver is not None else {}
    outcome = run_crawl(
        session_factory,
        source_id,
        settings=settings_for_runner(**setting_overrides),
        limits=limits,
        fetcher_factory=lambda: fetcher_for(site, **fetcher_kwargs),
        embedder_factory=lambda: embedder or FakeEmbeddingProvider(),
        should_stop=should_stop,
        sleep=lambda seconds: None,
    )
    return outcome, site


def counts(db: Session) -> dict[str, int]:
    return {
        "sources": db.scalar(select(func.count()).select_from(KnowledgeSource)) or 0,
        "documents": db.scalar(select(func.count()).select_from(Document)) or 0,
        "chunks": db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0,
    }
