import pytest

from app.services.crawler.extract import detect_encoding, extract_page


def extract(html: str | bytes, *, charset=None, max_chars=100_000):
    body = html if isinstance(html, bytes) else html.encode("utf-8")
    return extract_page(body, charset=charset, max_chars=max_chars)


FULL_PAGE = """<!doctype html><html><head><title>Returns &amp; Refunds | Acme</title>
<script>var tracking = "SCRIPT-SECRET";</script><style>.x{color:red} /* STYLE-SECRET */</style>
<noscript>NOSCRIPT-SECRET</noscript></head>
<body>
<header><nav><a href="/shop">Shop</a> <a href="/faq">FAQ</a> NAV-NOISE</nav></header>
<div class="cookie-banner">COOKIE-NOISE We use cookies. Accept?</div>
<div id="newsletter-popup">NEWSLETTER-NOISE Subscribe!</div>
<div class="modal">MODAL-NOISE</div>
<main>
  <h1>Returns Policy</h1>
  <p>You can return items within <b>30 days</b> of delivery.<br>Items must be unused.</p>
  <h2>How to start</h2>
  <ul><li>Email us</li><li>Print the label</li></ul>
  <table><tr><th>Region</th><th>Cost</th></tr><tr><td>US</td><td>Free</td></tr></table>
  <script>alert("INLINE-SCRIPT")</script>
  <iframe src="https://ads.example/"></iframe><svg><text>SVG-NOISE</text></svg>
  <form><input type="text" value="INPUT-VALUE"><button>BUTTON-NOISE</button></form>
</main>
<aside>SIDEBAR-NOISE</aside>
<footer>FOOTER-NOISE Copyright Acme <a href="/privacy">Privacy</a></footer>
</body></html>"""


def test_title_headings_paragraphs_lists_and_tables_are_preserved():
    page = extract(FULL_PAGE)
    assert page.title == "Returns & Refunds | Acme"
    text = page.text
    assert "# Returns Policy" in text
    assert "You can return items within 30 days of delivery." in text
    assert "Items must be unused." in text  # <br> keeps a line break inside the paragraph
    assert "## How to start" in text
    assert "- Email us\n- Print the label" in text  # list items stay together
    assert "Region | Cost\nUS | Free" in text  # table rows become pipe-separated lines


@pytest.mark.parametrize(
    "noise",
    [
        "SCRIPT-SECRET",
        "STYLE-SECRET",
        "NOSCRIPT-SECRET",
        "INLINE-SCRIPT",
        "NAV-NOISE",
        "COOKIE-NOISE",
        "NEWSLETTER-NOISE",
        "MODAL-NOISE",
        "SVG-NOISE",
        "INPUT-VALUE",
        "BUTTON-NOISE",
        "SIDEBAR-NOISE",
        "FOOTER-NOISE",
        "<script",
        "<style",
        "<div",
        "</",
    ],
)
def test_scripts_styles_navigation_banners_and_markup_are_removed(noise):
    assert noise not in extract(FULL_PAGE).text


def test_links_are_harvested_from_navigation_and_footer_before_they_are_stripped():
    # The nav is exactly how the crawler finds the FAQ, shipping and returns pages.
    assert extract(FULL_PAGE).links == ["/shop", "/faq", "/privacy"]


def test_page_without_main_uses_body_but_still_drops_site_header_and_footer():
    page = extract(
        "<html><body><header>HEAD-NOISE</header><div><h1>Our story</h1><p>We make soap.</p></div>"
        "<footer>FOOT-NOISE</footer></body></html>"
    )
    assert "We make soap." in page.text
    assert "HEAD-NOISE" not in page.text and "FOOT-NOISE" not in page.text


def test_header_and_footer_inside_an_article_are_kept():
    page = extract(
        "<html><body><nav>NAV</nav><article><header><h1>Care guide</h1></header>"
        "<p>Wash cold.</p><footer>Updated 2024</footer></article></body></html>"
    )
    assert "# Care guide" in page.text and "Wash cold." in page.text and "Updated 2024" in page.text


def test_tiny_main_inside_a_big_page_falls_back_to_the_whole_body():
    filler = "<p>" + ("Useful content about shipping and returns. " * 60) + "</p>"
    page = extract(f"<html><body><main><p>skip</p></main><div>{filler}</div></body></html>")
    assert "Useful content about shipping" in page.text


def test_short_banner_class_is_removed_but_a_long_matching_container_is_kept():
    long_policy = "Our cookie policy explains everything. " * 80  # well over the 2000-char guard
    page = extract(
        f'<html><body><main><div class="cookie-policy"><p>{long_policy}</p></div></main></body></html>'
    )
    assert "Our cookie policy explains" in page.text


