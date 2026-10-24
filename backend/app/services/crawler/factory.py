from app.core.config import Settings
from app.services.crawler.crawler import CrawlLimits
from app.services.crawler.fetcher import FetchLimits, SafeFetcher


def build_fetcher(settings: Settings) -> SafeFetcher:
    """The production fetcher: strict public-address policy, configured ports and limits."""
    return SafeFetcher(
        user_agent=settings.crawl_user_agent,
        limits=FetchLimits(
            max_bytes=settings.crawl_max_response_bytes,
            max_redirects=settings.crawl_max_redirects,
            connect_timeout=settings.crawl_connect_timeout_seconds,
            read_timeout=settings.crawl_read_timeout_seconds,
            request_timeout=settings.crawl_request_timeout_seconds,
        ),
        allowed_ports=settings.crawl_allowed_port_set,
    )


def build_limits(settings: Settings) -> CrawlLimits:
    return CrawlLimits(
        max_pages=settings.crawl_max_pages,
        max_depth=settings.crawl_max_depth,
        max_response_bytes=settings.crawl_max_response_bytes,
        max_document_chars=settings.crawl_max_document_chars,
        min_content_chars=settings.crawl_min_content_chars,
        total_timeout=settings.crawl_total_timeout_seconds,
        delay=settings.crawl_delay_seconds,
    )
