import pytest

from app.services.crawler.crawler import (
    CrawlCancelled,
    CrawlError,
    CrawlLimits,
    SiteCrawler,
    content_hash,
)
from tests.crawler.sites import FakeSite, Resp, fetcher_for, html, page, redirect, text

SEED = "https://shop.example/"
LIMITS = CrawlLimits(max_pages=50, max_depth=3, delay=0, min_content_chars=10)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run(routes, *, seed=SEED, limits=LIMITS, should_stop=lambda: False, clock=None, **kwargs):
    site = FakeSite(routes)
    clock = clock or Clock()
    crawler = SiteCrawler(seed, fetcher_for(site), limits, clock=clock, sleep=clock.sleep, **kwargs)
    pages = list(crawler.crawl(should_stop))
    return pages, crawler, site


def failure(routes, *, seed=SEED, limits=LIMITS):
    with pytest.raises(CrawlError) as exc:
        run(routes, seed=seed, limits=limits)
    return exc.value.code


def urls(pages):
    return [p.url for p in pages]


def test_crawls_linked_pages_and_extracts_text():
    pages, crawler, _ = run(
        {
            "/": page("Home", "Welcome to the shop.", ("/about", "/faq")),
            "/about": page("About", "We make soap by hand."),
            "/faq": page("FAQ", "Yes we ship worldwide."),
        }
    )
    assert set(urls(pages)) == {SEED, "https://shop.example/about", "https://shop.example/faq"}
    home = next(p for p in pages if p.url == SEED)
    assert home.title == "Home" and "Welcome to the shop." in home.text and home.depth == 0
    assert crawler.stats.pages_fetched == 4 or crawler.stats.pages_fetched == 3


def test_maximum_depth_is_enforced():
    routes = {
        "/": page("L0", "level zero content", ("/l1",)),
        "/l1": page("L1", "level one content", ("/l2",)),
        "/l2": page("L2", "level two content", ("/l3",)),
        "/l3": page("L3", "level three content", ("/l4",)),
        "/l4": page("L4", "level four content"),
    }
    pages, _, site = run(routes, limits=CrawlLimits(max_depth=2, delay=0, min_content_chars=5))
    assert [p.title for p in pages] == ["L0", "L1", "L2"]
    assert "/l3" not in site.paths()  # never even requested


def test_depth_zero_crawls_only_the_start_page():
    routes = {"/": page("Home", "only home content", ("/a",)), "/a": page("A", "never fetched")}
    pages, _, site = run(routes, limits=CrawlLimits(max_depth=0, delay=0, min_content_chars=5))
    assert urls(pages) == [SEED] and "/a" not in site.paths()


def test_maximum_page_count_is_enforced_and_reported():
    links = tuple(f"/p{i}" for i in range(30))
    routes = {"/": page("Home", "home content here", links)}
    routes.update({f"/p{i}": page(f"P{i}", f"page number {i} content") for i in range(30)})
    pages, crawler, site = run(
        routes, limits=CrawlLimits(max_pages=5, delay=0, min_content_chars=5)
    )
    assert len(pages) == 5
    assert crawler.stats.stopped_by == "page_limit"
    assert len([p for p in site.paths() if p != "/robots.txt"]) == 5  # requests, not just pages


def test_failed_fetches_count_toward_the_page_limit():
    links = tuple(f"/dead{i}" for i in range(40))
    routes = {"/": page("Home", "home content here", links)}  # every other URL is a 404
    _, crawler, site = run(routes, limits=CrawlLimits(max_pages=6, delay=0, min_content_chars=5))
    assert len([p for p in site.paths() if p != "/robots.txt"]) == 6
    assert crawler.stats.failed == 5 and crawler.stats.stopped_by == "page_limit"


def test_only_the_same_site_is_crawled():
    routes = {
        "/": page(
            "Home",
            "home content here",
            (
                "/local",
                "https://other.example/page",
                "https://shop.example.evil.com/page",
                "https://sub.shop.example/page",
                "http://www.shop.example/www-page",
            ),
        ),
        "/local": page("Local", "local page content"),
        "other.example/page": page("Other", "must never be fetched"),
        "sub.shop.example/page": page("Sub", "subdomains are out of scope"),
        "shop.example.evil.com/page": page("Evil", "lookalike domain"),
        "www.shop.example/www-page": page("WWW", "the www twin is in scope"),
    }
    pages, crawler, site = run(routes)
    assert {p.title for p in pages} == {"Home", "Local", "WWW"}
    hosts = {host for host, _, _ in site.requests}
    assert hosts <= {"shop.example", "www.shop.example"}
    assert crawler.stats.skipped_off_site >= 3


def test_redirect_to_the_www_twin_is_followed_and_the_final_url_is_recorded():
    routes = {
        "shop.example/": redirect("https://www.shop.example/"),
        "www.shop.example/": page("Home", "welcome home", ("/about",)),
        "www.shop.example/about": page("About", "about us content"),
    }
    pages, _, _ = run(routes)
    assert urls(pages) == ["https://www.shop.example/", "https://www.shop.example/about"]


