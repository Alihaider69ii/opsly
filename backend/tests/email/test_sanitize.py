from app.services.email.sanitize import MAX_HTML_BYTES, sanitize_html


def test_none_and_empty_input_sanitize_to_none():
    assert sanitize_html(None) is None
    assert sanitize_html("") is None
    assert sanitize_html("   ") is None


def test_script_tags_are_stripped_entirely():
    out = sanitize_html("<p>hi</p><script>alert(1)</script>")
    assert "<script" not in out
    assert "alert" not in out
    assert "<p>hi</p>" in out


def test_event_handler_attributes_are_stripped():
    out = sanitize_html('<img src="x.png" onerror="alert(1)">')
    assert "onerror" not in out
    assert "alert" not in out


def test_javascript_url_scheme_is_stripped_from_href():
    out = sanitize_html('<a href="javascript:alert(1)">click</a>')
    assert "javascript:" not in out


def test_safe_http_link_survives_and_gains_safe_rel():
    out = sanitize_html('<a href="https://example.com">link</a>')
    assert 'href="https://example.com"' in out
    assert "noopener" in out
    assert "nofollow" in out


def test_style_attribute_is_not_in_the_allowlist_and_is_stripped():
    out = sanitize_html('<p style="display:none">hidden text stays</p>')
    assert "style=" not in out
    assert "hidden text stays" in out


def test_disallowed_tag_like_iframe_is_stripped():
    out = sanitize_html('<iframe src="https://evil.example"></iframe><p>ok</p>')
    assert "<iframe" not in out
    assert "<p>ok</p>" in out


def test_oversized_html_is_bounded_not_stored_unbounded():
    huge = "<p>" + ("a" * (MAX_HTML_BYTES + 1000)) + "</p>"
    out = sanitize_html(huge)
    assert len(out) <= MAX_HTML_BYTES + 50  # some slack for closing tags nh3 may add back
