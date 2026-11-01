"""Mutants for Phase 6: the WhatsApp status state machine, the customer-service window, the
outbound send policy gate and idempotency boundary, template approval/variable validation, phone
normalization's fail-closed guard, webhook signature verification and dedupe, and the automation
engine's WhatsApp-specific validation and crash-recovery extensions.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


STATE = "app/services/messaging/whatsapp/state.py"
WINDOW = "app/services/messaging/whatsapp/window.py"
SEND = "app/services/messaging/whatsapp/send.py"
TEMPLATES = "app/services/messaging/whatsapp/templates.py"
PHONE = "app/services/messaging/phone.py"
WEBHOOKS = "app/services/messaging/whatsapp/webhooks.py"
VALIDATION = "app/services/messaging/whatsapp/validation.py"
AUTOMATION = "app/services/automation.py"

STATE_UNIT = ["tests/whatsapp/test_state.py"]
WINDOW_UNIT = ["tests/whatsapp/test_window.py"]
SEND_UNIT = ["tests/whatsapp/test_send.py", "tests/whatsapp/test_security.py"]
TEMPLATES_UNIT = ["tests/whatsapp/test_templates.py", "tests/whatsapp/test_send.py"]
PHONE_UNIT = ["tests/whatsapp/test_phone.py"]
WEBHOOKS_UNIT = ["tests/whatsapp/test_webhooks.py", "tests/whatsapp/test_api.py"]
AUTOMATION_UNIT = ["tests/whatsapp/test_automation_integration.py"]

# --- state machine: never regresses, terminal states never change --------------------------------

add(
    "wa-01-equal-rank-reapplies",
    STATE,
    "    return _RANK.get(incoming, -1) > _RANK.get(current, -1)",
    "    return _RANK.get(incoming, -1) >= _RANK.get(current, -1)",
    STATE_UNIT,
)
add(
    "wa-02-terminal-current-can-still-change",
    STATE,
    "    if current in _TERMINAL:\n        return False",
    "    if False:\n        return False",
    STATE_UNIT,
)
add(
    "wa-03-terminal-incoming-is-dropped",
    STATE,
    "    if incoming in _TERMINAL:\n        return True",
    "    if incoming in _TERMINAL:\n        return False",
    STATE_UNIT,
)

# --- customer-service window ----------------------------------------------------------------------

add(
    "wa-04-window-boundary-off-by-one",
    WINDOW,
    "    return _aware(conversation.window_expires_at) > now",
    "    return _aware(conversation.window_expires_at) >= now",
    WINDOW_UNIT,
)
add(
    "wa-05-no-window-expiry-fails-open",
    WINDOW,
    "    if conversation.window_expires_at is None:\n        return False\n    return _aware(conversation.window_expires_at) > now",
    "    if conversation.window_expires_at is None:\n        return True\n    return _aware(conversation.window_expires_at) > now",
    WINDOW_UNIT,
)

# --- send.py: the policy gate ---------------------------------------------------------------------

add(
    "wa-06-consent-check-inverted",
    SEND,
    "    if status != WhatsAppConsentStatus.OPTED_IN:\n        return MessagingErrorCode.OPTED_OUT",
    "    if status == WhatsAppConsentStatus.OPTED_IN:\n        return MessagingErrorCode.OPTED_OUT",
    SEND_UNIT,
)
add(
    "wa-07-suppression-check-removed",
    SEND,
    "    if consent_service.is_suppressed(db, organization_id=organization_id, contact_id=contact_id):\n        return MessagingErrorCode.POLICY_BLOCKED",
    "    if False:\n        return MessagingErrorCode.POLICY_BLOCKED",
    SEND_UNIT,
)
add(
    "wa-08-disconnected-connection-allowed",
    SEND,
    "    if connection.status != WhatsAppConnectionStatus.CONNECTED:\n        return MessagingErrorCode.UNCONFIGURED",
    "    if False:\n        return MessagingErrorCode.UNCONFIGURED",
    SEND_UNIT,
)
add(
    "wa-09-retryable-branch-inverted",
    SEND,
    "        if exc.retryable:",
    "        if not exc.retryable:",
    SEND_UNIT,
)
add(
    "wa-10-claim-ignores-current-status",
    SEND,
    "        .where(WhatsAppMessage.id == row.id, WhatsAppMessage.status == WhatsAppMessageStatus.QUEUED)",
    "        .where(WhatsAppMessage.id == row.id)",
    SEND_UNIT,
)
add(
    "wa-11-existing-lookup-disabled",
    SEND,
    "    return db.scalar(\n        select(WhatsAppMessage).where(\n            WhatsAppMessage.organization_id == organization_id,\n            WhatsAppMessage.idempotency_key == idempotency_key,\n        )\n    )",
    "    return None",
    SEND_UNIT,
)

# --- templates: only approved, variables must match exactly --------------------------------------

add(
    "wa-12-any-status-is-usable",
    TEMPLATES,
    "    return template.status.strip().upper() == APPROVED_STATUS",
    "    return True",
    TEMPLATES_UNIT,
)
add(
    "wa-13-variable-mismatch-not-checked",
    TEMPLATES,
    "    if set(variables) != set(required):",
    "    if False:",
    TEMPLATES_UNIT,
)
add(
    "wa-14-named-placeholder-guard-removed",
    TEMPLATES,
    "    if not all(n.isdigit() for n in names):",
    "    if False:",
    TEMPLATES_UNIT,
)

# --- phone normalization: ambiguous input must never be guessed ----------------------------------

add(
    "wa-15-ambiguous-number-guessed-instead-of-rejected",
    PHONE,
    "        if not default_country or not is_known_region(default_country):\n            raise InvalidPhoneNumber(",
    "        if False:\n            raise InvalidPhoneNumber(",
    PHONE_UNIT,
)

# --- webhooks: signature verification and duplicate-delivery dedupe ------------------------------

add(
    "wa-16-signature-check-disabled",
    WEBHOOKS,
    '    if not verify_meta_signature(body, headers.get("x-hub-signature-256"), secret):\n        raise WebhookRejected(401, "invalid signature")',
    '    if False:\n        raise WebhookRejected(401, "invalid signature")',
    WEBHOOKS_UNIT,
)
add(
    "wa-17-duplicate-ledger-insert-not-caught",
    WEBHOOKS,
    '    try:\n        db.commit()\n    except IntegrityError:\n        db.rollback()\n        return IntakeResult("duplicate")',
    "    db.commit()",
    WEBHOOKS_UNIT,
)
add(
    "wa-18-handshake-mode-not-checked",
    VALIDATION,
    '        mode == "subscribe"\n        and token is not None',
    "        token is not None",
    WEBHOOKS_UNIT,
)

# --- automation.py: validation and crash-recovery extensions -------------------------------------

add(
    "wa-19-consent-category-validation-inverted",
    AUTOMATION,
    '        ) not in {c.value for c in WhatsAppConsentCategory}:\n            raise InvalidWorkflow("WhatsApp consent category is invalid.")',
    '        ) in {c.value for c in WhatsAppConsentCategory}:\n            raise InvalidWorkflow("WhatsApp consent category is invalid.")',
    AUTOMATION_UNIT,
)
add(
    "wa-20-whatsapp-sends-excluded-from-crash-protection",
    AUTOMATION,
    'EXTERNAL_CALL_ACTION_TYPES = {\n    "ai_generate",\n    "send_whatsapp_text",\n    "send_whatsapp_template",\n    "send_email",\n    "send_email_template",\n}',
    'EXTERNAL_CALL_ACTION_TYPES = {\n    "ai_generate",\n}',
    AUTOMATION_UNIT,
)