def test_seed_that_redirects_to_another_domain_fails_clearly():
    assert failure({"/": redirect("https://brand-new-domain.example/")}) == "redirect_off_site"


def test_internal_redirect_target_is_blocked_and_cannot_be_crawled():
    routes = {
        "/": page("Home", "home content here", ("/trap",)),
        "/trap": redirect("http://169.254.169.254/latest/meta-data/"),
    }
    pages, crawler, site = run(routes)
    assert urls(pages) == [SEED]
    assert all(host != "169.254.169.254" for host, _, _ in site.requests)
    assert crawler.stats.skipped_off_site >= 1


def test_duplicate_urls_are_fetched_once():
    routes = {
        "/": page(
            "Home",
            "home content here",
            ("/a", "/a#section", "/a?utm_source=x", "/A/../a", "https://SHOP.example/a", "/a"),
        ),
        "/a": page("A", "page a content here"),
    }
    _, _, site = run(routes)
    assert site.paths().count("/a") == 1


def test_distinct_product_variants_are_all_crawled():
    routes = {
        "/": page("Home", "home content here", ("/p?variant=1", "/p?variant=2", "/p")),
        "/p?variant=1": page("V1", "variant one description"),
        "/p?variant=2": page("V2", "variant two description"),
        "/p": page("P", "the base product description"),
    }
    pages, _, _ = run(routes)
    assert {p.title for p in pages} == {"Home", "V1", "V2", "P"}


def test_pages_with_identical_content_are_stored_once():
    same = page("Same", "exactly the same text on both pages")
    routes = {"/": page("Home", "home content here", ("/x", "/y")), "/x": same, "/y": same}
    pages, crawler, _ = run(routes)
    assert sorted(p.title for p in pages) == ["Home", "Same"]
    assert crawler.stats.skipped_duplicate == 1


def test_urls_that_redirect_to_an_already_crawled_page_are_not_duplicated():
    routes = {
        "/": page("Home", "home content here", ("/old-about", "/about")),
        "/old-about": redirect("/about"),
        "/about": page("About", "about us content"),
    }
    pages, _, site = run(routes)
    assert urls(pages).count("https://shop.example/about") == 1


def test_assets_and_non_html_are_never_stored():
    routes = {
        "/": page(
            "Home",
            "home content here",
            ("/logo.png", "/guide.pdf", "/video.mp4", "/archive.zip", "/api/data", "/ok"),
        ),
        "/api/data": Resp(200, b'{"a": 1}', {"content-type": "application/json"}),
        "/ok": page("OK", "regular page content"),
    }
    pages, crawler, site = run(routes)
    assert {p.title for p in pages} == {"Home", "OK"}
    requested = set(site.paths())
    assert not requested & {"/logo.png", "/guide.pdf", "/video.mp4", "/archive.zip"}
    assert crawler.stats.skipped_non_html == 1  # the JSON endpoint, refused by content type


def test_non_http_links_are_ignored():
    routes = {
        "/": page(
            "Home",
            "home content here",
            ("mailto:a@b.c", "tel:+123", "javascript:void(0)", "ftp://x/y", "#top", "data:x,y"),
        )
    }
    pages, _, site = run(routes)
    assert urls(pages) == [SEED] and len(site.paths()) == 1 + 1  # robots.txt + home


def test_high_value_pages_are_crawled_before_products_when_the_budget_is_tight():
    products = tuple(f"/products/item-{i}" for i in range(20))
    routes = {
        "/": page("Home", "home content here", products + ("/pages/faq", "/policies/returns"))
    }
    routes.update({p: page(p, "product page content") for p in products})
    routes["/pages/faq"] = page("FAQ", "frequently asked questions")
    routes["/policies/returns"] = page("Returns", "returns policy text")
    pages, _, _ = run(routes, limits=CrawlLimits(max_pages=4, delay=0, min_content_chars=5))
    titles = {p.title for p in pages}
    assert {"Home", "FAQ", "Returns"} <= titles  # policies were not starved by 20 products


def test_robots_disallowed_pages_are_skipped():
    routes = {
        "/robots.txt": text("User-agent: *\nDisallow: /private"),
        "/": page("Home", "home content here", ("/private/x", "/public")),
        "/private/x": page("Private", "must not be fetched"),
        "/public": page("Public", "public page content"),
    }
    pages, crawler, site = run(routes)
    assert {p.title for p in pages} == {"Home", "Public"}
    assert "/private/x" not in site.paths()
    assert crawler.stats.skipped_robots == 1


def test_seed_disallowed_by_robots_fails_the_crawl():
    routes = {"/robots.txt": text("User-agent: *\nDisallow: /"), "/": page("Home", "content here")}
    assert failure(routes) == "robots_disallowed"


def test_unreachable_robots_fails_the_crawl_conservatively():
    routes = {"/robots.txt": Resp(503), "/": page("Home", "content here")}
    assert failure(routes) == "robots_unavailable"


