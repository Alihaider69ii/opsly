import base64

from app.services.email.validation import sign_svix_body, verify_svix_signature

SECRET = "whsec_" + base64.b64encode(b"a-32-byte-test-signing-key-here").decode()


def _sign(body: bytes, svix_id="msg_1", svix_timestamp="1000"):
    return sign_svix_body(svix_id=svix_id, svix_timestamp=svix_timestamp, body=body, secret=SECRET)


def test_correctly_signed_webhook_is_accepted():
    body = b'{"type":"email.sent"}'
    sig = _sign(body)
    assert verify_svix_signature(
        body=body, svix_id="msg_1", svix_timestamp="1000", svix_signature=sig, secret=SECRET, now=1000
    ) is True


def test_wrong_secret_is_rejected():
    body = b'{"type":"email.sent"}'
    sig = _sign(body)
    other_secret = "whsec_" + base64.b64encode(b"a-different-32-byte-key-value!!!").decode()
    assert verify_svix_signature(
        body=body, svix_id="msg_1", svix_timestamp="1000", svix_signature=sig, secret=other_secret, now=1000
    ) is False


def test_tampered_body_is_rejected():
    body = b'{"type":"email.sent"}'
    sig = _sign(body)
    tampered = b'{"type":"email.bounced"}'
    assert verify_svix_signature(
        body=tampered, svix_id="msg_1", svix_timestamp="1000", svix_signature=sig, secret=SECRET, now=1000
    ) is False


def test_mismatched_id_or_timestamp_invalidates_the_signature():
    body = b'{"type":"email.sent"}'
    sig = _sign(body, svix_id="msg_1", svix_timestamp="1000")
    assert verify_svix_signature(
        body=body, svix_id="msg_2", svix_timestamp="1000", svix_signature=sig, secret=SECRET, now=1000
    ) is False


def test_timestamp_outside_skew_tolerance_is_rejected_as_replay_protection():
    body = b'{"type":"email.sent"}'
    sig = _sign(body, svix_timestamp="1000")
    # far outside the 300s tolerance relative to "now"
    assert verify_svix_signature(
        body=body, svix_id="msg_1", svix_timestamp="1000", svix_signature=sig, secret=SECRET, now=10_000
    ) is False


def test_missing_headers_or_secret_fail_closed():
    body = b"{}"
    assert verify_svix_signature(
        body=body, svix_id=None, svix_timestamp="1000", svix_signature="v1,abc", secret=SECRET
    ) is False
    assert verify_svix_signature(
        body=body, svix_id="msg_1", svix_timestamp="1000", svix_signature="v1,abc", secret=""
    ) is False


def test_non_numeric_timestamp_is_rejected():
    body = b"{}"
    assert verify_svix_signature(
        body=body, svix_id="msg_1", svix_timestamp="not-a-number", svix_signature="v1,abc", secret=SECRET
    ) is False


def test_multiple_space_separated_signature_candidates_any_match_accepted():
    """Supports Svix secret rotation: several candidate signatures are sent, any valid one passes."""
    body = b'{"type":"email.sent"}'
    real_sig = _sign(body)
    combined = f"v1,bogus-signature-value {real_sig}"
    assert verify_svix_signature(
        body=body, svix_id="msg_1", svix_timestamp="1000", svix_signature=combined, secret=SECRET, now=1000
    ) is True
