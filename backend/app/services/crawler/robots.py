"""robots.txt handling (RFC 9309).

Behavior, per origin (scheme + host + port), fetched once per crawl through the same SSRF-hardened
fetcher as everything else:

* 2xx            -> parsed. Rules use longest-match precedence and ``*`` / ``$`` wildcards
                    (Protego). Only the first 500 KiB are read, as search engines do.
* 4xx            -> "no robots.txt": everything is allowed (RFC 9309 section 2.3.1.3).
* 5xx / network  -> "unreachable": everything is DISALLOWED for this crawl (section 2.3.1.4). One
                    retry is made first, since a single failed request is common.
* Redirects      -> followed only within the site being crawled; anything else is "unreachable".

``Crawl-delay`` is honoured, capped by ``max_delay`` so a hostile value cannot stall a worker.
"""

import logging
from collections.abc import Callable
from urllib.parse import urlsplit

from protego import Protego

from app.services.crawler.fetcher import FetchError, SafeFetcher
from app.services.crawler.urls import url_host

logger = logging.getLogger(__name__)

ROBOTS_MAX_BYTES = 500 * 1024
# Failures that a second attempt cannot fix; only transient ones are retried.
PERMANENT_FAILURES = frozenset({"blocked", "off_site", "too_many_redirects"})


class RobotsPolicy:
    def __init__(
        self,
        fetcher: SafeFetcher,
        agent: str,
        host_allowed: Callable[[str], bool],
        *,
        max_delay: float = 10.0,
    ) -> None:
        self._fetcher = fetcher
        self._agent = agent
        self._host_allowed = host_allowed
        self._max_delay = max_delay
        # origin -> parsed rules, or None meaning "unreachable: disallow everything"
        self._cache: dict[str, Protego | None] = {}
        # origin -> why it was unreachable (a FetchError code, or "http_5xx")
        self._reasons: dict[str, str] = {}

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _rules(self, url: str) -> Protego | None:
        origin = self._origin(url)
        if origin not in self._cache:
            self._cache[origin] = self._load(origin)
        return self._cache[origin]

    def _load(self, origin: str) -> Protego | None:
        reason = "unavailable"
        for attempt in (1, 2):
            try:
                result = self._fetcher.fetch(
                    f"{origin}/robots.txt",
                    host_allowed=self._host_allowed,
                    max_bytes=ROBOTS_MAX_BYTES,
                    truncate=True,
                )
            except FetchError as exc:
                reason = exc.code
                if exc.code in PERMANENT_FAILURES:
                    break  # retrying a blocked or off-site robots.txt cannot succeed
                logger.info(
                    "robots.txt unavailable host=%s code=%s attempt=%d",
                    url_host(origin),
                    exc.code,
                    attempt,
                )
                continue
            if 200 <= result.status < 300:
                return Protego.parse(result.body.decode("utf-8", errors="replace"))
            if 400 <= result.status < 500:
                return Protego.parse("")  # no robots.txt: allow all
            reason = "http_5xx"
            logger.info(
                "robots.txt server error host=%s status=%d", url_host(origin), result.status
            )
        self._reasons[origin] = reason
        return None  # unreachable -> disallow all

    def allowed(self, url: str) -> bool:
        rules = self._rules(url)
        if rules is None:
            return False
        return bool(rules.can_fetch(url, self._agent))

    def unreachable(self, url: str) -> bool:
        return self._rules(url) is None

    def failure_reason(self, url: str) -> str | None:
        """Why robots.txt could not be retrieved (None if it could)."""
        return self._reasons.get(self._origin(url)) if self.unreachable(url) else None

    def crawl_delay(self, url: str) -> float:
        rules = self._rules(url)
        if rules is None:
            return 0.0
        delay = rules.crawl_delay(self._agent)
        if delay is None:
            return 0.0
        return max(0.0, min(float(delay), self._max_delay))
