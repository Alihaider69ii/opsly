"""SSRF-hardened HTTP fetching.

For every request (and every redirect hop) the fetcher:

1. canonicalizes the URL and checks scheme, port and (optionally) that the host is in scope;
2. resolves the host itself and requires every address to be public (ssrf.py);
3. connects to the validated IP address, sending the original host name as the ``Host`` header and
   as the TLS SNI / certificate-verification name, so the address that was checked is the address
   that is used. There is no second DNS lookup for a rebinding attacker to answer differently;
4. refuses to follow redirects automatically: the ``Location`` is run through steps 1-3 again.

It also bounds everything an untrusted server controls: response size (including after
decompression), wall-clock time per request, redirect count, content type and encodings. Proxy
environment variables are ignored (``trust_env=False``): a proxy would resolve names itself and
bypass these checks. Connections are never reused, so a pooled connection can't carry one site's
TLS identity to another.
"""

import re
import ssl
import time
import zlib
from collections.abc import Callable, Collection
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.services.crawler.ssrf import (
    AddressBlockedError,
    DnsError,
    IpValidator,
    Resolver,
    is_public_ip,
    resolve_public_addresses,
    system_resolver,
)
from app.services.crawler.urls import DEFAULT_PORTS, UrlError, canonicalize_url, url_host, url_port

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_ADDRESS_ATTEMPTS = 3
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)


