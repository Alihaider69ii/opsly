import logging

import httpx
import pytest

from app.services.integrations.shopify import queries
from app.services.integrations.shopify.client import (
    MAX_RESPONSE_BYTES,
    ShopifyClient,
    StaticToken,
    ThrottleGovernor,
)
from app.services.integrations.shopify.errors import (
    InvalidShopDomain,
    OAuthError,
    ShopifyAccessDenied,
    ShopifyAuthError,
    ShopifyRequestError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from tests.shopify.fake_shopify import FakeShopify, make_product

CLIENT_ID, CLIENT_SECRET = "test-client-id", "test-client-secret"


def client_for(fake: FakeShopify, clock, *, token: str | None = None, **kwargs) -> ShopifyClient:
    if token is None:
        token = fake._issue()["access_token"]
    return ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken(token),
        transport=fake.transport(),
        sleep=clock.sleep,
        clock=clock.monotonic,
        jitter=lambda: 0.0,
        **kwargs,
    )


def shop_info(client):
    return client.graphql(queries.SHOP_INFO, operation="ShopInfo", cost_hint=20)


# --- addressing and SSRF properties --------------------------------------------------------------------


def test_requests_go_to_the_validated_shop_over_https_with_the_token_in_a_header(fake, clock):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), headers=dict(request.headers), method=request.method)
        return fake.handler(request)

    c = ShopifyClient(
        shop_domain="  Test-Shop.MyShopify.com ",
        api_version="2026-10",
        tokens=StaticToken(fake._issue()["access_token"]),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
    )
    shop_info(c)
    assert seen["url"] == "https://test-shop.myshopify.com/admin/api/2026-10/graphql.json"
    assert seen["headers"]["x-shopify-access-token"].startswith("shpat_")
    assert "authorization" not in seen["headers"]
    assert c.shop_domain == "test-shop.myshopify.com"


@pytest.mark.parametrize(
    "bad", ["evil.com", "localhost", "127.0.0.1", "169.254.169.254", "a.myshopify.com.evil.com"]
)
def test_the_client_refuses_to_even_be_built_for_a_non_shopify_host(fake, bad):
    with pytest.raises(InvalidShopDomain):
        ShopifyClient(shop_domain=bad, api_version="2026-10", tokens=StaticToken("x"))


def test_redirects_are_never_followed(fake, clock):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken("t"),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
    )
    with pytest.raises(ShopifyRequestError):
        shop_info(c)
    assert calls == ["https://test-shop.myshopify.com/admin/api/2026-10/graphql.json"]


def test_environment_proxy_settings_are_ignored(fake, clock, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.example:3128")
    c = client_for(fake, clock)
    assert c._client._trust_env is False  # noqa: SLF001
    assert shop_info(c).data["shop"]["name"] == "Test Shop"


def test_the_dev_override_still_requires_a_valid_myshopify_domain(fake, clock):
    with pytest.raises(InvalidShopDomain):
        ShopifyClient(
            shop_domain="evil.com",
            api_version="2026-10",
            dev_base_url="http://localhost:9300",
            tokens=StaticToken("x"),
        )


def test_oversized_responses_are_rejected_without_reading_them_into_memory(fake, clock):
    def huge(request):
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 10))

    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken("t"),
        transport=httpx.MockTransport(huge),
        sleep=clock.sleep,
    )
    with pytest.raises(ShopifyRequestError) as exc:
        shop_info(c)
    assert exc.value.code == "response_too_large"


def test_non_json_and_non_object_responses_are_treated_as_an_unavailable_shopify(fake, clock):
    for content in (b"<html>maintenance</html>", b"[1,2,3]", b""):
        c = ShopifyClient(
            shop_domain=fake.shop,
            api_version="2026-10",
            tokens=StaticToken("t"),
            transport=httpx.MockTransport(
                lambda r, body=content: httpx.Response(200, content=body)
            ),
            sleep=clock.sleep,
            max_retries=1,
        )
        with pytest.raises(ShopifyUnavailable):
            shop_info(c)


# --- rate limiting: THROTTLED, HTTP 429, pacing ------------------------------------------------------


def test_throttled_responses_are_retried_with_exponential_backoff_then_succeed(fake, clock):
    fake.throttle_responses = 3
    c = client_for(fake, clock, max_retries=5)
    assert shop_info(c).data["shop"]["name"] == "Test Shop"
    assert clock.sleeps == [1.0, 2.0, 4.0]  # doubles each time, no jitter in this test
    assert len(fake.requests) == 4


