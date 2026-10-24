import io
import logging

import pytest

from app.core.logging import configure_logging, log_event

FLAG = "_bizflow_handler"


@pytest.fixture
def clean_app_logger():
    app_logger = logging.getLogger("app")
    handlers, level = list(app_logger.handlers), app_logger.level
    app_logger.handlers[:] = [h for h in handlers if not getattr(h, FLAG, False)]
    app_logger.setLevel(logging.NOTSET)
    yield app_logger
    app_logger.handlers[:] = handlers
    app_logger.setLevel(level)


def test_info_events_from_application_loggers_are_actually_emitted(clean_app_logger):
    stream = io.StringIO()
    configure_logging("INFO", stream)
    log_event(
        logging.getLogger("app.services.chat.service"), "chat.message_received", conversation="c1"
    )
    logging.getLogger("app.services.chat.service").debug("hidden detail")
    output = stream.getvalue()
    assert "INFO app.services.chat.service chat.message_received conversation=c1" in output
    assert "hidden detail" not in output


def test_the_level_is_configurable(clean_app_logger):
    stream = io.StringIO()
    configure_logging("WARNING", stream)
    log_event(logging.getLogger("app.x"), "quiet.event", n=1)
    log_event(logging.getLogger("app.x"), "loud.event", level=logging.WARNING, n=2)
    assert "quiet.event" not in stream.getvalue() and "loud.event" in stream.getvalue()


def test_configuring_twice_does_not_duplicate_output(clean_app_logger):
    first, second = io.StringIO(), io.StringIO()
    configure_logging("INFO", first)
    configure_logging("INFO", second)
    log_event(logging.getLogger("app.x"), "once.only", n=1)
    assert first.getvalue() == ""
    assert second.getvalue().count("once.only") == 1


def test_the_application_configures_logging_when_it_starts(clean_app_logger):
    from app.main import create_app

    assert not any(getattr(h, FLAG, False) for h in clean_app_logger.handlers)
    create_app()
    assert any(getattr(h, FLAG, False) for h in clean_app_logger.handlers)
    assert logging.getLogger("app.services.ai.rag").isEnabledFor(logging.INFO)


# --- the access log must not record credentials that travel in a URL ------------------------------


def test_redact_query_hides_only_the_sensitive_parameters():
    from app.core.logging import redact_query

    assert (
        redact_query("/cb?code=abc&shop=a.myshopify.com&state=s&hmac=h&timestamp=1")
        == "/cb?code=[redacted]&shop=a.myshopify.com&state=[redacted]&hmac=[redacted]&timestamp=1"
    )
    assert redact_query("/plain/path") == "/plain/path"
    assert redact_query("/x?flag&page=2") == "/x?flag&page=2"
    assert (
        redact_query("/x?CODE=abc&Access_Token=t") == "/x?CODE=[redacted]&Access_Token=[redacted]"
    )
    assert redact_query("/x?code=a=b=c") == "/x?code=[redacted]"


def test_oauth_callback_codes_never_reach_the_access_log(clean_app_logger):
    configure_logging("INFO", io.StringIO())
    access = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    previous_level = access.level
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000",
            "GET",
            "/api/integrations/shopify/callback?code=SECRETCODE&shop=a.myshopify.com"
            "&state=SECRETSTATE&hmac=SECRETHMAC&timestamp=1",
            "1.1",
            200,
        )
    finally:
        access.removeHandler(handler)
        access.setLevel(previous_level)
    line = stream.getvalue()
    assert "SECRETCODE" not in line and "SECRETSTATE" not in line and "SECRETHMAC" not in line
    assert "shop=a.myshopify.com" in line and "/api/integrations/shopify/callback" in line
    assert line.rstrip().endswith("200")


def test_configuring_logging_repeatedly_installs_the_redaction_once(clean_app_logger):
    from app.core.logging import RedactSensitiveQuery

    for _ in range(3):
        configure_logging("INFO", io.StringIO())
    filters = [
        f
        for f in logging.getLogger("uvicorn.access").filters
        if isinstance(f, RedactSensitiveQuery)
    ]
    assert len(filters) == 1
