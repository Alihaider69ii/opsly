"""URL validation and canonicalization for the crawler.

Canonicalization is deliberately conservative: two URLs are only merged when they are the same
resource by RFC 3986 rules (case, default port, fragment, dot-segments, percent-encoding spelling)
or differ only by well-known tracking parameters. Query parameters are never reordered or removed
otherwise, and trailing slashes are kept, so distinct product URLs (``?variant=1`` vs
``?variant=2``) are never merged. Pages that are still identical after that are caught by
content hashing.
"""

import ipaddress
import re
import string
from urllib.parse import urljoin, urlsplit, urlunsplit

import idna

MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253
DEFAULT_PORTS = {"http": 80, "https": 443}

# Exact names (lower-case) and prefixes that carry no page identity.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "_ga",
        "_gl",
    }
)
TRACKING_PREFIXES = ("utm_",)

ASSET_EXTENSIONS = frozenset(
    {
        # images / media
        "jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tif", "tiff", "heic",
        "mp4", "webm", "mov", "avi", "mkv", "mp3", "wav", "ogg", "flac", "m4a", "m4v",
        # documents / archives / binaries (PDF ingestion is a future source type)
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "zip", "rar", "7z", "gz",
        "tar", "tgz", "bz2", "dmg", "exe", "msi", "apk", "iso", "bin",
        # web assets / data
        "css", "js", "mjs", "map", "json", "xml", "rss", "atom", "txt", "woff", "woff2", "ttf",
        "otf", "eot",
    }
)  # fmt: skip

_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_PATH_SAFE = _UNRESERVED | frozenset("!$&'()*+,;=:@/")
_QUERY_SAFE = _PATH_SAFE | frozenset("?")
_HEX = frozenset(string.hexdigits)
_LABEL_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_NUMERIC_TLD_RE = re.compile(r"^(0x[0-9a-f]*|\d+)$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


class UrlError(ValueError):
    """The URL is not acceptable. The message is safe to show to end users."""


def _percent_normalize(value: str, safe: frozenset[str]) -> str:
    """Upper-case %XX escapes, decode escaped unreserved characters, encode everything unsafe."""
    data = value.encode("utf-8")
    size = len(data)
    out: list[str] = []
    i = 0
    while i < size:
        byte = data[i]
        if byte == 0x25:  # "%"
            if i + 2 < size and chr(data[i + 1]) in _HEX and chr(data[i + 2]) in _HEX:
                code = int(data[i + 1 : i + 3], 16)
                out.append(chr(code) if chr(code) in _UNRESERVED else f"%{code:02X}")
                i += 3
            else:
                out.append("%25")  # a stray percent sign is data, not an escape
                i += 1
            continue
        char = chr(byte)
        out.append(char if byte < 0x80 and char in safe else f"%{byte:02X}")
        i += 1
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    segments = path.split("/")
    out: list[str] = []
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == ".":
            if index == last:
                out.append("")
            continue
        if segment == "..":
            if len(out) > 1:
                out.pop()
            if index == last:
                out.append("")
            continue
        out.append(segment)
    result = "/".join(out)
    return result if result.startswith("/") else "/" + result


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def _clean_query(query: str) -> str:
    kept = []
    for pair in query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0]
        if _is_tracking(key):
            continue
        kept.append(_percent_normalize(pair, _QUERY_SAFE))
    return "&".join(kept)


def normalize_host(host: str) -> str:
    """Lower-case, IDNA-encode and sanity-check a host name or IP literal. Raises UrlError."""
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UrlError("The address has no host name.")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True).decode("ascii")
        except idna.IDNAError:
            raise UrlError("The host name is not valid.") from None
    if len(host) > MAX_HOST_LENGTH:
        raise UrlError("The host name is too long.")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UrlError("That address is not a public website.")
    labels = host.split(".")
    if len(labels) < 2:
        raise UrlError("Please enter a full website address, such as https://example.com.")
    if not all(_LABEL_RE.match(label) for label in labels):
        raise UrlError("The host name is not valid.")
    # "2130706433", "0x7f.1" and "0177.0.0.1" are legacy spellings of IPv4 addresses that some
    # resolvers accept. They are never legitimate public sites, so refuse them outright.
    if _NUMERIC_TLD_RE.match(labels[-1]):
        raise UrlError("The host name is not valid.")
    return host


def canonicalize_url(raw: str, base: str | None = None) -> str:
    """Return the canonical form of ``raw`` (resolved against ``base``) or raise UrlError."""
    raw = (raw or "").strip()
    if not raw:
        raise UrlError("Please enter a website address.")
    if _CONTROL_RE.search(raw):
        raise UrlError("The address contains invalid characters.")
    if "\\" in raw:
        raise UrlError("The address contains invalid characters.")
    if base is not None:
        raw = urljoin(base, raw)
    if len(raw) > MAX_URL_LENGTH:
        raise UrlError("The address is too long.")

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UrlError("The address is not valid.") from None

    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise UrlError("Only http:// and https:// addresses are supported.")
    if "@" in parts.netloc:
        raise UrlError("Addresses containing a username or password are not allowed.")
    if port == 0:
        raise UrlError("The port is not valid.")
    host = normalize_host(parts.hostname or "")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = _remove_dot_segments(_percent_normalize(parts.path or "/", _PATH_SAFE))
    query = _clean_query(parts.query)
    return urlunsplit((scheme, netloc, path, query, ""))


def url_host(url: str) -> str:
    return urlsplit(url).hostname or ""


def url_port(url: str) -> int:
    parts = urlsplit(url)
    return parts.port or DEFAULT_PORTS[parts.scheme]


def is_probably_asset(url: str) -> bool:
    path = urlsplit(url).path
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return False
    return last.rsplit(".", 1)[-1].lower() in ASSET_EXTENSIONS


def site_hosts(seed_host: str) -> frozenset[str]:
    """Hosts treated as 'the same site': the seed host and its www/non-www twin."""
    if seed_host.startswith("www."):
        return frozenset({seed_host, seed_host[4:]})
    return frozenset({seed_host, f"www.{seed_host}"})


_HIGH_VALUE = re.compile(
    r"(?:^|[/_-])(faq|faqs|about|about-us|contact|contact-us|shipping|delivery|returns?|refunds?|"
    r"polic(?:y|ies)|terms|warranty|sizing|size-guide|care|help|support|how-it-works|privacy)(?:$|[/_.-])",
    re.IGNORECASE,
)
_PRODUCTISH = re.compile(
    r"/(products?|collections?|shop|catalog|store|pages?)(?:/|$)", re.IGNORECASE
)
_LOW_VALUE = re.compile(
    r"/(blog|news|articles?|tag|tags|author|search|cart|account|login)(?:/|$)", re.I
)


def link_priority(url: str) -> int:
    """Lower is crawled first. Business-information pages beat products beat everything else, so a
    page limit cannot be consumed by a thousand product variants before the FAQ is reached."""
    parts = urlsplit(url)
    path = parts.path
    if _HIGH_VALUE.search(path):
        score = 0
    elif _PRODUCTISH.search(path):
        score = 1
    elif _LOW_VALUE.search(path):
        score = 3
    else:
        score = 2
    if parts.query:
        score += 1
    return score
