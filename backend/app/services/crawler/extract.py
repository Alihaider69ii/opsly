"""HTML to clean, structured text for the knowledge base.

Only cleaned text is kept; raw HTML is never stored. Output is plain text with light structure
(``# headings``, ``- list items``, ``cell | cell`` table rows, blank lines between blocks) so the
chunker can split on real boundaries.

What is dropped: scripts/styles/iframes/svg/forms controls, navigation, sidebars, dialogs and
modals, cookie/consent banners, newsletter pop-ups, and site-wide headers/footers (unless they sit
inside the page's main content). Links are harvested from the *whole* page first, because the
navigation is exactly how the crawler discovers the FAQ, shipping and returns pages.

Known trade-offs: a footer that holds contact details is dropped when the page has a ``<main>``;
content rendered client-side by JavaScript is invisible (there is no headless browser); and an
element whose class or id looks like a banner/modal but holds < 2000 characters is removed, which
can occasionally drop a very short block that is genuinely content.
"""

import codecs
import re
import unicodedata
from dataclasses import dataclass

import lxml.html
from lxml import etree

MAX_LINKS = 1000
MAX_TITLE_CHARS = 500
MAX_DEPTH = 200  # deeper than this is treated as flat text rather than recursed into
NOISE_TEXT_LIMIT = 2000