def test_jitter_is_added_to_backoff(fake, clock):
    fake.throttle_responses = 1
    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken(fake._issue()["access_token"]),
        transport=fake.transport(),
        sleep=clock.sleep,
        jitter=lambda: 1.0,
    )
    shop_info(c)
    assert clock.sleeps == [1.25]  # base 1.0 + 25% jitter


def test_throttling_is_bounded_and_then_reported(fake, clock):
    fake.throttle_responses = 100
    c = client_for(fake, clock, max_retries=3)
    with pytest.raises(ShopifyThrottled):
        shop_info(c)
    assert len(fake.requests) == 4  # the first try plus exactly 3 retries, never an endless loop
    assert len(clock.sleeps) == 3


def test_http_429_honours_retry_after(fake, clock):
    fake.http_failures = [429, 429]
    fake.retry_after = "7"
    c = client_for(fake, clock, max_retries=5)
    shop_info(c)
    assert clock.sleeps == [7.0, 7.0]


def test_a_hostile_retry_after_cannot_stall_the_worker(fake, clock):
    fake.http_failures = [429]
    fake.retry_after = "86400"
    shop_info(client_for(fake, clock))
    assert clock.sleeps == [60.0]  # capped


def test_persistent_429_is_reported_as_throttled(fake, clock):
    fake.http_failures = [429] * 10
    with pytest.raises(ShopifyThrottled):
        shop_info(client_for(fake, clock, max_retries=2))
    assert len(fake.requests) == 3


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried_then_reported_as_unavailable(fake, clock, status):
    fake.http_failures = [status] * 10
    with pytest.raises(ShopifyUnavailable):
        shop_info(client_for(fake, clock, max_retries=2))
    assert len(fake.requests) == 3
    fake.http_failures = [status]
    assert shop_info(client_for(fake, clock)).data  # a single blip is absorbed


def test_network_failures_are_retried_then_reported(fake, clock):
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("down", request=request)
        return fake.handler(request)

    token = fake._issue()["access_token"]
    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken(token),
        transport=httpx.MockTransport(flaky),
        sleep=clock.sleep,
        jitter=lambda: 0.0,
    )
    assert shop_info(c).data
    assert len(attempts) == 3

    def dead(request):
        raise httpx.ReadTimeout("slow", request=request)

    c2 = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken(token),
        transport=httpx.MockTransport(dead),
        sleep=clock.sleep,
        max_retries=2,
    )
    with pytest.raises(ShopifyUnavailable):
        shop_info(c2)


def test_the_client_waits_for_the_bucket_to_refill_before_an_expensive_query(fake, clock):
    c = client_for(fake, clock)
    fake.bucket_available = 40.0  # nearly empty; restore rate 50 points/second
    shop_info(c)  # the response teaches the governor the bucket level
    clock.sleeps.clear()
    c.graphql(
        queries.PRODUCTS_PAGE,
        {"first": 20, "after": None, "variants": 25},
        operation="Products",
        cost_hint=600,
    )
    # needs 600 + 50 floor; has 40; refills 50/s -> (650 - 40) / 50 = 12.2 seconds
    assert clock.sleeps and clock.sleeps[0] == pytest.approx(12.2, abs=0.01)


def test_a_full_bucket_means_no_waiting(fake, clock):
    c = client_for(fake, clock)
    fake.bucket_available = 1000.0
    for _ in range(3):
        shop_info(c)
    assert clock.sleeps == []


def test_the_bucket_refills_with_time_so_waiting_is_only_as_long_as_needed(fake, clock):
    g = ThrottleGovernor(clock=clock.monotonic)
    g.update(
        {"throttleStatus": {"maximumAvailable": 1000, "currentlyAvailable": 100, "restoreRate": 50}}
    )
    first = g.delay_for(300)
    clock.advance(4)  # 4 seconds restore 200 points
    assert g.delay_for(300) == pytest.approx(first - 4, abs=0.01)
    clock.advance(60)
    assert g.delay_for(300) == 0.0  # long since full


