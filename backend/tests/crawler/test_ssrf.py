import ipaddress

import pytest

from app.services.crawler.fetcher import FetchError, FetchLimits, SafeFetcher
from app.services.crawler.ssrf import (
    AddressBlockedError,
    DnsError,
    is_public_ip,
    resolve_public_addresses,
)
from tests.crawler.sites import PUBLIC_IP, FakeSite, fetcher_for, html, redirect

BLOCKED = [
    # loopback
    "127.0.0.1",
    "127.255.255.254",
    "::1",
    # unspecified
    "0.0.0.0",
    "::",
    # RFC 1918 private
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.0.1",
    "192.168.255.255",
    # link-local and cloud metadata
    "169.254.169.254",
    "169.254.0.1",
    "fe80::1",
    "fe80::a00:27ff:fe4e:66a1",
    # carrier-grade NAT (includes Alibaba metadata 100.100.100.200)
    "100.64.0.1",
    "100.100.100.200",
    # Oracle Cloud metadata and IETF protocol assignments
    "192.0.0.192",
    "192.0.0.1",
    # AWS IPv6 metadata and unique-local
    "fd00:ec2::254",
    "fc00::1",
    "fdff::1",
    # documentation / benchmarking / reserved / multicast / broadcast
    "192.0.2.1",
    "198.51.100.1",
    "203.0.113.1",
    "198.18.0.1",
    "240.0.0.1",
    "255.255.255.255",
    "224.0.0.1",
    "239.255.255.250",
    "ff02::1",
    "2001:db8::1",
    "100::1",
    # IPv4 smuggled inside IPv6
    "::ffff:127.0.0.1",
    "::ffff:7f00:1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254
    "64:ff9b:1::1",  # local-use NAT64
    "2002:7f00:1::1",  # 6to4 -> 127.0.0.1
    "2002:a9fe:a9fe::1",  # 6to4 -> 169.254.169.254
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
]

PUBLIC = [
    "93.184.216.34",
    "8.8.8.8",
    "1.1.1.1",
    "172.15.255.255",  # just outside 172.16/12
    "172.32.0.1",  # just outside 172.16/12
    "100.63.255.255",  # just outside 100.64/10
    "100.128.0.1",
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    "::ffff:8.8.8.8",  # IPv4-mapped public
    "64:ff9b::808:808",  # NAT64 -> 8.8.8.8
]


@pytest.mark.parametrize("address", BLOCKED)
def test_non_public_addresses_are_blocked(address):
    assert is_public_ip(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_are_allowed(address):
    assert is_public_ip(ipaddress.ip_address(address)) is True


# --- resolution policy ---------------------------------------------------------------------------


def test_every_resolved_address_must_be_public():
    mixed = lambda host, port: [PUBLIC_IP, "10.0.0.5"]  # noqa: E731
    with pytest.raises(AddressBlockedError):
        resolve_public_addresses("evil.example", 80, resolver=mixed)


def test_a_single_private_record_among_many_public_blocks_the_host():
    many = lambda host, port: ["8.8.8.8", "1.1.1.1", PUBLIC_IP, "127.0.0.1", "9.9.9.9"]  # noqa: E731
    with pytest.raises(AddressBlockedError):
        resolve_public_addresses("evil.example", 443, resolver=many)


def test_all_public_addresses_returned_deduplicated_in_order():
    resolver = lambda host, port: ["8.8.8.8", "1.1.1.1", "8.8.8.8"]  # noqa: E731
    assert resolve_public_addresses("ok.example", 443, resolver=resolver) == ["8.8.8.8", "1.1.1.1"]


def test_ip_literals_skip_dns_and_are_still_checked():
    def resolver(host, port):
        raise AssertionError("DNS must not be consulted for IP literals")

    assert resolve_public_addresses("8.8.8.8", 80, resolver=resolver) == ["8.8.8.8"]
    with pytest.raises(AddressBlockedError):
        resolve_public_addresses("127.0.0.1", 80, resolver=resolver)
    with pytest.raises(AddressBlockedError):
        resolve_public_addresses("::1", 80, resolver=resolver)


def test_ipv6_zone_ids_are_stripped_before_checking():
    resolver = lambda host, port: ["fe80::1%eth0"]  # noqa: E731
    with pytest.raises(AddressBlockedError):
        resolve_public_addresses("x.example", 80, resolver=resolver)


def test_empty_resolution_is_a_dns_error():
    with pytest.raises(DnsError):
        resolve_public_addresses("x.example", 80, resolver=lambda h, p: [])


def test_garbage_address_is_blocked_not_trusted():
    with pytest.raises(AddressBlockedError):
        resolve_public_addresses("x.example", 80, resolver=lambda h, p: ["not-an-ip"])


# --- end to end through the fetcher --------------------------------------------------------------


def _blocked_by(fetcher: SafeFetcher, url: str) -> FetchError:
    with pytest.raises(FetchError) as exc:
        fetcher.fetch(url)
    return exc.value


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://127.0.0.1:80/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://10.1.2.3/",
        "http://192.168.0.10/admin",
        "http://172.16.5.5/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://127.1/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "http://user:pass@example.com/",
        "https://example.com:22/",
        "https://example.com:6379/",
        "http://example.com:8080/",
        "http://example.com:9200/_cat/indices",
    ],
)
def test_internal_and_dangerous_destinations_never_reach_the_network(url):
    site = FakeSite({"/": html("<p>should never be served</p>")})
    error = _blocked_by(fetcher_for(site), url)
    assert error.code == "blocked"
    assert site.requests == []  # nothing was sent anywhere