def test_crawl_delay_and_politeness_delay_are_applied_between_requests():
    clock = Clock()
    routes = {
        "/robots.txt": text("User-agent: *\nCrawl-delay: 2"),
        "/": page("Home", "home content here", ("/a", "/b")),
        "/a": page("A", "page a content"),
        "/b": page("B", "page b content"),
    }
    run(routes, limits=CrawlLimits(delay=0.5, min_content_chars=5), clock=clock)
    assert clock.sleeps == [2.0, 2.0]  # no sleep before the first page; max(delay, Crawl-delay)


def test_total_time_limit_stops_the_crawl_without_failing_it():
    clock = Clock()
    routes = {"/": page("Home", "home content here", ("/a", "/b", "/c"))}
    routes.update({f"/{x}": page(x, f"page {x} content") for x in "abc"})
    limits = CrawlLimits(delay=10, total_timeout=15, min_content_chars=5)
    pages, crawler, _ = run(routes, limits=limits, clock=clock)
    assert 1 <= len(pages) < 4 and crawler.stats.stopped_by == "time_limit"


def test_thin_pages_are_skipped_but_their_links_are_still_followed():
    routes = {
        "/": html("<html><body><main><a href='/real'>go</a></main></body></html>"),
        "/real": page("Real", "this is a real page with content"),
    }
    pages, crawler, _ = run(routes, limits=CrawlLimits(delay=0, min_content_chars=30))
    assert [p.title for p in pages] == ["Real"] and crawler.stats.skipped_thin == 1


def test_a_failing_page_does_not_stop_the_crawl_and_transient_failures_are_remembered():
    routes = {
        "/": page("Home", "home content here", ("/boom", "/gone", "/ok")),
        "/boom": Resp(503),
        "/gone": Resp(404),
        "/ok": page("OK", "healthy page content"),
    }
    pages, crawler, _ = run(routes)
    assert {p.title for p in pages} == {"Home", "OK"}
    assert crawler.stats.failed == 2
    assert crawler.stats.transient_failures == {"https://shop.example/boom"}  # 404 is not transient


def test_extraction_crash_on_one_page_is_contained(monkeypatch):
    from app.services.crawler import crawler as crawler_module

    real = crawler_module.extract_page

    def flaky(body, **kwargs):
        if b"POISON" in body:
            raise RuntimeError("parser exploded")
        return real(body, **kwargs)

    monkeypatch.setattr(crawler_module, "extract_page", flaky)
    routes = {
        "/": page("Home", "home content here", ("/bad", "/good")),
        "/bad": page("Bad", "POISON page"),
        "/good": page("Good", "good page content"),
    }
    pages, crawler, _ = run(routes)
    assert {p.title for p in pages} == {"Home", "Good"} and crawler.stats.failed == 1


@pytest.mark.parametrize(
    ("route", "code"),
    [
        (Resp(404), "http_error"),
        (Resp(500), "http_error"),
        (Resp(403), "http_error"),
        (Resp(200, b"{}", {"content-type": "application/json"}), "not_html"),
        (redirect("/"), "too_many_redirects"),
    ],
)
def test_seed_failures_fail_the_crawl_with_a_specific_code(route, code):
    assert failure({"/": route}) == code


def test_seed_that_is_a_blocked_address_fails_before_any_request():
    site = FakeSite({"/": page("Home", "x")})
    crawler = SiteCrawler("http://169.254.169.254/", fetcher_for(site), LIMITS)
    with pytest.raises(CrawlError) as exc:
        list(crawler.crawl())
    assert exc.value.code == "blocked_url" and site.requests == []


def test_dns_failure_on_the_seed_is_reported():
    from app.services.crawler.ssrf import DnsError

    def nxdomain(host, port):
        raise DnsError("nxdomain")

    site = FakeSite({})
    crawler = SiteCrawler(SEED, fetcher_for(site, resolver=nxdomain), LIMITS)
    with pytest.raises(CrawlError) as exc:
        list(crawler.crawl())
    assert exc.value.code == "dns_error"


def test_site_with_no_readable_content_fails_with_no_content():
    routes = {"/": html("<html><body><div id='app'></div><script>render()</script></body></html>")}
    assert failure(routes) == "no_content"


def test_cancellation_stops_the_crawl_between_pages():
    routes = {
        "/": page("Home", "home content here", ("/a", "/b")),
        "/a": page("A", "page a content"),
        "/b": page("B", "page b content"),
    }
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 1  # let the first page through, then ask to stop

    site = FakeSite(routes)
    crawler = SiteCrawler(SEED, fetcher_for(site), LIMITS, sleep=lambda s: None)
    seen = []
    with pytest.raises(CrawlCancelled):
        for crawled in crawler.crawl(stop):
            seen.append(crawled.title)
    assert seen == ["Home"]


def test_content_hash_is_stable_and_content_addressed():
    assert content_hash("abc") == content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 64


def test_every_request_carries_the_user_agent_and_no_credentials():
    seen = []

    def spy(request):
        seen.append(
            (
                request.headers["user-agent"],
                request.headers.get("cookie"),
                request.headers.get("authorization"),
            )
        )
        return page("Home", "home content here")

    run({"/": spy})
    assert seen and all(entry == ("TestBot/1.0", None, None) for entry in seen)