def test_the_governor_never_waits_longer_than_its_cap_and_ignores_garbage(fake, clock):
    g = ThrottleGovernor(clock=clock.monotonic, max_wait=30)
    g.update(
        {"throttleStatus": {"maximumAvailable": 1000, "currentlyAvailable": 0, "restoreRate": 1}}
    )
    assert g.delay_for(900) == 30  # would be 950s uncapped
    g2 = ThrottleGovernor(clock=clock.monotonic)
    for junk in (None, {}, {"throttleStatus": "x"}, {"throttleStatus": {"maximumAvailable": "a"}}):
        g2.update(junk)
    assert g2.delay_for(500) == 0.0  # no information means no delay, and no crash


# --- permanent failures are not retried ---------------------------------------------------------------


@pytest.mark.parametrize("status", [402, 404, 423])
def test_store_level_refusals_are_permanent(fake, clock, status):
    """402 frozen (unpaid), 404 gone, 423 locked: nothing a retry can fix."""
    fake.http_failures = [status]
    c = client_for(fake, clock)
    with pytest.raises(ShopifyRequestError) as exc:
        shop_info(c)
    assert exc.value.code == "shop_unavailable" and len(fake.requests) == 1


def test_403_means_a_missing_scope_not_a_broken_store_and_is_never_retried(fake, clock):
    """Shopify: 403 is "returned if you haven't requested the appropriate scope for this action"
    (https://shopify.dev/docs/api/usage/response-codes); from 2027 also for non-expiring tokens."""
    fake.http_failures = [403, 403, 403]
    c = client_for(fake, clock)
    with pytest.raises(ShopifyAccessDenied) as exc:
        shop_info(c)
    assert exc.value.code == "access_denied"
    assert len(fake.requests) == 1 and clock.sleeps == []


# --- the Admin API version Shopify actually served ---------------------------------------------------------


def test_a_fallen_forward_api_version_is_reported_once_not_silently_accepted(
    fake, clock, caplog, monkeypatch
):
    """Shopify answers a retired version with the oldest one it still serves and says so in
    X-Shopify-API-Version. The client must notice (and not spam the log)."""
    from app.services.integrations.shopify import client as client_module

    monkeypatch.setattr(client_module, "_REPORTED_VERSION_FALLBACKS", set())

    def served_older(request):
        response = fake.handler(request)
        response.headers["X-Shopify-API-Version"] = "2025-10"
        return response

    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken(fake._issue()["access_token"]),
        transport=httpx.MockTransport(served_older),
        sleep=clock.sleep,
    )
    caplog.set_level(logging.WARNING)
    shop_info(c)
    shop_info(c)
    warnings = [r.getMessage() for r in caplog.records if "api version" in r.getMessage()]
    assert len(warnings) == 1
    assert "configured=2026-10" in warnings[0] and "served=2025-10" in warnings[0]
    assert c.served_version == "2025-10"


def test_a_matching_or_absent_version_header_is_silent(fake, clock, caplog):
    caplog.set_level(logging.WARNING)
    c = client_for(fake, clock)
    shop_info(c)  # the fake sends no header at all
    assert c.served_version is None
    assert "api version" not in caplog.text


def test_bad_requests_are_not_retried(fake, clock):
    fake.http_failures = [400]
    with pytest.raises(ShopifyRequestError):
        shop_info(client_for(fake, clock))
    assert len(fake.requests) == 1 and clock.sleeps == []


def test_invalid_graphql_is_a_permanent_error_naming_only_the_code(fake, clock):
    def bad(request):
        return httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "message": "Query cost is 5000, which exceeds the single query max cost limit",
                        "extensions": {"code": "MAX_COST_EXCEEDED", "cost": 5000},
                    }
                ]
            },
        )

    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=StaticToken("t"),
        transport=httpx.MockTransport(bad),
        sleep=clock.sleep,
    )
    with pytest.raises(ShopifyRequestError) as exc:
        shop_info(c)
    assert "MAX_COST_EXCEEDED" in str(exc.value) and "5000" not in str(exc.value)
    assert clock.sleeps == []


# --- authorization ------------------------------------------------------------------------------------------


def test_a_rejected_token_is_refreshed_once_and_the_call_retried(fake, clock):
    refreshed = []

    class Tokens:
        def __init__(self):
            self.current = "stale-token"

        def access_token(self):
            return self.current

        def force_refresh(self):
            refreshed.append(1)
            self.current = fake._issue()["access_token"]
            return self.current

    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=Tokens(),
        transport=fake.transport(),
        sleep=clock.sleep,
    )
    assert shop_info(c).data["shop"]["name"] == "Test Shop"
    assert len(refreshed) == 1 and fake.requests[0]["token"] == "stale-token"
    assert fake.requests[1]["token"].startswith("shpat_")


