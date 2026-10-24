import pytest

from app.services.crawler.urls import (
    UrlError,
    canonicalize_url,
    is_probably_asset,
    link_priority,
    site_hosts,
)
from app.services.crawler.validation import validate_seed_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/a/b/..", "https://example.com/a/"),
        ("https://example.com/../x", "https://example.com/x"),
        ("https://example.com/%7Euser", "https://example.com/~user"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),  # reserved: keep, upcase
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/café", "https://example.com/caf%C3%A9"),
        ("https://example.com/100%", "https://example.com/100%25"),
        ("https://Bücher.de/", "https://xn--bcher-kva.de/"),
        ("https://example.com./", "https://example.com/"),
    ],
)
def test_canonical_forms(raw, expected):
    assert canonicalize_url(raw) == expected


def test_tracking_parameters_are_removed_but_everything_else_survives_in_order():
    url = "https://shop.example/p?utm_source=a&variant=42&fbclid=x&b=2&a=1&gclid=y&UTM_Medium=m"
    assert canonicalize_url(url) == "https://shop.example/p?variant=42&b=2&a=1"
    assert canonicalize_url("https://shop.example/p?utm_source=a") == "https://shop.example/p"


def test_distinct_product_urls_are_never_merged():
    urls = {
        canonicalize_url(u)
        for u in (
            "https://shop.example/products/shirt?variant=1",
            "https://shop.example/products/shirt?variant=2",
            "https://shop.example/products/shirt",
            "https://shop.example/products/shirt/",
            "https://shop.example/products/Shirt",
            "https://shop.example/products/shirt?a=1&b=2",
            "https://shop.example/products/shirt?b=2&a=1",
        )
    }
    assert len(urls) == 7  # query order and trailing slash are deliberately not normalized


def test_relative_urls_resolve_against_base():
    base = "https://example.com/shop/item"
    assert canonicalize_url("../faq?ref=1#x", base=base) == "https://example.com/faq?ref=1"
    assert canonicalize_url("/about", base=base) == "https://example.com/about"
    assert canonicalize_url("//cdn.example.com/x", base=base) == "https://cdn.example.com/x"
    assert canonicalize_url("details", base=base) == "https://example.com/shop/details"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
        "gopher://example.com",
        "http://user:pw@example.com/",
        "http://@example.com/",
        "https://example.com\\@evil.com",
        "https://example.com/\x00",
        "https://example.com/a\nb",
        "http://",
        "http://example.com:0/",
        "http://example.com:99999/",
        "http://example.com:abc/",
        "mailto:a@example.com",
        "http://localhost/",
        "http://LOCALHOST:8080/",
        "http://foo.localhost/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "http://intranet/",
        "http://2130706433/",  # decimal form of 127.0.0.1
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://-bad-.example.com/",
        "https://exa mple.com/",
        "https://" + "a" * 64 + ".com/",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_rejected_urls(raw):
    with pytest.raises(UrlError):
        canonicalize_url(raw)


def test_error_messages_are_user_safe():
    with pytest.raises(UrlError) as exc:
        canonicalize_url("http://localhost/")
    assert "not a public website" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/",
        "http://127.0.0.1/",
        "http://127.0.0.1:80/",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://100.100.100.200/",
        "http://[::1]/",
        "http://[fd00:ec2::254]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "https://example.com:8080/",
        "https://example.com:22/",
        "http://example.com:6379/",
    ],
)
def test_seed_validation_rejects_internal_literals_and_odd_ports(url):
    with pytest.raises(UrlError):
        validate_seed_url(url, {80, 443})


def test_seed_validation_accepts_public_sites():
    assert validate_seed_url("https://Example.com/shop?utm_x=1", {80, 443}) == (
        "https://example.com/shop"
    )
    assert validate_seed_url("http://93.184.216.34/", {80, 443}) == "http://93.184.216.34/"
    assert validate_seed_url("https://example.com:8443/", {80, 443, 8443})


def test_scheme_less_input_is_rejected_not_guessed():
    with pytest.raises(UrlError):
        validate_seed_url("example.com", {80, 443})


@pytest.mark.parametrize(
    ("url", "asset"),
    [
        ("https://x.com/logo.png", True),
        ("https://x.com/a/b/photo.JPG", True),
        ("https://x.com/video.mp4", True),
        ("https://x.com/file.zip", True),
        ("https://x.com/app.exe", True),
        ("https://x.com/style.css", True),
        ("https://x.com/guide.pdf", True),
        ("https://x.com/products/shirt", False),
        ("https://x.com/pages/faq.html", False),
        ("https://x.com/v1.2/page", False),
        ("https://x.com/", False),
    ],
)
def test_asset_detection(url, asset):
    assert is_probably_asset(url) is asset


def test_same_site_hosts_cover_www_twin_only():
    assert site_hosts("example.com") == {"example.com", "www.example.com"}
    assert site_hosts("www.example.com") == {"www.example.com", "example.com"}
    assert "shop.example.com" not in site_hosts("example.com")
    assert "example.com.evil.com" not in site_hosts("example.com")


def test_link_priority_orders_policies_before_products_before_the_rest():
    faq = link_priority("https://x.com/pages/faq")
    shipping = link_priority("https://x.com/policies/shipping-policy")
    product = link_priority("https://x.com/products/shirt")
    other = link_priority("https://x.com/lookbook")
    blog = link_priority("https://x.com/blog/post")
    filtered = link_priority("https://x.com/products/shirt?sort=price")
    assert faq == shipping < product < other < blog
    assert filtered > product
