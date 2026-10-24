"""Bounded, polite, same-site website crawler.

``SiteCrawler.crawl()`` is a generator: it fetches one page, yields it to the caller (who chunks,
embeds and stores it), and only then fetches the next. Memory stays flat and a slow consumer
naturally throttles the crawl.

Scope and limits (all configurable via CrawlLimits):

* Same site only: the seed host plus its www/non-www twin. Other hosts, including subdomains and
  redirects to other domains, are never followed.
* Breadth-first with priorities: FAQ/about/contact/shipping/returns/policy pages first, then
  product-like pages, then everything else; query-string URLs go last. A page cap therefore can't
  be eaten by thousands of product variants before the policies are reached.
* At most ``max_pages`` requests, ``max_depth`` link hops, ``total_timeout`` seconds. Hitting a cap
  stops the crawl normally (it is not a failure) and is recorded in the stats.
* robots.txt is obeyed; ``delay`` seconds (or the site's Crawl-delay, capped) between requests.
"""

import hashlib
import heapq
import itertools
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from app.services.crawler.extract import extract_page
from app.services.crawler.fetcher import FetchError, SafeFetcher
from app.services.crawler.robots import RobotsPolicy
from app.services.crawler.urls import (
    UrlError,
    canonicalize_url,
    is_probably_asset,
    link_priority,
    site_hosts,
    url_host,
)

logger = logging.getLogger(__name__)

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
MAX_QUEUE_SIZE = 5000
MAX_LINKS_PER_PAGE = 500

# Fetch-failure codes that may disappear on retry. Documents from URLs that failed this way are
# kept on a re-crawl instead of being deleted as "no longer on the site".
TRANSIENT_FETCH_CODES = frozenset({"timeout", "connect", "dns", "bad_response", "tls"})


