import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Annotated

import redis
from fastapi import Depends, HTTPException, Request

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """In-process limiter. Per-worker state; the Redis backend below shares it across workers."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RedisFixedWindowLimiter:
    """Shared limiter: INCR a per-window counter and let Redis expire it. Fixed windows allow a
    short burst at a window boundary, which is acceptable for abuse protection."""

    def __init__(self, url: str, prefix: str = "bizflow:ratelimit:") -> None:
        self._prefix = prefix
        self._client: redis.Redis = redis.Redis.from_url(
            url, socket_connect_timeout=1.0, socket_timeout=1.0
        )

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        redis_key = f"{self._prefix}{key}"
        pipe = self._client.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds, nx=True)
        count = int(pipe.execute()[0])  # type: ignore[arg-type]
        return count <= limit

    def reset(self) -> None:
        for key in self._client.scan_iter(f"{self._prefix}*"):
            self._client.delete(key)


class RateLimiter:
    """The one limiter the app uses. Chooses the backend from settings and never lets a Redis
    outage take the endpoints down: it falls back to the in-process limiter."""

    def __init__(self) -> None:
        self.memory = SlidingWindowLimiter()
        self._redis: RedisFixedWindowLimiter | None = None
        self._redis_url: str | None = None

    def _shared(self, settings: Settings) -> RedisFixedWindowLimiter | None:
        if settings.rate_limit_backend != "redis":
            return None
        if self._redis is None or self._redis_url != settings.redis_url:
            self._redis = RedisFixedWindowLimiter(settings.redis_url)
            self._redis_url = settings.redis_url
        return self._redis

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        shared = self._shared(get_settings())
        if shared is not None:
            try:
                return shared.allow(key, limit, window_seconds)
            except redis.RedisError:
                logger.warning("rate limiter redis unavailable, using in-process limits")
        return self.memory.allow(key, limit, window_seconds)

    def reset(self) -> None:
        self.memory.reset()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _auth_limit(settings: Settings) -> int:
    return settings.auth_rate_limit_attempts


def _auth_window(settings: Settings) -> int:
    return settings.auth_rate_limit_window_seconds


def rate_limit(
    scope: str,
    *,
    limit: Callable[[Settings], int] = _auth_limit,
    window: Callable[[Settings], int] = _auth_window,
    key: Callable[[Request], str] = client_ip,
    detail: str = "Too many attempts. Please try again later.",
) -> Callable[..., None]:
    """Dependency factory. Defaults to the auth limits keyed by client IP (login/register);
    other callers pass their own limit, window and key. Behind a proxy, configure it to set the
    real client IP (uvicorn ``--proxy-headers`` with ``FORWARDED_ALLOW_IPS``)."""

    def dependency(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
        seconds = window(settings)
        if not limiter.allow(f"{scope}:{key(request)}", limit(settings), seconds):
            raise HTTPException(
                status_code=429, detail=detail, headers={"Retry-After": str(seconds)}
            )

    return dependency
