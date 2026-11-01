"""Phase 9: inbound email HTML is sanitized once, at ingestion time, and only the sanitized
result is ever stored (``EmailMessage.body_html_sanitized``) -- the raw provider payload is never
persisted (see ``app/db/models/email.py``). Nothing downstream ever renders untrusted HTML.

Uses ``nh3`` (Python bindings to Mozilla's Ammonia, an allowlist-based Rust sanitizer) rather than
a denylist approach: anything not explicitly allowed is stripped, which is the only sound defence
against script injection, unsafe attributes (``onerror``, ``onclick``, ``javascript:`` URLs) and
the countless HTML parsing quirks a denylist misses.
"""

import nh3

ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}
MAX_HTML_BYTES = 500_000  # oversized inbound HTML is truncated, never stored unbounded


def sanitize_html(raw: str | None) -> str | None:
    """The safe-to-render form of ``raw``, or ``None`` if there was nothing to sanitize. Always
    strips ``<script>``/``<style>``/event handlers/``javascript:`` URLs; only an allowlisted set
    of formatting tags and attributes survives."""
    if not raw:
        return None
    bounded = raw[:MAX_HTML_BYTES]
    cleaned = nh3.clean(
        bounded,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    return cleaned.strip() or None
