import logging
import re
from typing import TextIO

from app.core.request_context import RequestIdFilter

# Fields that must never be logged. The helper raises instead of silently logging them, so a
# careless `log_event(..., content=message)` fails in development rather than leaking in production.
FORBIDDEN_FIELDS = frozenset(
    {
        "content",
        "message",
        "question",
        "prompt",
        "answer",
        "reply",
        "text",
        "token",
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "password",
        "secret",
        "visitor_id",
    }
)
MAX_VALUE_CHARS = 120


def _clean(value: object) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: object):
    """Structured (logfmt-style) event: ``event key=value key=value``.

    Log identifiers, counts, sizes, durations and status codes; never message text, prompts,
    tokens, cookies or credentials.
    """
    bad = FORBIDDEN_FIELDS & fields.keys()
    if bad:
        raise ValueError(f"refusing to log sensitive field(s): {sorted(bad)}")
    rendered = " ".join(f"{key}={_clean(value)}" for key, value in fields.items())
    logger.log(level, "%s %s", event, rendered)


# Query parameters whose values are credentials or single-use proofs (OAuth ``code``/``state``/
# ``hmac`` arrive in the Shopify callback's URL). uvicorn's access log prints the whole request URL.
SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "code",
        "state",
        "hmac",
        "signature",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "key",
        "secret",
        "password",
    }
)


def redact_query(target: str) -> str:
    """``/path?code=abc&shop=x`` -> ``/path?code=[redacted]&shop=x``. Other text is untouched."""
    path, separator, query = target.partition("?")
    if not separator:
        return target
    pairs = []
    for pair in query.split("&"):
        name, equals, _value = pair.partition("=")
        pairs.append(
            f"{name}=[redacted]" if equals and name.lower() in SENSITIVE_QUERY_PARAMS else pair
        )
    return f"{path}?{'&'.join(pairs)}"


class RedactSensitiveQuery(logging.Filter):
    """For ``uvicorn.access``: its record args are (client, method, path?query, version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_query(args[2]), *args[3:])
        return True


# Last line of defence for exception text and stray messages. PostgreSQL's own error messages echo
# the offending value (a unique violation prints ``Key (email)=(a@b.c) already exists``; a bad cast
# prints ``invalid input syntax for type integer: "<value>"``), which SQLAlchemy's
# ``hide_parameters`` cannot suppress, and a library may include a credential in its message.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"Key \((?P<cols>[^)\n]*)\)=\((?P<value>.*?)\)"
            r"(?= (?:already exists|is not present|is still referenced|conflicts))"
        ),
        r"Key (\g<cols>)=([redacted])",
    ),
    (re.compile(r"Failing row contains \(.*\)"), "Failing row contains ([redacted])"),
    (
        re.compile(r"(invalid input (?:syntax|value) for [^:\n]+): \"[^\n]*\""),
        r'\1: "[redacted]"',
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [redacted]"),
    (re.compile(r"\bshp(?:at|ca|ss|pa|rt)_[A-Za-z0-9]{16,}"), "[redacted-shopify-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[redacted-api-key]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "[redacted-jwt]",
    ),
    (
        re.compile(r"(?i)\b(X-Shopify-Access-Token|X-Api-Key|api-key|Authorization)\s*[:=]\s*\S+"),
        r"\1: [redacted]",
    ),
)


def redact_secrets(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class SafeFormatter(logging.Formatter):
    """Formats like ``logging.Formatter`` (traceback included), then redacts what must not stay."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


_HANDLER_FLAG = "_bizflow_handler"


def quiet_http_clients() -> None:
    """``httpx`` logs every request URL at INFO. URLs can carry secrets (an OAuth callback's
    code and signature, a merchant-supplied page's query string), so only warnings and errors
    from the HTTP libraries are ever emitted."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(level: str | int = "INFO", stream: TextIO | None = None) -> None:
    """Make the application's own loggers visible.

    uvicorn configures only its own loggers; with nothing configured for ``app.*`` the root logger
    (WARNING, no handler) silently drops every INFO event. Safe to call repeatedly: it replaces the
    handler it installed earlier instead of stacking another.
    """
    app_logger = logging.getLogger("app")
    for existing in [h for h in app_logger.handlers if getattr(h, _HANDLER_FLAG, False)]:
        app_logger.removeHandler(existing)
    handler = logging.StreamHandler(stream)
    setattr(handler, _HANDLER_FLAG, True)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s%(request_suffix)s")
    )
    app_logger.addHandler(handler)
    app_logger.setLevel(level)
    quiet_http_clients()

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSensitiveQuery) for f in access_logger.filters):
        access_logger.addFilter(RedactSensitiveQuery())
