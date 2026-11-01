"""Mutants for Phase 7: the per-organization send rate limit, Meta-error-code-aware classification
(distinguishing a transient throughput error from a permanent validation error when both arrive as
plain HTTP 400), and capturing Meta's own error code on a failed status webhook.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


SEND = "app/services/messaging/whatsapp/send.py"
HTTP = "app/services/messaging/providers/_http.py"
WEBHOOKS = "app/services/messaging/whatsapp/webhooks.py"

SEND_UNIT = ["tests/whatsapp/test_send.py"]
PROVIDER_UNIT = ["tests/whatsapp/test_provider.py"]
WEBHOOKS_UNIT = ["tests/whatsapp/test_webhooks.py"]

# --- send.py: the per-organization rate limit -----------------------------------------------------

add(
    "wa7-01-send-rate-limit-not-checked",
    SEND,
    "    if _rate_limit_exceeded(organization_id):\n        return MessagingErrorCode.RATE_LIMITED",
    "    if False:\n        return MessagingErrorCode.RATE_LIMITED",
    SEND_UNIT,
)
add(
    "wa7-02-rate-limit-allow-check-inverted",
    SEND,
    "    return not limiter.allow(",
    "    return limiter.allow(",
    SEND_UNIT,
)

# --- _http.py: Meta error-code-aware classification ------------------------------------------------

add(
    "wa7-03-vendor-rate-limit-code-not-classified",
    HTTP,
    "    if vendor_code in _RATE_LIMIT_ERROR_CODES:\n        return MessagingErrorCode.RATE_LIMITED",
    "    if False:\n        return MessagingErrorCode.RATE_LIMITED",
    PROVIDER_UNIT,
)
add(
    "wa7-04-vendor-auth-code-not-classified",
    HTTP,
    "    if vendor_code in _AUTH_ERROR_CODES:\n        return MessagingErrorCode.AUTHENTICATION_ERROR",
    "    if False:\n        return MessagingErrorCode.AUTHENTICATION_ERROR",
    PROVIDER_UNIT,
)
add(
    "wa7-05-vendor-transient-code-not-classified",
    HTTP,
    (
        "    if vendor_code in _TRANSIENT_ERROR_CODES:\n"
        "        return MessagingErrorCode.PROVIDER_UNAVAILABLE"
    ),
    "    if False:\n        return MessagingErrorCode.PROVIDER_UNAVAILABLE",
    PROVIDER_UNIT,
)
add(
    "wa7-06-error-body-not-passed-to-classifier",
    HTTP,
    "classify_http_status(response.status_code, safe_json(response))",
    "classify_http_status(response.status_code)",
    PROVIDER_UNIT,
)

# --- webhooks.py: Meta's own error code on a failed status -----------------------------------------

add(
    "wa7-07-failed-status-error-code-not-captured",
    WEBHOOKS,
    (
        "        message.error_code = message.error_code or (\n"
        '            f"meta_{reported_code}"\n'
        "            if isinstance(reported_code, int)\n"
        '            else "provider_reported_failure"\n'
        "        )"
    ),
    '        message.error_code = message.error_code or "provider_reported_failure"',
    WEBHOOKS_UNIT,
)
add(
    "wa7-08-status-error-field-not-parsed",
    WEBHOOKS,
    (
        "                error_code = None\n"
        '                if status == "failed":\n'
        '                    errors = st.get("errors")\n'
        "                    if isinstance(errors, list) and errors and isinstance(errors[0], dict):\n"
        '                        code = errors[0].get("code")\n'
        "                        error_code = code if isinstance(code, int) else None"
    ),
    "                error_code = None",
    WEBHOOKS_UNIT,
)