_SKIP_TAGS = frozenset(
    "script style noscript template iframe svg canvas object embed audio video map head meta "
    "link select option button input textarea dialog datalist".split()
)
_BLOCK_TAGS = frozenset(
    "address article aside blockquote body caption center dd details dir div dl dt fieldset "
    "figcaption figure footer form header hgroup hr html main menu nav ol p pre section summary "
    "table tbody tfoot thead ul".split()
)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_NOISE_RE = re.compile(
    r"(?:^|[\s_-])(?:cookie|cookies|consent|gdpr|ccpa|onetrust|cookiebot|didomi|cc-window|"
    r"cc-banner|modal|popup|drawer|overlay|newsletter|mini-cart|cart-drawer|cart-notification|"
    r"skip-link|mobile-menu|menu-drawer|mega-menu|megamenu)(?:$|[\s_-])",
    re.IGNORECASE,
)
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")
_NEWLINE_SPACES_RE = re.compile(r" *\n *")
_MANY_NEWLINES_RE = re.compile(r"\n{2,}")
_INVISIBLE_RE = re.compile("[​-‏⁠﻿]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class ExtractedPage:
    title: str | None
    text: str
    links: list[str]


def _text_encoding(name: str | None) -> str | None:
    if not name:
        return None
    try:
        info = codecs.lookup(name)
    except LookupError:
        return None
    return name if getattr(info, "_is_text_encoding", True) else None


def detect_encoding(body: bytes, header_charset: str | None) -> str:
    if body.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    declared = _text_encoding(header_charset)
    if declared:
        return declared
    match = _META_CHARSET_RE.search(body[:4096])
    if match:
        declared = _text_encoding(match.group(1).decode("ascii", "ignore"))
        if declared:
            return declared
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8"


def _clean_inline(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = _INVISIBLE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _SPACES_RE.sub(" ", text)
    text = _NEWLINE_SPACES_RE.sub("\n", text)
    text = _MANY_NEWLINES_RE.sub("\n", text)
    return text.strip()


def _parse(body: bytes, encoding: str) -> lxml.html.HtmlElement | None:
    parser = lxml.html.HTMLParser(
        encoding=encoding,
        remove_comments=True,
        remove_pis=True,
        no_network=True,
        huge_tree=False,
        recover=True,
    )
    try:
        return lxml.html.document_fromstring(body, parser=parser)
    except (etree.LxmlError, ValueError, LookupError):
        return None


def _tag(el: lxml.html.HtmlElement) -> str | None:
    tag = el.tag
    return tag.lower() if isinstance(tag, str) else None


def _is_boilerplate(el: lxml.html.HtmlElement, tag: str, in_content: bool) -> bool:
    if tag in ("html", "body", "main"):
        return False
    if tag in ("nav", "aside"):
        return True
    role = (el.get("role") or "").lower()
    if role in ("navigation", "complementary", "dialog", "alertdialog", "search"):
        return True
    if el.get("aria-modal") == "true":
        return True
    if not in_content and (tag in ("header", "footer") or role in ("banner", "contentinfo")):
        return True
    ident = f"{el.get('id') or ''} {el.get('class') or ''}"[:300]
    return bool(
        ident.strip() and _NOISE_RE.search(ident) and len(el.text_content()) < NOISE_TEXT_LIMIT
    )


class _Walker:
    def __init__(self, max_chars: int) -> None:
        self.blocks: list[tuple[str, str]] = []
        self._buffer: list[str] = []
        self._max_chars = max_chars
        self._chars = 0
        self.full = False

    def _add(self, text: str | None) -> None:
        if text:
            # Line breaks in the HTML source are just whitespace; only <br> makes a real one.
            self._buffer.append(text.replace("\r", " ").replace("\n", " "))

    def _flush(self, kind: str = "p", level: int = 0) -> None:
        text = _clean_inline("".join(self._buffer))
        self._buffer = []
        if kind == "tr":
            text = text.strip(" |").strip()
        if not text:
            return
        if kind == "h":
            text = f"{'#' * level} {text}"
        elif kind == "li":
            text = f"- {text}"
        if self.blocks and self.blocks[-1][1] == text:
            return  # identical consecutive blocks (responsive duplicates, repeated labels)
        self.blocks.append((kind, text))
        self._chars += len(text)
        if self._chars >= self._max_chars:
            self.full = True

    def _children(self, el: lxml.html.HtmlElement, in_content: bool, depth: int) -> None:
        self._add(el.text)
        for child in el:
            self.visit(child, in_content, depth + 1)
            self._add(child.tail)

    def visit(self, el: lxml.html.HtmlElement, in_content: bool, depth: int = 0) -> None:
        if self.full:
            return
        tag = _tag(el)
        if tag is None or tag in _SKIP_TAGS or _is_boilerplate(el, tag, in_content):
            return
        if depth > MAX_DEPTH:
            self._add(el.text_content())
            return
        inside = in_content or tag in ("main", "article")
        if tag in _HEADINGS:
            self._flush()
            self._children(el, inside, depth)
            self._flush("h", _HEADINGS[tag])
        elif tag == "li":
            self._flush()
            self._children(el, inside, depth)
            self._flush("li")
        elif tag == "tr":
            self._flush()
            self._children(el, inside, depth)
            self._flush("tr")
        elif tag in ("td", "th"):
            self._add(" ")
            self._children(el, inside, depth)
            self._add(" | ")
        elif tag == "br":
            self._buffer.append("\n")
        elif tag in _BLOCK_TAGS:
            self._flush()
            self._children(el, inside, depth)
            self._flush()
        else:
            self._children(el, inside, depth)

    def render(self) -> str:
        parts: list[str] = []
        previous = ""
        for kind, text in self.blocks:
            if parts:
                parts.append("\n" if kind == previous and kind in ("li", "tr") else "\n\n")
            parts.append(text)
            previous = kind
        return "".join(parts)


def _select_root(root: lxml.html.HtmlElement) -> tuple[lxml.html.HtmlElement, bool]:
    body = root.find("body")
    if body is None:
        body = root
    elements = [el for el in body.iter() if _tag(el) is not None]
    candidates = [el for el in elements if _tag(el) == "main" or (el.get("role") or "") == "main"]
    if not candidates:
        articles = [el for el in elements if _tag(el) == "article"]
        candidates = articles if len(articles) == 1 else []
    if not candidates:
        return body, False
    sized = [(len(el.text_content()), el) for el in candidates]
    best_len, best = max(sized, key=lambda pair: pair[0])
    body_len = len(body.text_content())
    # A <main> that holds a small fraction of a large page is probably not the whole story.
    if body_len > 1500 and best_len < 0.2 * body_len:
        return body, False
    return best, True


def _title(root: lxml.html.HtmlElement) -> str | None:
    title = None
    node = root.find(".//title")
    if node is not None and node.text:
        title = _clean_inline(node.text.replace("\n", " "))
    if not title:
        heading = root.find(".//h1")
        if heading is not None:
            title = _clean_inline(heading.text_content().replace("\n", " "))
    return title[:MAX_TITLE_CHARS] if title else None


def extract_page(body: bytes, *, charset: str | None, max_chars: int) -> ExtractedPage:
    """Extract title, cleaned text (at most ``max_chars``) and raw link hrefs from an HTML page."""
    if not body.strip():
        return ExtractedPage(None, "", [])
    root = _parse(body, detect_encoding(body, charset))
    if root is None:
        return ExtractedPage(None, "", [])

    links: list[str] = []
    for anchor in root.iter("a"):
        href = anchor.get("href")
        if href:
            links.append(href)
            if len(links) >= MAX_LINKS:
                break

    content_root, in_content = _select_root(root)
    walker = _Walker(max_chars)
    walker.visit(content_root, in_content)
    walker._flush()
    text = unicodedata.normalize("NFC", walker.render())
    if len(text) > max_chars:
        cut = text.rfind("\n\n", 0, max_chars)
        text = text[: cut if cut > max_chars // 2 else max_chars].rstrip()
    return ExtractedPage(_title(root), text, links)
