"""Log output is redacted after formatting, so credentials and echoed database values cannot persist
even when a library puts them in an exception message."""

import io
import logging

import pytest

from app.core.logging import configure_logging, redact_secrets

SHOPIFY_TOKEN = "shpat_" + "0123456789abcdef" * 2
OPENAI_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wxyz-0123456789"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        ("Key (email)=(buyer@example.com) already exists.", "buyer@example.com"),
        ("Key (organization_id, email)=(7b1c, a@b.co) already exists.", "a@b.co"),
        ("Key (email)=(has ) paren@example.com) already exists.", "paren@example.com"),
        ('Key (user_id)=(3) is not present in table "users".', "(3)"),
        ('Key (id)=(5) is still referenced from table "orders".', "(5)"),
        ("Failing row contains (1, buyer@example.com, +4930123456).", "+4930123456"),
        ('invalid input syntax for type integer: "buyer@example.com"', "buyer@example.com"),
        ('invalid input value for enum order_status: "secret-state"', "secret-state"),
        (f"Authorization: Bearer {SHOPIFY_TOKEN}", SHOPIFY_TOKEN),
        (f"request failed: Bearer {OPENAI_KEY}", OPENAI_KEY),
        (f"X-Shopify-Access-Token: {SHOPIFY_TOKEN}", SHOPIFY_TOKEN),
        (f"x-api-key={ANTHROPIC_KEY}", ANTHROPIC_KEY),
        (f"token {SHOPIFY_TOKEN} was rejected", SHOPIFY_TOKEN),
        (f"incorrect API key provided: {OPENAI_KEY}.", OPENAI_KEY),
        (f"session {JWT} expired", JWT),
        ("shprt_" + "f" * 32, "f" * 32),
        ("shpca_" + "a" * 32, "a" * 32),
    ],
)
def test_sensitive_text_does_not_survive(raw, gone):
    assert gone not in redact_secrets(f"before {raw} after")


@pytest.mark.parametrize(
    "line",
    [
        "shopify sync completed connection=7b1c4d92-1111-4222-8333-444455556666 products=120",
        "retrieval ok chunks=5 duration_ms=412 request_id=trace-0001-abcdef",
        'POST /api/auth/login 401 "Invalid credentials"',
        "worker started id=host-1234 queues=3",
        "the key to scaling is batching; the bearer of the order was notified",  # prose, not secrets
        "sk-short",
        "product 'shpat' is a word in a title",
    ],
)
def test_ordinary_log_lines_are_unchanged(line):
    assert redact_secrets(line) == line


def test_the_shape_of_a_database_error_is_kept_so_it_is_still_diagnosable():
    out = redact_secrets(
        'duplicate key value violates unique constraint "uq_users_email"\n'
        "DETAIL:  Key (email)=(buyer@example.com) already exists."
    )
    assert "uq_users_email" in out and "Key (email)=([redacted]) already exists." in out


def test_a_logged_traceback_is_redacted_too_not_just_the_message():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise RuntimeError(
            f"upstream said: Key (email)=(victim@example.com) already exists {OPENAI_KEY}"
        )
    except RuntimeError:
        logging.getLogger("app.test").exception("job crashed job=abc")
    text = stream.getvalue()
    assert "Traceback" in text and "job crashed job=abc" in text
    assert "victim@example.com" not in text and OPENAI_KEY not in text


def test_the_message_arguments_are_redacted_as_well():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("app.test").warning("provider error: %s", f"bad key {OPENAI_KEY}")
    assert OPENAI_KEY not in stream.getvalue() and "[redacted-api-key]" in stream.getvalue()


def test_redaction_is_idempotent():
    once = redact_secrets(f"Authorization: Bearer {SHOPIFY_TOKEN} Key (a)=(b) already exists")
    assert redact_secrets(once) == once


# --- URLs carry secrets, so the HTTP libraries' per-request INFO lines are switched off ----------------------


def _fetch_secret_url():
    import httpx

    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        client.get(
            "https://merchant.example/page?code=OAUTH-CODE-123&token=TOKEN-456&hmac=HMAC-789"
        )


def test_the_api_never_logs_request_urls_even_if_the_root_logger_is_verbose(caplog):
    configure_logging("INFO", io.StringIO())
    caplog.set_level(logging.DEBUG)  # a hosting platform may configure the root logger itself
    _fetch_secret_url()
    for secret in ("OAUTH-CODE-123", "TOKEN-456", "HMAC-789"):
        assert secret not in caplog.text


def test_the_worker_never_logs_request_urls_either_and_redacts_like_the_api(caplog, capsys):
    from app.workers import main as worker

    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    try:
        worker.configure_worker_logging()
        logging.getLogger("bizflow.worker").error("token %s rejected", SHOPIFY_TOKEN)
        _fetch_secret_url()
        written = capsys.readouterr().err
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
    assert "OAUTH-CODE-123" not in written and "TOKEN-456" not in written
    assert SHOPIFY_TOKEN not in written and "[redacted-shopify-token]" in written


# --- bearer / basic credentials in free text (no other pattern may be the one catching them) -------------------


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        (
            "upstream rejected Bearer Zm9vYmFyYmF6cXV4MTIzNDU2 for the request",
            "Zm9vYmFyYmF6cXV4MTIzNDU2",
        ),
        (
            "proxy said: bearer opaque-session-id.0123456789abcdef",
            "opaque-session-id.0123456789abcdef",
        ),
        ("401 for Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng==", "dXNlcjpwYXNzd29yZDEyMzQ1Ng=="),
    ],
)
def test_opaque_bearer_and_basic_credentials_are_redacted_wherever_they_appear(raw, gone):
    out = redact_secrets(raw)
    assert gone not in out and "[redacted]" in out


@pytest.mark.parametrize(
    "line",
    [
        "basic configuration and bearer information were applied",
        "the basic requirements are listed on the settings page",
        "Bearer of bad news: connection reset",
    ],
)
def test_ordinary_sentences_with_those_words_are_left_alone(line):
    assert redact_secrets(line) == line