def test_a_token_that_is_still_rejected_after_refresh_is_an_auth_error_not_a_loop(fake, clock):
    class AlwaysBad:
        def access_token(self):
            return "bad"

        def force_refresh(self):
            return "also-bad"

    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        tokens=AlwaysBad(),
        transport=fake.transport(),
        sleep=clock.sleep,
    )
    with pytest.raises(ShopifyAuthError):
        shop_info(c)
    assert len(fake.requests) == 2  # one try, one retry after the refresh, then stop


def test_a_static_token_cannot_be_refreshed(fake, clock):
    c = client_for(fake, clock, token="not-valid")
    with pytest.raises(ShopifyAuthError):
        shop_info(c)
    assert len(fake.requests) == 1


def test_a_client_without_tokens_cannot_call_the_api(fake):
    c = ShopifyClient(shop_domain=fake.shop, api_version="2026-10", transport=fake.transport())
    with pytest.raises(ShopifyAuthError):
        shop_info(c)
    assert fake.requests == []


# --- protected customer data ----------------------------------------------------------------------------------


def test_partial_data_with_access_denied_is_returned_and_flagged(fake, clock):
    fake.customers = [__import__("tests.shopify.fake_shopify", fromlist=["x"]).make_customer(1)]
    fake.restrict_customer_pii = True
    result = client_for(fake, clock).graphql(
        queries.CUSTOMERS_PAGE, {"first": 10, "after": None}, operation="Customers"
    )
    assert result.access_denied is True
    (node,) = result.data["customers"]["nodes"]
    assert node["defaultEmailAddress"] is None  # the data we did get is kept


def test_access_denied_without_any_data_is_an_error(fake, clock):
    fake.deny = {"orders"}
    with pytest.raises(ShopifyAccessDenied):
        client_for(fake, clock).graphql(
            queries.ORDERS_PAGE, {"first": 5, "after": None, "items": 5}, operation="Orders"
        )


# --- shop info, webhooks ---------------------------------------------------------------------------------------


def test_shop_info_is_normalized(fake, clock):
    info = client_for(fake, clock).shop_info()
    assert (info.name, info.currency, info.timezone) == ("Test Shop", "USD", "America/New_York")
    assert info.myshopify_domain == "test-shop.myshopify.com"


def test_webhook_subscription_helpers(fake, clock):
    c = client_for(fake, clock)
    assert c.list_webhook_subscriptions() == []
    assert c.create_webhook_subscription("PRODUCTS_UPDATE", "https://api.example.com/x") is True
    fake.refuse_webhook_topics = {"ORDERS_CREATE"}
    assert c.create_webhook_subscription("ORDERS_CREATE", "https://api.example.com/x") is False
    (sub,) = c.list_webhook_subscriptions()
    assert sub["topic"] == "PRODUCTS_UPDATE"
    assert c.delete_webhook_subscription(sub["id"]) is True
    assert c.list_webhook_subscriptions() == []


# --- the OAuth token endpoint ------------------------------------------------------------------------------------


def test_code_exchange_sends_the_documented_form_and_returns_an_expiring_grant(fake, clock):
    c = ShopifyClient(
        shop_domain=fake.shop, api_version="2026-10", transport=fake.transport(), sleep=clock.sleep
    )
    grant = c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")
    assert fake.token_requests == [
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": "good-code",
            "expiring": "1",
        }
    ]
    assert grant.access_token.startswith("shpat_") and grant.refresh_token.startswith("shprt_")
    assert grant.expires_in == 3600 and grant.refresh_token_expires_in == 7_776_000
    assert "read_products" in grant.scope


def test_a_bad_code_is_an_oauth_error_and_is_never_retried(fake, clock):
    c = ShopifyClient(
        shop_domain=fake.shop, api_version="2026-10", transport=fake.transport(), sleep=clock.sleep
    )
    with pytest.raises(OAuthError) as exc:
        c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="bad")
    assert exc.value.code == "exchange_failed"
    with pytest.raises(OAuthError):
        c.exchange_code(client_id=CLIENT_ID, client_secret="wrong", code="good-code")
    assert len(fake.token_requests) == 2 and clock.sleeps == []