@pytest.mark.parametrize(
    "private_ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1"]
)
def test_public_looking_hostname_that_resolves_to_a_private_address_is_blocked(private_ip):
    site = FakeSite({"/": html("<p>secret</p>")})
    fetcher = fetcher_for(site, resolver=lambda host, port: [private_ip])
    assert _blocked_by(fetcher, "https://totally-public.example/").code == "blocked"
    assert site.requests == []


def test_hostname_with_one_private_record_is_blocked():
    site = FakeSite({"/": html("<p>secret</p>")})
    fetcher = fetcher_for(site, resolver=lambda host, port: [PUBLIC_IP, "10.0.0.5"])
    assert _blocked_by(fetcher, "https://mixed.example/").code == "blocked"
    assert site.requests == []


def test_dns_failure_is_reported_as_dns():
    site = FakeSite({})

    def failing(host, port):
        raise DnsError("nxdomain")

    assert (
        _blocked_by(fetcher_for(site, resolver=failing), "https://nxdomain.example/").code == "dns"
    )


# --- DNS rebinding ---------------------------------------------------------------------------------


def test_connection_goes_to_the_validated_address_with_the_original_host_name():
    site = FakeSite({"/": html("<p>hello</p>")})
    fetcher = fetcher_for(site, resolver=lambda host, port: ["8.8.8.8"])
    result = fetcher.fetch("https://shop.example/")
    host_header, _, connected = site.requests[0]
    assert connected == "8.8.8.8"  # the request was pinned to the address that was validated
    assert host_header == "shop.example"  # ...while the site is still addressed by name
    assert result.status == 200


def test_tls_server_name_is_the_real_host_not_the_ip():
    seen = {}

    def spy(request):
        seen["sni"] = request.extensions.get("sni_hostname")
        seen["host_header"] = request.headers["host"]
        return html("<p>ok</p>")

    site = FakeSite({"/": spy})
    fetcher_for(site, resolver=lambda host, port: ["8.8.8.8"]).fetch("https://shop.example/")
    assert seen == {"sni": "shop.example", "host_header": "shop.example"}


def test_non_default_port_is_kept_in_the_host_header():
    seen = {}

    def spy(request):
        seen["host_header"] = request.headers["host"]
        return html("<p>ok</p>")

    site = FakeSite({"/": spy})
    fetcher = SafeFetcher(
        user_agent="t",
        resolver=lambda h, p: ["8.8.8.8"],
        transport=site.transport,
        allowed_ports={443, 8443},
    )
    fetcher.fetch("https://shop.example:8443/")
    assert seen["host_header"] == "shop.example:8443"


