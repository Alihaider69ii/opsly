"""The Shopify GraphQL Admin API client and OAuth token endpoint calls.

Safety properties (each covered by tests):

* **No SSRF.** The host is always ``<validated handle>.myshopify.com`` (or the dev-only override),
  the scheme is https, the paths are fixed templates, redirects are never followed, environment
  proxy settings are ignored and the response size is capped.
* **Rate limits.** Shopify's GraphQL API is a leaky bucket measured in "query cost" points. The
  client reads ``extensions.cost.throttleStatus`` from every response and waits for the bucket to
  refill before a request that would exceed it; if Shopify still throttles (``THROTTLED`` or HTTP
  429) it retries with exponential backoff and jitter, honouring ``Retry-After``, a bounded number
  of times.
* **No pointless retries.** Authentication, permission and malformed-query failures are permanent
  and raised immediately.
* **No leaks.** Tokens go only in the ``X-Shopify-Access-Token`` header; logs carry the shop,
  operation, status and attempt, never tokens, headers, query text or response bodies.

Protected customer data: when an app is not approved for it Shopify answers HTTP 200 with the field
null plus an ``ACCESS_DENIED`` error. ``GraphQLResult.access_denied`` exposes that so a caller can
keep the data it did get instead of failing the whole sync.
"""

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.services.integrations.shopify import queries
from app.services.integrations.shopify.errors import (
    OAuthError,
    ShopifyAccessDenied,
    ShopifyAuthError,
    ShopifyRequestError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.services.integrations.shopify.validation import normalize_shop_domain

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 30.0
USER_AGENT = "BizFlow/1.0 (+shopify-integration)"

# (configured, served) pairs already reported, so a fallen-forward version warns once per process.
_REPORTED_VERSION_FALLBACKS: set[tuple[str, str]] = set()


class TokenSource(Protocol):
    """Supplies the access token. The refresh logic (and its locking) lives behind this."""

    def access_token(self) -> str: ...

    def force_refresh(self) -> str:
        """Obtain a new access token after Shopify rejected the current one."""
        ...


@dataclass(frozen=True)
class StaticToken:
    """A fixed token (used once, right after authorization, before the connection is stored)."""

    token: str = field(repr=False)

    def access_token(self) -> str:
        return self.token

    def force_refresh(self) -> str:
        raise ShopifyAuthError("the token was rejected and cannot be refreshed")


@dataclass(frozen=True)
class _Raw:
    """One HTTP exchange, fully read within the size cap."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes


@dataclass(frozen=True)
class TokenGrant:
    access_token: str = field(repr=False)
    scope: str
    expires_in: int | None = None
    refresh_token: str | None = field(default=None, repr=False)
    refresh_token_expires_in: int | None = None


@dataclass(frozen=True)
class ShopInfo:
    name: str | None
    myshopify_domain: str | None
    currency: str | None
    timezone: str | None


@dataclass
class GraphQLResult:
    data: dict[str, Any]
    errors: list[dict[str, Any]]
    cost: dict[str, Any] | None = None

    @property
    def access_denied(self) -> bool:
        return any(_error_code(e) == "ACCESS_DENIED" for e in self.errors)


def _error_code(error: Mapping[str, Any]) -> str:
    extensions = error.get("extensions")
    code = extensions.get("code") if isinstance(extensions, Mapping) else None
    if isinstance(code, str):
        return code
    return "THROTTLED" if str(error.get("message", "")).strip().lower() == "throttled" else ""


class ThrottleGovernor:
    """Tracks Shopify's leaky bucket from response metadata and says how long to wait."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        floor: float = 50.0,
        max_wait: float = 30.0,
    ) -> None:
        self._clock = clock
        self._floor = floor
        self._max_wait = max_wait
        self._maximum: float | None = None
        self._available = 0.0
        self._restore_rate = 50.0
        self._stamp = 0.0
        self._last_cost = 100.0

    def update(self, cost: Mapping[str, Any] | None) -> None:
        if not cost:
            return
        status = cost.get("throttleStatus")
        if isinstance(status, Mapping):
            try:
                self._maximum = float(status["maximumAvailable"])
                self._available = float(status["currentlyAvailable"])
                self._restore_rate = max(float(status["restoreRate"]), 1.0)
                self._stamp = self._clock()
            except (KeyError, TypeError, ValueError):
                return
        actual = cost.get("actualQueryCost")
        if isinstance(actual, int | float) and actual > 0:
            self._last_cost = float(actual)

    def snapshot(self) -> dict[str, float | None]:
        """The last bucket state Shopify reported (None until a response has carried one)."""
        return {
            "maximum": self._maximum,
            "available": self._available if self._maximum is not None else None,
            "restore_rate": self._restore_rate if self._maximum is not None else None,
        }

    def delay_for(self, estimated_cost: float) -> float:
        """Seconds to wait so the bucket can pay for a query of about ``estimated_cost`` points."""
        if self._maximum is None:
            return 0.0
        elapsed = max(self._clock() - self._stamp, 0.0)
        available = min(self._maximum, self._available + elapsed * self._restore_rate)
        need = max(estimated_cost, self._last_cost) + self._floor
        if available >= need:
            return 0.0
        return min((need - available) / self._restore_rate, self._max_wait)


