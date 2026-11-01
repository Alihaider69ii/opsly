"""Phase 9: safe email template rendering.

Deliberately not a general templating language (same philosophy as the automation engine's one
bounded ``{{generated_text}}`` placeholder, see ``app/services/automation.py``): only a fixed,
dotted-path placeholder syntax -- ``{{customer.first_name}}``, ``{{order.number}}`` -- is ever
recognized. No loops, no conditionals, no function calls, no code execution of any kind.

An unknown variable (one the render context does not provide) fails safely: it is replaced with
an empty string, never left as a literal ``{{...}}`` in a customer-facing email and never raises.
Values substituted into HTML are escaped; values substituted into plain text are not.
"""

import html
import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\}\}")
MAX_TEMPLATE_CHARS = 100_000


class InvalidTemplate(ValueError):
    pass


def extract_placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text or ""))


def _resolve(path: str, context: dict[str, Any]) -> str | None:
    value: Any = context
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None  # a structured value is never a valid substitution
    return str(value)


def render(text: str | None, context: dict[str, Any], *, escape_html: bool) -> str:
    """Replace every ``{{dotted.path}}`` placeholder found in ``text``. A placeholder the
    context cannot resolve becomes an empty string -- it is never left literal."""
    if not text:
        return ""
    bounded = text[:MAX_TEMPLATE_CHARS]

    def _sub(match: re.Match[str]) -> str:
        resolved = _resolve(match.group(1), context)
        if resolved is None:
            return ""
        return html.escape(resolved) if escape_html else resolved

    return _PLACEHOLDER_RE.sub(_sub, bounded)


def render_template(
    *, subject: str, body_html: str, body_text: str | None, context: dict[str, Any]
) -> tuple[str, str, str]:
    """Returns ``(subject, html, text)``, all placeholders resolved or safely emptied."""
    rendered_subject = render(subject, context, escape_html=False)
    rendered_html = render(body_html, context, escape_html=True)
    rendered_text = render(body_text, context, escape_html=False) if body_text else ""
    return rendered_subject, rendered_html, rendered_text


def validate_variables(
    declared: list[str], *, subject: str, body_html: str, body_text: str | None
) -> None:
    """Raises ``InvalidTemplate`` if the template body references a placeholder not in
    ``declared`` -- caught at save time, not at send time, so an author sees the problem
    immediately rather than a customer receiving a silently-blanked field."""
    used = extract_placeholders(subject) | extract_placeholders(body_html)
    if body_text:
        used |= extract_placeholders(body_text)
    unknown = used - set(declared)
    if unknown:
        raise InvalidTemplate(f"undeclared template variables: {', '.join(sorted(unknown))}")
