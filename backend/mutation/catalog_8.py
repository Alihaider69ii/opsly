"""Mutants for Phase 8: the AI auto-reply policy order (eligibility gates, human-request
detection, max-replies cap, grounding/confidence acceptance, fallback-counter bookkeeping), the
deterministic confidence classifier and greeting/human-request detectors, the WhatsApp-prompt
leak guard (validate_reply), the human-handoff state machine's race-safe transitions, and the
per-window AI-reply-counter reset.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


AUTOREPLY = "app/services/messaging/whatsapp/autoreply.py"
AGENT = "app/services/ai/whatsapp_agent.py"
HANDOFF = "app/services/messaging/whatsapp/handoff.py"
WINDOW = "app/services/messaging/whatsapp/window.py"
WEBHOOKS = "app/services/messaging/whatsapp/webhooks.py"

AUTOREPLY_UNIT = ["tests/whatsapp/test_autoreply.py"]
AGENT_UNIT = ["tests/ai/test_whatsapp_agent.py"]
INJECTION_UNIT = ["tests/ai/test_whatsapp_prompt_injection.py"]
HANDOFF_UNIT = ["tests/whatsapp/test_handoff.py"]
WINDOW_UNIT = ["tests/whatsapp/test_window.py"]
WEBHOOKS_UNIT = ["tests/whatsapp/test_webhooks.py"]

# --- autoreply.py: the eligibility gate (policy order) --------------------------------------------

add(
    "wa8-01-auto-reply-toggle-bypassed",
    AUTOREPLY,
    '    if not connection.ai_auto_reply_enabled:\n        return "disabled"',
    '    if False:\n        return "disabled"',
    AUTOREPLY_UNIT,
)
add(
    "wa8-02-suppression-check-bypassed",
    AUTOREPLY,
    '    if consent_service.is_suppressed(db, organization_id=organization_id, contact_id=contact.id):\n        return "suppressed"',
    '    if False:\n        return "suppressed"',
    AUTOREPLY_UNIT,
)
add(
    "wa8-03-support-consent-check-inverted",
    AUTOREPLY,
    '        != WhatsAppConsentStatus.OPTED_IN\n    ):\n        return "opted_out"',
    '        == WhatsAppConsentStatus.OPTED_IN\n    ):\n        return "opted_out"',
    AUTOREPLY_UNIT,
)
add(
    "wa8-04-window-check-bypassed",
    AUTOREPLY,
    '    if not window_service.is_window_open(conversation, now=now):\n        return "window_closed"',
    '    if False:\n        return "window_closed"',
    AUTOREPLY_UNIT,
)
add(
    "wa8-05-handoff-active-check-bypassed",
    AUTOREPLY,
    '    if not handoff_service.is_ai_active(conversation):\n        return f"handoff_active:{conversation.ai_state.value.lower()}"',
    '    if False:\n        return f"handoff_active:{conversation.ai_state.value.lower()}"',
    AUTOREPLY_UNIT,
)

# --- autoreply.py: explicit human request --------------------------------------------------------

add(
    "wa8-06-human-request-detection-skipped",
    AUTOREPLY,
    "    if requests_human(text):",
    "    if False and requests_human(text):",
    AUTOREPLY_UNIT,
)

# --- autoreply.py: max replies per window --------------------------------------------------------

add(
    "wa8-07-max-replies-boundary-off-by-one",
    AUTOREPLY,
    "        and conversation.ai_replies_in_window >= connection.ai_max_replies_per_window",
    "        and conversation.ai_replies_in_window > connection.ai_max_replies_per_window",
    AUTOREPLY_UNIT,
)
add(
    "wa8-08-max-replies-handoff-disabled-check-removed",
    AUTOREPLY,
    '        if not connection.ai_handoff_enabled:\n            return "max_replies_reached"',
    '        if False:\n            return "max_replies_reached"',
    AUTOREPLY_UNIT,
)

# --- autoreply.py: grounding / confidence acceptance ----------------------------------------------

add(
    "wa8-09-confidence-threshold-ignored",
    AUTOREPLY,
    "    confidence_ok = reply.is_greeting or meets_minimum(\n        reply.confidence, connection.ai_min_confidence\n    )",
    "    confidence_ok = True",
    AUTOREPLY_UNIT,
)
add(
    "wa8-10-grounding-required-setting-ignored",
    AUTOREPLY,
    "    acceptable = confidence_ok and (grounding_ok or not connection.ai_grounding_required)",
    "    acceptable = confidence_ok and (grounding_ok or connection.ai_grounding_required)",
    AUTOREPLY_UNIT,
)
add(
    "wa8-11-fallback-counter-not-reset-on-success",
    AUTOREPLY,
    "    if acceptable and reply.content:\n        conversation.ai_consecutive_fallbacks = 0",
    "    if acceptable and reply.content:\n        conversation.ai_consecutive_fallbacks = 1",
    AUTOREPLY_UNIT,
)

# --- whatsapp_agent.py: deterministic confidence classifier ---------------------------------------

add(
    "wa8-12-confidence-high-margin-threshold-weakened",
    AGENT,
    "    if margin >= 0.35 and len(sources) >= 2:\n        return WhatsAppAIConfidence.HIGH",
    "    if margin >= 0.05 and len(sources) >= 2:\n        return WhatsAppAIConfidence.HIGH",
    AGENT_UNIT,
)
add(
    "wa8-13-confidence-high-source-count-weakened",
    AGENT,
    "    if margin >= 0.35 and len(sources) >= 2:\n        return WhatsAppAIConfidence.HIGH",
    "    if margin >= 0.35 and len(sources) >= 1:\n        return WhatsAppAIConfidence.HIGH",
    AGENT_UNIT,
)
add(
    "wa8-14-confidence-medium-threshold-weakened",
    AGENT,
    "    if margin >= 0.12:\n        return WhatsAppAIConfidence.MEDIUM",
    "    if margin >= 0.0:\n        return WhatsAppAIConfidence.MEDIUM",
    AGENT_UNIT,
)

# --- whatsapp_agent.py: the grounding-required gate (the hard "no LLM call without retrieved
# knowledge" guarantee, and the organization's explicit opt-out of it) ----------------------------

add(
    "wa8-15-grounding-gate-always-skips-the-llm",
    AGENT,
    "        if not retrieval.found and not greeting and grounding_required:",
    "        if not retrieval.found and not greeting and False:",
    AGENT_UNIT,
)
add(
    "wa8-16-grounding-required-parameter-ignored",
    AGENT,
    "        if not retrieval.found and not greeting and grounding_required:",
    "        if not retrieval.found and not greeting:",
    AGENT_UNIT,
)
add(
    "wa8-17-greeting-detection-disabled-in-respond",
    AGENT,
    "        greeting = not retrieval.found and is_greeting(question)",
    "        greeting = False",
    AGENT_UNIT,
)

# --- whatsapp_agent.py: validate_reply (the prompt/instruction leak guard) -----------------------

add(
    "wa8-18-canary-leak-check-removed",
    AGENT,
    '    if (\n        prompts.PROMPT_CANARY in lowered\n        or f"<{prompts.KNOWLEDGE_TAG}" in lowered\n        or f"<{prompts.CUSTOMER_CONTEXT_TAG}" in lowered\n    ):',
    '    if (\n        False\n        or f"<{prompts.KNOWLEDGE_TAG}" in lowered\n        or f"<{prompts.CUSTOMER_CONTEXT_TAG}" in lowered\n    ):',
    AGENT_UNIT + INJECTION_UNIT,
)
add(
    "wa8-19-customer-context-tag-leak-check-removed",
    AGENT,
    '    if (\n        prompts.PROMPT_CANARY in lowered\n        or f"<{prompts.KNOWLEDGE_TAG}" in lowered\n        or f"<{prompts.CUSTOMER_CONTEXT_TAG}" in lowered\n    ):',
    '    if (\n        prompts.PROMPT_CANARY in lowered\n        or f"<{prompts.KNOWLEDGE_TAG}" in lowered\n        or False\n    ):',
    AGENT_UNIT + INJECTION_UNIT,
)

# --- whatsapp_agent.py: the deterministic detectors themselves ------------------------------------

add(
    "wa8-20-requests-human-detector-disabled",
    AGENT,
    "def requests_human(text: str) -> bool:\n"
    '    """A deterministic detector supporting English and Hindi/Hinglish patterns."""\n'
    "    return bool(_HUMAN_REQUEST_RE.search(text))",
    "def requests_human(text: str) -> bool:\n"
    '    """A deterministic detector supporting English and Hindi/Hinglish patterns."""\n'
    "    return False",
    AGENT_UNIT + AUTOREPLY_UNIT,
)
add(
    "wa8-21-is-greeting-detector-disabled",
    AGENT,
    "def is_greeting(text: str) -> bool:\n    return bool(_GREETING_RE.match(text))",
    "def is_greeting(text: str) -> bool:\n    return False",
    AGENT_UNIT,
)
add(
    "wa8-27-hindi-human-request-disabled",
    AGENT,
    r'    r"\b(baat\s*(karni|karna|karwao|karvaao|karein|karado|kara\s*do|karo))\b|"',
    r'    r"(?!)|"',
    AGENT_UNIT + AUTOREPLY_UNIT,
)
add(
    "wa8-28-hindi-greeting-disabled",
    AGENT,
    r'    r"namaste|namaskar|pranam|pranaam|salaam|salam|"',
    r'    r"(?!)|"',
    AGENT_UNIT,
)

# --- handoff.py: the race-safe state transitions --------------------------------------------------

add(
    "wa8-22-handoff-claim-not-scoped-to-ai-active",
    HANDOFF,
    "            WhatsAppConversation.ai_state == WhatsAppAIState.AI_ACTIVE,",
    "            WhatsAppConversation.id == conversation.id,",
    HANDOFF_UNIT,
)
add(
    "wa8-23-handoff-rowcount-check-inverted",
    HANDOFF,
    "    if result.rowcount != 1:  # type: ignore[attr-defined]\n        return False\n    db.refresh(conversation)\n    logger.info(\n        \"whatsapp handoff triggered",
    "    if result.rowcount == 1:  # type: ignore[attr-defined]\n        return False\n    db.refresh(conversation)\n    logger.info(\n        \"whatsapp handoff triggered",
    HANDOFF_UNIT,
)
add(
    "wa8-24-claim-state-check-bypassed",
    HANDOFF,
    "    if conversation.ai_state != WhatsAppAIState.HUMAN_REQUIRED:\n        return False",
    "    if False:\n        return False",
    HANDOFF_UNIT,
)
add(
    "wa8-25-resolve-already-active-check-bypassed",
    HANDOFF,
    "    if conversation.ai_state == WhatsAppAIState.AI_ACTIVE:\n        return False",
    "    if False:\n        return False",
    HANDOFF_UNIT,
)

# --- window.py: the per-window AI-reply counter reset ----------------------------------------------

add(
    "wa8-26-ai-reply-counter-reset-condition-inverted",
    WINDOW,
    "    if not is_window_open(conversation, now=now):\n        conversation.ai_replies_in_window = 0",
    "    if is_window_open(conversation, now=now):\n        conversation.ai_replies_in_window = 0",
    WINDOW_UNIT,
)

# --- webhooks.py & autoreply.py: worker crash recovery --------------------------------------------

add(
    "wa8-29-crash-recovery-inbound-retry-dropped",
    WEBHOOKS,
    "        message = existing_inbound\n        existing_conv = db.get(WhatsAppConversation, message.conversation_id)",
    '        _finish(db, event.id, WebhookEventStatus.PROCESSED)\n        return "processed"',
    WEBHOOKS_UNIT,
)
add(
    "wa8-30-crash-recovery-queued-dispatch-disabled",
    AUTOREPLY,
    "        if existing_outbound.status == WhatsAppMessageStatus.QUEUED:",
    "        if False and existing_outbound.status == WhatsAppMessageStatus.QUEUED:",
    AUTOREPLY_UNIT + WEBHOOKS_UNIT,
)