def test_dns_rebinding_between_hops_cannot_reach_an_internal_address():
    """The attacker answers DNS with a public IP when asked about the first URL and with a private
    IP for any later lookup. Each hop is resolved once and the answer is validated and used, so
    the rebound answer is simply refused when the redirect is followed."""
    answers = iter([["8.8.8.8"], ["127.0.0.1"]])
    calls = []

    def rebinding(host, port):
        calls.append(host)
        return next(answers)

    site = FakeSite({"/start": redirect("https://attacker.example/internal")})
    fetcher = fetcher_for(site, resolver=rebinding)
    assert _blocked_by(fetcher, "https://attacker.example/start").code == "blocked"
    assert calls == ["attacker.example", "attacker.example"]  # exactly one lookup per hop
    assert [path for _, path, _ in site.requests] == ["/start"]  # the second hop sent nothing


def test_each_hop_is_resolved_exactly_once():
    calls = []

    def resolver(host, port):
        calls.append(host)
        return ["8.8.8.8"]

    site = FakeSite({"/a": redirect("/b"), "/b": redirect("/c"), "/c": html("<p>done</p>")})
    fetcher_for(site, resolver=resolver).fetch("https://shop.example/a")
    assert calls == ["shop.example"] * 3


# --- redirects ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/admin",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "http://user:pw@shop.example/",
        "https://shop.example:6379/",
    ],
)
def test_redirect_from_public_site_to_internal_target_is_blocked(target):
    site = FakeSite({"/start": redirect(target), "/": html("<p>x</p>")})
    fetcher = fetcher_for(site)
    assert _blocked_by(fetcher, "https://shop.example/start").code == "blocked"
    assert [path for _, path, _ in site.requests] == ["/start"]  # only the first hop was fetched


def test_unparseable_redirect_target_is_a_bad_response_not_a_crash():
    # httpx itself raises InvalidURL while pre-parsing this Location; the fetcher must contain it.
    site = FakeSite({"/start": redirect("javascript:alert(1)")})
    assert _blocked_by(fetcher_for(site), "https://shop.example/start").code == "bad_response"
    assert len(site.requests) == 1


def test_redirect_to_hostname_resolving_privately_is_blocked():
    def resolver(host, port):
        return ["10.0.0.5"] if host == "internal.corp.example" else [PUBLIC_IP]

    site = FakeSite({"/start": redirect("https://internal.corp.example/secrets")})
    fetcher = fetcher_for(site, resolver=resolver)
    assert _blocked_by(fetcher, "https://shop.example/start").code == "blocked"
    assert len(site.requests) == 1


def test_redirect_chain_is_followed_and_validated_hop_by_hop():
    site = FakeSite(
        {
            "/a": redirect("/b", 302),
            "/b": redirect("https://shop.example/c?utm_source=x", 307),
            "/c": html("<p>arrived</p>"),
        }
    )
    result = fetcher_for(site).fetch("https://shop.example/a")
    assert result.url == "https://shop.example/c"
    assert result.redirects == 2
    assert b"arrived" in result.body


def test_too_many_redirects():
    site = FakeSite({"/loop": redirect("/loop")})
    fetcher = fetcher_for(site, limits=FetchLimits(max_redirects=3))
    assert _blocked_by(fetcher, "https://shop.example/loop").code == "too_many_redirects"
    assert len(site.requests) == 4  # the original request plus 3 followed redirects


def test_redirect_without_location_is_a_bad_response():
    site = FakeSite({"/x": redirect("")})
    site.routes["/x"].headers.pop("location")
    assert _blocked_by(fetcher_for(site), "https://shop.example/x").code == "bad_response"


def test_redirect_off_site_is_refused_when_a_scope_is_given():
    site = FakeSite({"/": redirect("https://other.example/")})
    fetcher = fetcher_for(site)
    with pytest.raises(FetchError) as exc:
        fetcher.fetch("https://shop.example/", host_allowed=lambda host: host == "shop.example")
    assert exc.value.code == "off_site"
    assert len(site.requests) == 1


def test_proxy_environment_variables_are_ignored(monkeypatch):
    """A proxy would resolve names itself and bypass every address check."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    site = FakeSite({"/": html("<p>direct</p>")})
    fetcher = SafeFetcher(
        user_agent="t", resolver=lambda h, p: ["8.8.8.8"], transport=site.transport
    )
    assert fetcher.fetch("https://shop.example/").status == 200
    assert fetcher._client._trust_env is False