class ShopifyClient:
    def __init__(
        self,
        *,
        shop_domain: str,
        api_version: str,
        tokens: TokenSource | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        dev_base_url: str = "",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.shop_domain = normalize_shop_domain(shop_domain)  # re-validated at the last moment
        self._api_version = api_version
        self.served_version: str | None = None  # from the last response's X-Shopify-API-Version
        self._tokens = tokens
        self._max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self.governor = ThrottleGovernor(clock=clock)
        base = dev_base_url.rstrip("/") if dev_base_url else f"https://{self.shop_domain}"
        self._client = httpx.Client(
            base_url=base,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def authorize(self, tokens: TokenSource) -> "ShopifyClient":
        """Attach a token source (a client built for the OAuth endpoints has none)."""
        self._tokens = tokens
        return self

    def close(self) -> None:
        self._client.close()

    # -- plumbing ------------------------------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), MAX_RETRY_AFTER_SECONDS)
        delay = min(1.0 * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        return delay + delay * 0.25 * self._jitter()

    @staticmethod
    def _retry_after(raw: _Raw) -> float | None:
        try:
            return float(raw.headers.get("retry-after", ""))
        except ValueError:
            return None

    def _check_served_version(self, raw: _Raw) -> None:
        """Shopify "falls forward" a retired API version to the oldest one it still serves and says
        so in ``X-Shopify-API-Version``. That would change behaviour silently, so report it."""
        served = (raw.headers.get("x-shopify-api-version") or "").strip()
        self.served_version = served or None
        if served and served != self._api_version:
            pair = (self._api_version, served)
            if pair not in _REPORTED_VERSION_FALLBACKS:
                _REPORTED_VERSION_FALLBACKS.add(pair)
                logger.warning(
                    "shopify served a different api version configured=%s served=%s "
                    "(the configured version may be retired: upgrade SHOPIFY_API_VERSION)",
                    self._api_version,
                    served,
                )

    @staticmethod
    def _read_json(raw: _Raw) -> dict[str, Any]:
        try:
            body = json.loads(raw.content)
        except ValueError:
            raise ShopifyUnavailable("Shopify returned a non-JSON response") from None
        if not isinstance(body, dict):
            raise ShopifyUnavailable("Shopify returned an unexpected response")
        return body

    def _send(self, method: str, path: str, **kwargs: Any) -> _Raw:
        """One HTTP exchange. The body is read as a stream and abandoned the moment it exceeds the
        cap, counting *decoded* bytes so a compression bomb cannot exhaust memory."""
        with self._client.stream(method, path, **kwargs) as response:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise ShopifyRequestError("response too large", code="response_too_large")
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise ShopifyRequestError("response too large", code="response_too_large")
                chunks.append(chunk)
            return _Raw(response.status_code, dict(response.headers), b"".join(chunks))

    # -- GraphQL ---------------------------------------------------------------------------------

    def graphql(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        operation: str,
        cost_hint: float = 100.0,
    ) -> GraphQLResult:
        if self._tokens is None:
            raise ShopifyAuthError("no token source configured")
        path = f"/admin/api/{self._api_version}/graphql.json"
        payload = {"query": query, "variables": dict(variables or {})}
        refreshed = False
        for attempt in range(1, self._max_retries + 2):
            wait = self.governor.delay_for(cost_hint)
            if wait > 0:
                self._sleep(wait)
            try:
                response = self._send(
                    "POST",
                    path,
                    json=payload,
                    headers={"X-Shopify-Access-Token": self._tokens.access_token()},
                )
            except (httpx.TimeoutException, httpx.TransportError):
                self._log("transport_error", operation, attempt)
                if attempt > self._max_retries:
                    raise ShopifyUnavailable("Shopify could not be reached") from None
                self._sleep(self._backoff(attempt, None))
                continue

            self._check_served_version(response)
            status = response.status_code
            if status == 401:
                if refreshed:
                    raise ShopifyAuthError("Shopify rejected the access token")
                refreshed = True
                self._tokens.force_refresh()  # may raise ShopifyAuthError (needs reauthorization)
                continue
            if status == 429 or status >= 500:
                self._log(f"http_{status}", operation, attempt)
                if attempt > self._max_retries:
                    if status == 429:
                        raise ShopifyThrottled("Shopify kept rate limiting the request")
                    raise ShopifyUnavailable(f"Shopify returned HTTP {status}")
                self._sleep(self._backoff(attempt, self._retry_after(response)))
                continue
            if status == 403:
                # Shopify: "returned if you haven't requested the appropriate scope for this action"
                # (and, from 1 January 2027, for non-expiring offline tokens). Permanent: a retry
                # cannot grant a scope.
                raise ShopifyAccessDenied(f"Shopify refused access (HTTP 403) for {operation}")
            if status in (402, 404, 423):  # frozen (unpaid), gone, or locked
                raise ShopifyRequestError(
                    f"Shopify reports the store is unavailable (HTTP {status})",
                    code="shop_unavailable",
                )
            if status >= 300:  # includes redirects, which are never legitimate here
                raise ShopifyRequestError(f"Shopify rejected the request (HTTP {status})")

            body = self._read_json(response)
            extensions = body.get("extensions")
            cost = extensions.get("cost") if isinstance(extensions, Mapping) else None
            self.governor.update(cost if isinstance(cost, Mapping) else None)
            errors = [e for e in (body.get("errors") or []) if isinstance(e, dict)]
            raw_data = body.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}

            codes = {_error_code(e) for e in errors}
            if "THROTTLED" in codes:
                self._log("throttled", operation, attempt)
                if attempt > self._max_retries:
                    raise ShopifyThrottled("Shopify kept throttling the request")
                self._sleep(self._backoff(attempt, None))
                continue
            if "INTERNAL_SERVER_ERROR" in codes:
                if attempt > self._max_retries:
                    raise ShopifyUnavailable("Shopify reported an internal error")
                self._sleep(self._backoff(attempt, None))
                continue
            fatal = codes - {"", "ACCESS_DENIED"}
            if fatal:
                raise ShopifyRequestError(
                    f"GraphQL error ({', '.join(sorted(fatal))}) in {operation}"
                )
            if errors and not any(v is not None for v in data.values()):
                if "ACCESS_DENIED" in codes:
                    raise ShopifyAccessDenied(f"access denied for {operation}")
                raise ShopifyRequestError(f"GraphQL request failed in {operation}")
            return GraphQLResult(
                data=data, errors=errors, cost=cost if isinstance(cost, dict) else None
            )
        raise ShopifyUnavailable("retries exhausted")  # pragma: no cover (loop always exits above)

    def _log(self, event: str, operation: str, attempt: int) -> None:
        logger.warning(
            "shopify request problem shop=%s operation=%s event=%s attempt=%d",
            self.shop_domain,
            operation,
            event,
            attempt,
        )

    # -- convenience operations ----------------------------------------------------------------

    def shop_info(self) -> ShopInfo:
        shop = self.graphql(
            queries.SHOP_INFO, operation="ShopInfo", cost_hint=queries.COST_HINTS["small"]
        ).data.get("shop")
        if not isinstance(shop, dict):
            raise ShopifyRequestError("Shopify did not return shop information")

        def text(key: str) -> str | None:
            value = shop.get(key)
            return value.strip()[:255] if isinstance(value, str) and value.strip() else None

        return ShopInfo(
            text("name"), text("myshopifyDomain"), text("currencyCode"), text("ianaTimezone")
        )

    def list_webhook_subscriptions(self) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        after: str | None = None
        for _ in range(20):  # a store has a handful; the cap only bounds a misbehaving API
            result = self.graphql(
                queries.WEBHOOK_SUBSCRIPTIONS,
                {"first": 100, "after": after},
                operation="WebhookSubscriptions",
                cost_hint=queries.COST_HINTS["small"],
            )
            connection = result.data.get("webhookSubscriptions") or {}
            for node in connection.get("nodes") or []:
                if isinstance(node, dict) and node.get("id") and node.get("topic"):
                    found.append(
                        {
                            "id": str(node["id"]),
                            "topic": str(node["topic"]),
                            "uri": str(node.get("uri") or ""),
                        }
                    )
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        return found

    def create_webhook_subscription(self, topic: str, uri: str) -> bool:
        """Subscribe; returns False (not an exception) when Shopify refuses this topic or URL."""
        result = self.graphql(
            queries.WEBHOOK_SUBSCRIPTION_CREATE,
            {"topic": topic, "uri": uri},
            operation="WebhookCreate",
            cost_hint=queries.COST_HINTS["small"],
        )
        payload = result.data.get("webhookSubscriptionCreate") or {}
        return not payload.get("userErrors") and bool(payload.get("webhookSubscription"))

    def delete_webhook_subscription(self, subscription_id: str) -> bool:
        result = self.graphql(
            queries.WEBHOOK_SUBSCRIPTION_DELETE,
            {"id": subscription_id},
            operation="WebhookDelete",
            cost_hint=queries.COST_HINTS["small"],
        )
        payload = result.data.get("webhookSubscriptionDelete") or {}
        return not payload.get("userErrors")

    # -- OAuth token endpoint ---------------------------------------------------------------------

    def _token_request(self, form: dict[str, str], *, retries: int) -> TokenGrant:
        last: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                response = self._send(
                    "POST",
                    "/admin/oauth/access_token",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except (httpx.TimeoutException, httpx.TransportError):
                last = ShopifyUnavailable("could not reach Shopify's token endpoint")
            else:
                if response.status_code == 200:
                    return self._grant(response)
                if response.status_code >= 500 or response.status_code == 429:
                    last = ShopifyUnavailable(
                        f"token endpoint returned HTTP {response.status_code}"
                    )
                else:
                    raise ShopifyAuthError(
                        f"the token endpoint rejected the request (HTTP {response.status_code})"
                    )
            if attempt <= retries:
                self._sleep(self._backoff(attempt, None))
        assert last is not None
        raise last

    def _grant(self, response: _Raw) -> TokenGrant:
        body = self._read_json(response)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise ShopifyAuthError("the token endpoint returned no access token")

        def number(key: str) -> int | None:
            value = body.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        refresh = body.get("refresh_token")
        return TokenGrant(
            access_token=token,
            scope=str(body.get("scope") or ""),
            expires_in=number("expires_in"),
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            refresh_token_expires_in=number("refresh_token_expires_in"),
        )

    def exchange_code(self, *, client_id: str, client_secret: str, code: str) -> TokenGrant:
        """Authorization code -> expiring offline token. Not retried on a transient failure: the
        code is single-use, so a blind retry could fail confusingly; the merchant starts again."""
        try:
            return self._token_request(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "expiring": "1",
                },
                retries=0,
            )
        except (ShopifyAuthError, ShopifyUnavailable):
            raise OAuthError(
                "Shopify did not accept the authorization code", code="exchange_failed"
            ) from None

    def refresh_access_token(
        self, *, client_id: str, client_secret: str, refresh_token: str
    ) -> TokenGrant:
        """Rotate: returns a new access token AND a new refresh token (store both)."""
        try:
            return self._token_request(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                retries=2,  # safe to retry: Shopify repeats the same answer for about an hour
            )
        except ShopifyAuthError:
            raise ShopifyAuthError(
                "the refresh token was rejected", code="reauthorization_required"
            ) from None