class FetchError(Exception):
    """A fetch failed. ``code`` is a stable machine-readable reason; ``detail`` is for server logs
    only and must never be shown to users."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FetchLimits:
    max_bytes: int = 2_000_000
    max_redirects: int = 5
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    request_timeout: float = 20.0  # wall-clock for one fetch including redirects and the body


@dataclass(frozen=True)
class FetchResult:
    url: str  # final canonical URL after redirects
    status: int
    content_type: str | None  # lower-case media type without parameters
    charset: str | None
    body: bytes  # empty for non-2xx responses
    redirects: int


def _parse_content_type(header: str | None) -> tuple[str | None, str | None]:
    if not header:
        return None, None
    media = header.split(";", 1)[0].strip().lower() or None
    match = _CHARSET_RE.search(header)
    return media, (match.group(1).lower() if match else None)


class _Inflater:
    """gzip/zlib decoder whose output is bounded, so a tiny 'zip bomb' can't expand in memory."""

    def __init__(self) -> None:
        self._d = zlib.decompressobj(zlib.MAX_WBITS | 32)  # auto-detect gzip or zlib framing

    def feed(self, data: bytes, max_out: int) -> bytes:
        """Return at most ``max_out + 1`` decoded bytes (the extra byte signals overflow)."""
        out = bytearray(self._d.decompress(data, max_out + 1))
        while self._d.unconsumed_tail and len(out) <= max_out:
            out += self._d.decompress(self._d.unconsumed_tail, max_out + 1 - len(out))
        return bytes(out)


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class SafeFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        limits: FetchLimits | None = None,
        allowed_ports: Collection[int] | None = frozenset({80, 443}),
        resolver: Resolver = system_resolver,
        ip_validator: IpValidator = is_public_ip,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._limits = limits or FetchLimits()
        self._user_agent = user_agent
        self._allowed_ports = None if allowed_ports is None else frozenset(allowed_ports)
        self._resolver = resolver
        self._validator = ip_validator
        timeout = httpx.Timeout(
            connect=self._limits.connect_timeout,
            read=self._limits.read_timeout,
            write=self._limits.connect_timeout,
            pool=self._limits.connect_timeout,
        )
        self._client = httpx.Client(
            transport=transport
            or httpx.HTTPTransport(
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=0)
            ),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SafeFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        accept_types: Collection[str] | None = None,
        host_allowed: Callable[[str], bool] | None = None,
        max_bytes: int | None = None,
        truncate: bool = False,
    ) -> FetchResult:
        """GET ``url``. Non-2xx statuses are returned (with an empty body), not raised.

        ``accept_types``: media types accepted for a 2xx body; anything else raises
        ``unsupported_content`` before the body is read. ``host_allowed`` is applied to the
        initial URL and to every redirect target. ``truncate`` returns the first ``max_bytes``
        instead of failing when the body is larger.
        """
        limit = max_bytes or self._limits.max_bytes
        deadline = time.monotonic() + self._limits.request_timeout
        current = self._canonical(url)
        redirects = 0
        while True:
            if time.monotonic() > deadline:
                raise FetchError("timeout", "deadline exceeded between hops")
            self._check_destination(current, host_allowed)
            response = self._open(current)
            try:
                status = response.status_code
                if status in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("bad_response", "redirect without Location")
                    redirects += 1
                    if redirects > self._limits.max_redirects:
                        raise FetchError("too_many_redirects")
                    current = self._canonical(location, base=current)
                    continue
                content_type, charset = _parse_content_type(response.headers.get("content-type"))
                if not 200 <= status < 300:
                    return FetchResult(current, status, content_type, charset, b"", redirects)
                if accept_types is not None and content_type not in accept_types:
                    raise FetchError("unsupported_content", str(content_type))
                body = self._read_body(response, limit, deadline, truncate)
                return FetchResult(current, status, content_type, charset, body, redirects)
            finally:
                response.close()

    # ------------------------------------------------------------------------------------------

    @staticmethod
    def _canonical(url: str, base: str | None = None) -> str:
        try:
            return canonicalize_url(url, base=base)
        except UrlError as exc:
            raise FetchError("blocked", f"url rejected: {exc}") from None

    def _check_destination(self, url: str, host_allowed: Callable[[str], bool] | None) -> None:
        if self._allowed_ports is not None and url_port(url) not in self._allowed_ports:
            raise FetchError("blocked", "port not allowed")
        if host_allowed is not None and not host_allowed(url_host(url)):
            raise FetchError("off_site", "host outside the site being crawled")

    def _open(self, url: str) -> httpx.Response:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = url_port(url)
        try:
            addresses = resolve_public_addresses(
                host, port, resolver=self._resolver, validator=self._validator
            )
        except AddressBlockedError:
            raise FetchError("blocked", "destination address is not public") from None
        except DnsError:
            raise FetchError("dns", "host did not resolve") from None

        bracketed = f"[{host}]" if ":" in host else host
        host_header = bracketed if port == DEFAULT_PORTS[parts.scheme] else f"{bracketed}:{port}"
        path_and_query = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {
            "Host": host_header,
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
        }

        tls_failure = False
        for ip in addresses[:MAX_ADDRESS_ATTEMPTS]:
            netloc = f"[{ip}]" if ":" in ip else ip
            request = self._client.build_request(
                "GET",
                f"{parts.scheme}://{netloc}:{port}{path_and_query}",
                headers=headers,
                # TLS: send and verify the certificate against the real host name, not the IP.
                extensions={"sni_hostname": host},
            )
            try:
                return self._client.send(request, stream=True)
            except httpx.ConnectError as exc:
                tls_failure = tls_failure or _is_tls_error(exc)
            except httpx.ConnectTimeout:
                pass
            except httpx.TimeoutException:
                raise FetchError("timeout", "no response in time") from None
            except (httpx.InvalidURL, httpx.RequestError) as exc:
                # InvalidURL: httpx pre-parses Location headers even with redirects disabled, so a
                # hostile server can make it raise. Anything unexpected is a bad response, never a
                # crash of the whole crawl.
                raise FetchError("bad_response", type(exc).__name__) from None
        raise FetchError("tls" if tls_failure else "connect", "could not connect")

    def _read_body(
        self, response: httpx.Response, limit: int, deadline: float, truncate: bool
    ) -> bytes:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        inflater: _Inflater | None = None
        if encoding in ("gzip", "x-gzip", "deflate"):
            inflater = _Inflater()
        elif encoding not in ("", "identity"):
            raise FetchError("bad_response", f"unsupported content-encoding {encoding}")

        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit and not truncate and inflater is None:
            raise FetchError("too_large", "declared content-length over limit")

        chunks: list[bytes] = []
        size = 0
        raw_total = 0
        try:
            # No chunk_size on purpose: iter_raw(n) buffers until n bytes have arrived, which would
            # let a server trickling one byte at a time hide from the deadline check below.
            for raw in response.iter_raw():
                if time.monotonic() > deadline:
                    raise FetchError("timeout", "body not received in time")
                raw_total += len(raw)
                if raw_total > limit and not truncate:
                    raise FetchError("too_large", "raw bytes over limit")
                data = inflater.feed(raw, limit - size) if inflater else raw
                if size + len(data) > limit:
                    if truncate:
                        chunks.append(data[: limit - size])
                        break
                    raise FetchError("too_large", "decoded bytes over limit")
                chunks.append(data)
                size += len(data)
        except httpx.TimeoutException:
            raise FetchError("timeout", "read timed out") from None
        except (httpx.TransportError, zlib.error):
            raise FetchError("bad_response", "body could not be read") from None
        return b"".join(chunks)