def test_collapsed_faq_answers_are_kept():
    # Accordions hide answers with display:none / hidden; dropping them would drop the FAQ.
    page = extract(
        '<html><body><main><h2>FAQ</h2><div class="panel" style="display:none">'
        "<p>Yes, we ship worldwide.</p></div><details><summary>Returns?</summary>"
        "<p>30 days.</p></details></main></body></html>"
    )
    assert "Yes, we ship worldwide." in page.text
    assert "Returns?" in page.text and "30 days." in page.text


def test_consecutive_duplicate_blocks_collapse():
    page = extract(
        "<html><body><main><p>Free shipping</p><p>Free shipping</p><p>Free shipping</p>"
        "<p>Different</p></main></body></html>"
    )
    assert page.text.count("Free shipping") == 1 and "Different" in page.text


def test_whitespace_nbsp_and_zero_width_characters_are_normalized():
    page = extract("<html><body><main><p>Hello   ​world⁠!   \n\n  again</p></main></body></html>")
    assert page.text == "Hello world! again"


def test_text_is_capped_at_a_block_boundary():
    paragraphs = "".join(f"<p>{'word ' * 40}paragraph {i}.</p>" for i in range(100))
    page = extract(f"<html><body><main>{paragraphs}</main></body></html>", max_chars=1000)
    assert 0 < len(page.text) <= 1000
    assert page.text.rstrip().endswith(".")  # not cut mid-sentence
    assert "paragraph 99" not in page.text


def test_deeply_nested_markup_does_not_crash():
    depth = 5000
    html = (
        "<html><body><main>"
        + "<div>" * depth
        + "deep text"
        + "</div>" * depth
        + "</main></body></html>"
    )
    page = extract(html)
    assert isinstance(page.text, str)


def test_pathological_inputs_never_raise():
    for body in (
        b"",
        b"   ",
        b"<",
        b"<<<<>>>>",
        b"\x00\x01\x02",
        b"<html",
        b"<p>" * 50_000,
        b"\xff\xfe\xfd",
    ):
        page = extract(body)
        assert isinstance(page.text, str) and isinstance(page.links, list)


def test_title_falls_back_to_first_h1():
    assert extract(
        "<html><body><main><h1> Our  Story </h1><p>x</p></main></body></html>"
    ).title == ("Our Story")
    assert extract("<html><body><p>no title anywhere</p></body></html>").title is None


def test_very_long_title_is_truncated():
    assert (
        len(extract(f"<html><head><title>{'t' * 2000}</title></head><body>x</body></html>").title)
        == 500
    )


def test_link_harvesting_is_bounded():
    anchors = "".join(f'<a href="/p{i}">x</a>' for i in range(5000))
    assert len(extract(f"<html><body>{anchors}</body></html>").links) == 1000


def test_malicious_markup_is_inert_text_never_html():
    page = extract(
        "<html><body><main><p>&lt;script&gt;alert(1)&lt;/script&gt; and <img src=x onerror=alert(2)>"
        "</p></main></body></html>"
    )
    assert "<script>alert(1)</script>" in page.text  # entity-decoded literal text, stored as text
    assert "onerror" not in page.text


# --- encodings -----------------------------------------------------------------------------------


def test_utf8_without_declaration_is_decoded_correctly():
    page = extract("<html><body><main><p>Café crème brûlée — 30 €</p></main></body></html>")
    assert "Café crème brûlée — 30 €" in page.text


def test_latin1_declared_in_the_http_header():
    body = "<html><body><main><p>Café crème</p></main></body></html>".encode("latin-1")
    assert "Café crème" in extract(body, charset="iso-8859-1").text


def test_charset_declared_in_a_meta_tag():
    body = '<html><head><meta charset="windows-1252"></head><body><main><p>Café “quoted”</p></main></body></html>'.encode(
        "cp1252"
    )
    assert "Café “quoted”" in extract(body).text


def test_undeclared_non_utf8_falls_back_to_cp1252_instead_of_mojibake_or_failure():
    body = "<html><body><main><p>Café</p></main></body></html>".encode("latin-1")
    assert "Café" in extract(body).text


def test_bogus_or_dangerous_charset_names_are_ignored():
    body = b"<html><body><main><p>plain ascii text here</p></main></body></html>"
    for bogus in ("no-such-charset", "rot13", "zlib", "base64", "../../etc/passwd"):
        assert "plain ascii text here" in extract(body, charset=bogus).text


def test_utf8_bom_is_handled():
    body = b"\xef\xbb\xbf<html><body><main><p>bom page</p></main></body></html>"
    assert "bom page" in extract(body).text


def test_detect_encoding_priorities():
    assert detect_encoding(b"<p>x</p>", "iso-8859-1") == "iso-8859-1"
    assert detect_encoding(b"<meta charset=utf-8><p>x</p>", None) == "utf-8"
    assert detect_encoding(b"<p>\xe9</p>", None) == "cp1252"
    assert detect_encoding(b"\xef\xbb\xbfhi", "latin-1") == "utf-8"