def test_a_code_cannot_be_exchanged_twice(fake, clock):
    c = ShopifyClient(
        shop_domain=fake.shop, api_version="2026-10", transport=fake.transport(), sleep=clock.sleep
    )
    c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")
    with pytest.raises(OAuthError):
        c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")


def test_refresh_rotates_both_tokens(fake, clock):
    c = ShopifyClient(
        shop_domain=fake.shop, api_version="2026-10", transport=fake.transport(), sleep=clock.sleep
    )
    first = c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")
    second = c.refresh_access_token(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=first.refresh_token
    )
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"
    assert second.access_token != first.access_token and second.refresh_token != first.refresh_token
    # Shopify keeps the presented refresh token usable (grace period) until the newer one is used...
    again = c.refresh_access_token(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=first.refresh_token
    )
    assert again.refresh_token not in (first.refresh_token, second.refresh_token)
    # ...and once a newer refresh token has been used, the older one is retired for good.
    c.refresh_access_token(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=again.refresh_token
    )
    with pytest.raises(ShopifyAuthError) as exc:
        c.refresh_access_token(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=first.refresh_token
        )
    assert exc.value.code == "reauthorization_required"


def test_refresh_retries_transient_failures_but_not_rejections(fake, clock):
    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        transport=fake.transport(),
        sleep=clock.sleep,
        jitter=lambda: 0.0,
    )
    grant = c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")
    fake.token_endpoint_status = 503
    with pytest.raises(ShopifyUnavailable):
        c.refresh_access_token(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=grant.refresh_token
        )
    assert len(fake.token_requests) == 1 + 3  # the exchange, then the first try plus two retries
    fake.token_endpoint_status = None
    before = len(fake.token_requests)
    with pytest.raises(ShopifyAuthError):
        c.refresh_access_token(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token="never-issued"
        )
    assert len(fake.token_requests) == before + 1  # a rejection is final


def test_a_response_without_an_access_token_is_rejected(fake, clock):
    c = ShopifyClient(
        shop_domain=fake.shop,
        api_version="2026-10",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"scope": "x"})),
        sleep=clock.sleep,
    )
    with pytest.raises(OAuthError):
        c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="c")


def test_non_expiring_tokens_are_supported(fake, clock):
    fake.expiring = False
    c = ShopifyClient(
        shop_domain=fake.shop, api_version="2026-10", transport=fake.transport(), sleep=clock.sleep
    )
    grant = c.exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")
    assert grant.refresh_token is None and grant.expires_in is None


# --- nothing sensitive reaches logs or reprs -------------------------------------------------------------------


def test_tokens_secrets_and_bodies_never_appear_in_logs_errors_or_reprs(fake, clock, caplog):
    caplog.set_level(logging.DEBUG)
    token = fake._issue()["access_token"]
    fake.throttle_responses = 1
    fake.http_failures = [503]
    c = client_for(fake, clock, token=token)
    shop_info(c)
    grant = ShopifyClient(
        shop_domain=fake.shop, api_version="2026-10", transport=fake.transport(), sleep=clock.sleep
    ).exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="good-code")
    fake.http_failures = [401]
    with pytest.raises(ShopifyAuthError) as exc:
        client_for(fake, clock, token="shpat_leaky_value_123").graphql(
            queries.SHOP_INFO, operation="ShopInfo"
        )
    blob = caplog.text + str(exc.value) + repr(exc.value) + repr(grant) + repr(StaticToken(token))
    for secret in (
        token,
        grant.access_token,
        grant.refresh_token,
        CLIENT_SECRET,
        "shpat_leaky_value_123",
    ):
        assert secret not in blob, secret


def test_graphql_variables_for_products_stay_within_the_cost_budget():
    # 20 products x 25 variants and 20 orders x 25 items must stay under Shopify's 1000-point cap
    assert queries.PRODUCT_PAGE_SIZE * (queries.VARIANT_PAGE_SIZE + 1) + 2 <= 1000
    assert queries.ORDER_PAGE_SIZE * (queries.LINE_ITEM_PAGE_SIZE + 1) + 2 <= 1000
    assert queries.CUSTOMER_PAGE_SIZE + 2 <= 1000
    assert make_product(1)["id"].startswith("gid://shopify/Product/")
