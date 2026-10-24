import pytest

from app.services.crawler.fetcher import FetchLimits
from app.services.crawler.robots import RobotsPolicy
from tests.crawler.sites import FakeSite, Resp, fetcher_for, html, redirect, text

SITE_HOSTS = {"shop.example", "www.shop.example"}


def policy(routes, *, agent="BizFlowBot", max_delay=10.0):
    site = FakeSite(routes)
    fetcher = fetcher_for(site, limits=FetchLimits())
    return RobotsPolicy(fetcher, agent, SITE_HOSTS.__contains__, max_delay=max_delay), site


def test_disallowed_paths_are_refused_and_others_allowed():
    robots, _ = policy({"/robots.txt": text("User-agent: *\nDisallow: /private/\nDisallow: /cart")})
    assert robots.allowed("https://shop.example/products/a")
    assert not robots.allowed("https://shop.example/private/data")
    assert not robots.allowed("https://shop.example/cart")
    assert not robots.allowed("https://shop.example/cart/checkout")


def test_our_own_user_agent_group_beats_the_wildcard_group():
    rules = "User-agent: *\nDisallow: /\n\nUser-agent: BizFlowBot\nAllow: /\nDisallow: /admin"
    robots, _ = policy({"/robots.txt": text(rules)})
    assert robots.allowed("https://shop.example/anything")
    assert not robots.allowed("https://shop.example/admin")


def test_blocked_for_everyone_when_the_wildcard_disallows_all():
    robots, _ = policy({"/robots.txt": text("User-agent: *\nDisallow: /")})
    assert not robots.allowed("https://shop.example/")


def test_longest_match_wins_and_wildcards_are_supported():
    rules = "User-agent: *\nDisallow: /shop/\nAllow: /shop/public/\nDisallow: /*.json$\nDisallow: /*?sort="
    robots, _ = policy({"/robots.txt": text(rules)})
    assert not robots.allowed("https://shop.example/shop/a")
    assert robots.allowed("https://shop.example/shop/public/a")
    assert not robots.allowed("https://shop.example/data.json")
    assert robots.allowed("https://shop.example/data.jsonl")
    assert not robots.allowed("https://shop.example/c?sort=price")


@pytest.mark.parametrize("status", [404, 410, 401, 403])
def test_client_errors_mean_no_restrictions(status):
    robots, _ = policy({"/robots.txt": Resp(status, b"nope", {"content-type": "text/plain"})})
    assert robots.allowed("https://shop.example/anything")
    assert not robots.unreachable("https://shop.example/")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_mean_disallow_everything(status):
    robots, site = policy({"/robots.txt": Resp(status, b"oops", {"content-type": "text/plain"})})
    assert not robots.allowed("https://shop.example/")
    assert robots.unreachable("https://shop.example/")
    assert site.paths().count("/robots.txt") == 2  # one retry before giving up


def test_a_single_transient_failure_is_retried():
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            return Resp(503, b"", {})
        return text("User-agent: *\nDisallow: /private")

    robots, _ = policy({"/robots.txt": flaky})
    assert robots.allowed("https://shop.example/public")
    assert not robots.allowed("https://shop.example/private")


def test_unreachable_robots_means_disallow_everything():
    def boom(request):
        raise RuntimeError("never reached: the resolver fails first")

    site = FakeSite({"/robots.txt": boom})
    fetcher = fetcher_for(site, resolver=lambda host, port: [])
    robots = RobotsPolicy(fetcher, "BizFlowBot", SITE_HOSTS.__contains__)
    assert not robots.allowed("https://shop.example/")


def test_robots_redirect_within_the_site_is_followed():
    robots, _ = policy(
        {
            "shop.example/robots.txt": redirect("https://www.shop.example/robots.txt"),
            "www.shop.example/robots.txt": text("User-agent: *\nDisallow: /nope"),
        }
    )
    assert not robots.allowed("https://shop.example/nope")
    assert robots.allowed("https://shop.example/yes")


def test_robots_redirect_off_site_is_treated_as_unreachable():
    robots, _ = policy({"/robots.txt": redirect("https://evil.example/robots.txt")})
    assert not robots.allowed("https://shop.example/")


def test_robots_that_redirects_to_an_internal_address_is_never_followed():
    robots, site = policy({"/robots.txt": redirect("http://169.254.169.254/latest/meta-data/")})
    assert not robots.allowed("https://shop.example/")
    assert all(host != "169.254.169.254" for host, _, _ in site.requests)


def test_oversized_robots_is_truncated_not_fatal():
    big = "User-agent: *\nDisallow: /secret\n" + ("# padding\n" * 200_000)
    robots, _ = policy({"/robots.txt": text(big)})
    assert not robots.allowed("https://shop.example/secret")
    assert robots.allowed("https://shop.example/open")


def test_html_soft_404_robots_does_not_block_anything():
    robots, _ = policy({"/robots.txt": html("<html><body>Page not found</body></html>")})
    assert robots.allowed("https://shop.example/anything")


def test_empty_robots_allows_everything():
    robots, _ = policy({"/robots.txt": text("")})
    assert robots.allowed("https://shop.example/anything")


def test_crawl_delay_is_honoured_and_capped():
    robots, _ = policy({"/robots.txt": text("User-agent: *\nCrawl-delay: 3")})
    assert robots.crawl_delay("https://shop.example/") == 3.0
    robots, _ = policy({"/robots.txt": text("User-agent: *\nCrawl-delay: 999")}, max_delay=10)
    assert robots.crawl_delay("https://shop.example/") == 10.0
    robots, _ = policy({"/robots.txt": text("User-agent: *\nDisallow: /x")})
    assert robots.crawl_delay("https://shop.example/") == 0.0


def test_robots_is_fetched_once_per_origin():
    robots, site = policy(
        {
            "shop.example/robots.txt": text("User-agent: *\nDisallow: /a"),
            "www.shop.example/robots.txt": text("User-agent: *\nDisallow: /b"),
        }
    )
    for _ in range(5):
        robots.allowed("https://shop.example/x")
        robots.allowed("https://shop.example/y")
    robots.allowed("https://www.shop.example/x")
    assert site.paths().count("/robots.txt") == 2  # one per origin, however many URLs are checked
    assert not robots.allowed("https://www.shop.example/b")
    assert robots.allowed("https://shop.example/b")  # a different origin has different rules


def test_robots_requests_use_the_safe_fetcher_and_the_same_user_agent():
    seen = {}

    def spy(request):
        seen["ua"] = request.headers["user-agent"]
        return text("")

    robots, _ = policy({"/robots.txt": spy})
    robots.allowed("https://shop.example/")
    assert seen["ua"] == "TestBot/1.0"


def test_permanent_robots_failures_are_not_retried():
    robots, site = policy({"/robots.txt": redirect("https://evil.example/robots.txt")})
    assert not robots.allowed("https://shop.example/")
    assert site.paths().count("/robots.txt") == 1  # off-site redirect cannot succeed on retry