class CrawlError(Exception):
    """The crawl cannot proceed. ``code`` indexes the user-facing message catalogue."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CrawlCancelled(Exception):
    """The caller asked the crawl to stop (shutdown, or the source was deleted)."""


@dataclass(frozen=True)
class CrawlLimits:
    max_pages: int = 50
    max_depth: int = 3
    max_response_bytes: int = 2_000_000
    max_document_chars: int = 100_000
    min_content_chars: int = 40
    total_timeout: float = 600.0
    delay: float = 0.5


@dataclass(frozen=True)
class CrawledPage:
    url: str
    title: str | None
    text: str
    content_hash: str
    depth: int


@dataclass
class CrawlStats:
    pages_fetched: int = 0
    pages_extracted: int = 0
    skipped_robots: int = 0
    skipped_non_html: int = 0
    skipped_thin: int = 0
    skipped_duplicate: int = 0
    skipped_off_site: int = 0
    failed: int = 0
    stopped_by: str | None = None  # "page_limit" | "time_limit" | None (ran to completion)
    # URLs that failed transiently; excluded from stale-document cleanup. Not serialized.
    transient_failures: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "pages_fetched": self.pages_fetched,
            "pages_extracted": self.pages_extracted,
            "skipped_robots": self.skipped_robots,
            "skipped_non_html": self.skipped_non_html,
            "skipped_thin": self.skipped_thin,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_off_site": self.skipped_off_site,
            "failed": self.failed,
            "stopped_by": self.stopped_by,
        }


_SEED_FAILURES = {
    "blocked": "blocked_url",
    "off_site": "redirect_off_site",
    "dns": "dns_error",
    "connect": "unreachable",
    "timeout": "timeout",
    "tls": "tls_error",
    "too_large": "too_large",
    "too_many_redirects": "too_many_redirects",
    "unsupported_content": "not_html",
    "bad_response": "unreachable",
}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SiteCrawler:
    def __init__(
        self,
        start_url: str,
        fetcher: SafeFetcher,
        limits: CrawlLimits,
        *,
        robots_agent: str = "BizFlowBot",
        max_robots_delay: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self._seed = canonicalize_url(start_url)
        except UrlError:
            raise CrawlError("blocked_url", "seed rejected") from None
        self._hosts = site_hosts(url_host(self._seed))
        self._fetcher = fetcher
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._robots = RobotsPolicy(
            fetcher, robots_agent, self._host_allowed, max_delay=max_robots_delay
        )
        self.stats = CrawlStats()

    def _host_allowed(self, host: str) -> bool:
        return host in self._hosts

    def _seed_robots_failure(self, url: str) -> str:
        """Why the start page may not be crawled. If robots.txt itself was unreachable because the
        site could not be reached at all, report that real cause rather than blaming robots.txt."""
        if not self._robots.unreachable(url):
            return "robots_disallowed"
        reason = self._robots.failure_reason(url)
        if reason in ("blocked", "dns", "connect", "tls", "timeout"):
            return _SEED_FAILURES[reason]
        return "robots_unavailable"

    def _enqueue_links(
        self,
        links: list[str],
        base: str,
        depth: int,
        queued: set[str],
        heap: list[tuple[int, int, int, str]],
        counter: "itertools.count[int]",
    ) -> None:
        for href in links[:MAX_LINKS_PER_PAGE]:
            if len(heap) >= MAX_QUEUE_SIZE:
                return
            try:
                link = canonicalize_url(href, base=base)
            except UrlError:
                continue
            if link in queued or is_probably_asset(link):
                continue
            if not self._host_allowed(url_host(link)):
                self.stats.skipped_off_site += 1
                continue
            queued.add(link)
            heapq.heappush(heap, (link_priority(link), depth + 1, next(counter), link))

    def crawl(self, should_stop: Callable[[], bool] = lambda: False) -> Iterator[CrawledPage]:
        limits = self._limits
        stats = self.stats
        started = self._clock()
        counter = itertools.count()
        queued: set[str] = {self._seed}
        fetched_urls: set[str] = set()
        seen_hashes: set[str] = set()
        heap: list[tuple[int, int, int, str]] = [(0, 0, next(counter), self._seed)]
        yielded = 0

        while heap:
            if should_stop():
                raise CrawlCancelled
            if stats.pages_fetched >= limits.max_pages:
                stats.stopped_by = "page_limit"
                break
            if self._clock() - started >= limits.total_timeout:
                stats.stopped_by = "time_limit"
                break

            _, depth, _, url = heapq.heappop(heap)
            is_seed = url == self._seed

            if not self._robots.allowed(url):
                if is_seed:
                    raise CrawlError(self._seed_robots_failure(url))
                stats.skipped_robots += 1
                continue

            if stats.pages_fetched > 0:
                self._sleep(max(limits.delay, self._robots.crawl_delay(url)))
            stats.pages_fetched += 1

            try:
                result = self._fetcher.fetch(
                    url,
                    accept_types=HTML_TYPES,
                    host_allowed=self._host_allowed,
                    max_bytes=limits.max_response_bytes,
                )
            except FetchError as exc:
                logger.info("fetch failed host=%s code=%s", url_host(url), exc.code)
                if is_seed:
                    raise CrawlError(
                        _SEED_FAILURES.get(exc.code, "unreachable"), exc.detail
                    ) from None
                if exc.code == "unsupported_content":
                    stats.skipped_non_html += 1
                elif exc.code in ("off_site", "blocked"):
                    stats.skipped_off_site += 1
                else:
                    stats.failed += 1
                    if exc.code in TRANSIENT_FETCH_CODES:
                        stats.transient_failures.add(url)
                continue

            if not 200 <= result.status < 300:
                if is_seed:
                    raise CrawlError("http_error", f"status {result.status}")
                stats.failed += 1
                if result.status >= 500 or result.status == 429:
                    stats.transient_failures.add(url)
                continue

            final = result.url
            if final in fetched_urls:
                stats.skipped_duplicate += 1
                continue
            fetched_urls.add(final)
            queued.add(final)
            if final != url and not self._robots.allowed(final):
                if is_seed:
                    raise CrawlError("robots_disallowed")
                stats.skipped_robots += 1
                continue

            try:
                page = extract_page(
                    result.body, charset=result.charset, max_chars=limits.max_document_chars
                )
            except Exception:
                # One malformed page must not take down the crawl. Details go to the server log.
                logger.exception("extraction failed host=%s", url_host(url))
                if is_seed:
                    raise CrawlError("internal_error", "extraction failed") from None
                stats.failed += 1
                continue
            if depth < limits.max_depth:
                self._enqueue_links(page.links, final, depth, queued, heap, counter)

            if len(page.text) < limits.min_content_chars:
                stats.skipped_thin += 1
                continue
            digest = content_hash(page.text)
            if digest in seen_hashes:
                stats.skipped_duplicate += 1
                continue
            seen_hashes.add(digest)

            stats.pages_extracted += 1
            yielded += 1
            yield CrawledPage(final, page.title, page.text, digest, depth)

        if yielded == 0:
            raise CrawlError("no_content")
