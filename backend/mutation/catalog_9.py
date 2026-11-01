"""Mutants for Phase 9: unified support (human-priority override, assignment race-safety,
authorization, tenant-scoped identity matching), email send policy (suppression, marketing
consent, the one-click unsubscribe gate), the email status rank-check, webhook signature
verification and dedup, campaign idempotency, AI/human sender attribution, and the automation
engine's send_email(_template) action validation.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


CONVERSATIONS = "app/services/support/conversations.py"
IDENTITY = "app/services/support/identity.py"
EMAIL_SEND = "app/services/email/send.py"
EMAIL_CONSENT = "app/services/email/consent.py"
EMAIL_STATE = "app/services/email/state.py"
EMAIL_VALIDATION = "app/services/email/validation.py"
EMAIL_WEBHOOKS = "app/services/email/webhooks.py"
EMAIL_CAMPAIGNS = "app/services/email/campaigns.py"
EMAIL_UNSUBSCRIBE = "app/services/email/unsubscribe.py"
EMAIL_AUTOREPLY = "app/services/email/autoreply.py"
AUTOMATION = "app/services/automation.py"

WHATSAPP_HANDOFF = "app/services/messaging/whatsapp/handoff.py"

CONVERSATIONS_UNIT = ["tests/support/test_conversations.py"]
CHANNEL_DISPATCH_UNIT = ["tests/support/test_conversations_channel_dispatch.py"]
IDENTITY_UNIT = ["tests/support/test_identity.py"]
SEND_UNIT = ["tests/email/test_send.py"]
CONSENT_UNIT = ["tests/email/test_consent.py", "tests/email/test_send.py"]
STATE_UNIT = ["tests/email/test_state.py", "tests/email/test_webhooks.py"]
VALIDATION_UNIT = ["tests/email/test_validation.py"]
WEBHOOKS_UNIT = ["tests/email/test_webhooks.py"]
CAMPAIGNS_UNIT = ["tests/email/test_campaigns.py"]
UNSUBSCRIBE_UNIT = ["tests/email/test_unsubscribe.py", "tests/email/test_unsubscribe_api.py"]
AUTOREPLY_UNIT = ["tests/email/test_autoreply.py"]
WEBHOOKS_AUTOREPLY_UNIT = ["tests/email/test_webhooks.py", "tests/email/test_autoreply.py"]
AUTOMATION_UNIT = ["tests/email/test_automation_integration.py"]

# --- support/conversations.py: human-priority override is never silently lost -------------------

add(
    "p9-01-human-priority-override-rule-bypassed",
    CONVERSATIONS,
    "    if conversation.priority_source == PrioritySource.HUMAN and source != PrioritySource.HUMAN:\n        return False",
    "    if False:\n        return False",
    CONVERSATIONS_UNIT,
)
add(
    "p9-02-human-priority-override-operator-inverted",
    CONVERSATIONS,
    "    if conversation.priority_source == PrioritySource.HUMAN and source != PrioritySource.HUMAN:\n        return False",
    "    if conversation.priority_source == PrioritySource.HUMAN and source == PrioritySource.HUMAN:\n        return False",
    CONVERSATIONS_UNIT,
)

# --- support/conversations.py: authorization + assignment race-safety ----------------------------

add(
    "p9-03-require-member-check-bypassed",
    CONVERSATIONS,
    "    if member is None:\n        raise NotOrganizationMember",
    "    if False:\n        raise NotOrganizationMember",
    CONVERSATIONS_UNIT,
)
add(
    "p9-04-claim-not-scoped-to-unassigned",
    CONVERSATIONS,
    "            SupportConversation.assigned_agent_user_id.is_(None),\n        )\n        .values(assigned_agent_user_id=agent_user_id, assigned_at=now, assigned_by=agent_user_id)",
    "            SupportConversation.id == conversation.id,\n        )\n        .values(assigned_agent_user_id=agent_user_id, assigned_at=now, assigned_by=agent_user_id)",
    CONVERSATIONS_UNIT,
)
add(
    "p9-05-claim-rowcount-check-inverted",
    CONVERSATIONS,
    "    if result.rowcount != 1:  # type: ignore[attr-defined]\n        return False",
    "    if result.rowcount == 1:  # type: ignore[attr-defined]\n        return False",
    CONVERSATIONS_UNIT,
)

# --- messaging/whatsapp/handoff.py: take_over (new Phase 9 capability, mirrors claim's race-
# safety but is reachable from any non-HUMAN_ACTIVE state) -----------------------------------------

add(
    "p9-28-whatsapp-take-over-rowcount-check-inverted",
    WHATSAPP_HANDOFF,
    "    if result.rowcount != 1:  # type: ignore[attr-defined]\n        return False\n    db.refresh(conversation)\n    logger.info(\n        \"whatsapp human take-over",
    "    if result.rowcount == 1:  # type: ignore[attr-defined]\n        return False\n    db.refresh(conversation)\n    logger.info(\n        \"whatsapp human take-over",
    CHANNEL_DISPATCH_UNIT,
)
add(
    "p9-29-whatsapp-take-over-not-scoped-to-eligible-states",
    WHATSAPP_HANDOFF,
    "            WhatsAppConversation.ai_state.in_(\n                [WhatsAppAIState.AI_ACTIVE, WhatsAppAIState.HUMAN_REQUIRED]\n            ),",
    "            WhatsAppConversation.id == conversation.id,",
    CHANNEL_DISPATCH_UNIT,
)

# --- support/identity.py: tenant-scoped matching ---------------------------------------------------

add(
    "p9-06-identity-email-match-not-scoped-to-tenant",
    IDENTITY,
    "        found = db.scalar(\n            select(Customer).where(\n                Customer.organization_id == organization_id,\n                Customer.normalized_email == norm_email,\n            )\n        )",
    "        found = db.scalar(\n            select(Customer).where(\n                Customer.normalized_email == norm_email,\n            )\n        )",
    IDENTITY_UNIT,
)

# --- email/send.py: the policy gate (fixed order; every check runs before any provider call) ------

add(
    "p9-07-email-suppression-check-bypassed",
    EMAIL_SEND,
    "    if consent_service.is_suppressed(customer):\n        return EmailErrorCode.SUPPRESSED",
    "    if False:\n        return EmailErrorCode.SUPPRESSED",
    SEND_UNIT,
)
add(
    "p9-08-marketing-consent-check-inverted",
    EMAIL_SEND,
    "    if (\n        classification == EmailClassification.MARKETING\n        and customer.email_marketing_consent is not True\n    ):\n        return EmailErrorCode.OPTED_OUT",
    "    if (\n        classification == EmailClassification.MARKETING\n        and customer.email_marketing_consent is True\n    ):\n        return EmailErrorCode.OPTED_OUT",
    SEND_UNIT,
)
add(
    "p9-09-unsubscribe-link-required-check-bypassed",
    EMAIL_SEND,
    "    if classification == EmailClassification.MARKETING and not _unsubscribe_link_configured():\n        return EmailErrorCode.POLICY_BLOCKED",
    "    if False:\n        return EmailErrorCode.POLICY_BLOCKED",
    SEND_UNIT,
)
add(
    "p9-10-unsubscribe-footer-applied-to-every-classification",
    EMAIL_SEND,
    "    if outbound.classification != EmailClassification.MARKETING:\n        return outbound.text_body, outbound.html_body",
    "    if False:\n        return outbound.text_body, outbound.html_body",
    SEND_UNIT,
)

# --- email/consent.py: suppression is a universal kill switch ------------------------------------

add(
    "p9-11-is-suppressed-always-false",
    EMAIL_CONSENT,
    "def is_suppressed(customer: Customer) -> bool:\n    return customer.email_suppressed",
    "def is_suppressed(customer: Customer) -> bool:\n    return False",
    CONSENT_UNIT,
)
add(
    "p9-12-suppress-never-actually-sets-the-flag",
    EMAIL_CONSENT,
    "def suppress(customer: Customer, *, reason: str, now: datetime) -> None:\n    customer.email_suppressed = True",
    "def suppress(customer: Customer, *, reason: str, now: datetime) -> None:\n    customer.email_suppressed = False",
    ["tests/email/test_consent.py"],
)

# --- email/state.py: the rank-checked status transition guard ------------------------------------

add(
    "p9-13-terminal-status-no-longer-sticky",
    EMAIL_STATE,
    "    if current in _TERMINAL:\n        return False",
    "    if False:\n        return False",
    STATE_UNIT,
)
add(
    "p9-14-terminal-incoming-no-longer-always-applies",
    EMAIL_STATE,
    "    if incoming in _TERMINAL:\n        return True",
    "    if False:\n        return True",
    STATE_UNIT,
)
add(
    "p9-15-rank-comparison-allows-same-rank-replay",
    EMAIL_STATE,
    "    return _RANK.get(incoming, -1) > _RANK.get(current, -1)",
    "    return _RANK.get(incoming, -1) >= _RANK.get(current, -1)",
    STATE_UNIT,
)

# --- email/validation.py: svix webhook signature verification ------------------------------------

add(
    "p9-16-svix-signature-comparison-always-true",
    EMAIL_VALIDATION,
    "        if value and hmac.compare_digest(value, expected):",
    "        if value:",
    VALIDATION_UNIT,
)
add(
    "p9-17-svix-timestamp-skew-check-bypassed",
    EMAIL_VALIDATION,
    "    if abs((now if now is not None else time.time()) - timestamp) > MAX_TIMESTAMP_SKEW_SECONDS:\n        return False",
    "    if False:\n        return False",
    VALIDATION_UNIT,
)

# --- email/webhooks.py: duplicate inbound/status delivery dedup + sender attribution --------------

add(
    "p9-18-webhook-dedup-reports-duplicate-as-accepted",
    EMAIL_WEBHOOKS,
    '    except IntegrityError:\n        db.rollback()\n        return IntakeResult("duplicate")',
    '    except IntegrityError:\n        db.rollback()\n        return IntakeResult("accepted")',
    WEBHOOKS_UNIT,
)
add(
    "p9-19-inbound-message-sender-type-misattributed",
    EMAIL_WEBHOOKS,
    "        sender_type=SenderType.CUSTOMER,\n    )",
    "        sender_type=SenderType.AI,\n    )",
    WEBHOOKS_UNIT,
)

# --- email/campaigns.py: idempotency (duplicate queue_campaign call, concurrent batch claim) ------

add(
    "p9-20-queue-campaign-duplicate-guard-removed",
    EMAIL_CAMPAIGNS,
    "        if customer.id in existing:\n            continue",
    "        if False:\n            continue",
    CAMPAIGNS_UNIT,
)
add(
    "p9-21-claim-batch-reclaims-already-queued-recipients",
    EMAIL_CAMPAIGNS,
    "                EmailCampaignRecipient.campaign_id == campaign_id,\n                EmailCampaignRecipient.status == EmailCampaignRecipientStatus.PENDING,",
    "                EmailCampaignRecipient.campaign_id == campaign_id,\n                EmailCampaignRecipient.status != EmailCampaignRecipientStatus.SENT,",
    CAMPAIGNS_UNIT,
)

# --- email/unsubscribe.py: the one-click unsubscribe token itself must be unforgeable -------------

add(
    "p9-22-unsubscribe-token-signature-check-always-passes",
    EMAIL_UNSUBSCRIBE,
    "    if not hmac.compare_digest(signature, expected):\n        return None",
    "    if False:\n        return None",
    UNSUBSCRIBE_UNIT,
)

# --- email/autoreply.py: AI vs. system/handoff sender attribution --------------------------------

add(
    "p9-23-autoreply-sender-type-always-ai",
    EMAIL_AUTOREPLY,
    '        sender_type=SenderType.AI if sent_label == "sent" else SenderType.SYSTEM,',
    "        sender_type=SenderType.AI,",
    AUTOREPLY_UNIT,
)

# --- automation.py: send_email / send_email_template action validation (automation eligibility) --

add(
    "p9-24-email-subject-length-ceiling-weakened",
    AUTOMATION,
    '            if not isinstance(subject, str) or not 1 <= len(subject) <= 200:\n                raise InvalidWorkflow("Email subject is invalid.")',
    '            if not isinstance(subject, str) or not 1 <= len(subject) <= 2000:\n                raise InvalidWorkflow("Email subject is invalid.")',
    AUTOMATION_UNIT,
)
add(
    "p9-25-email-content-empty-string-allowed",
    AUTOMATION,
    '            if not isinstance(content, str) or not 1 <= len(content) <= 5000:\n                raise InvalidWorkflow("Email content is invalid.")',
    '            if not isinstance(content, str) or not 0 <= len(content) <= 5000:\n                raise InvalidWorkflow("Email content is invalid.")',
    AUTOMATION_UNIT,
)
add(
    "p9-26-email-classification-validity-check-bypassed",
    AUTOMATION,
    '            if action.get("classification") not in {c.value for c in EmailClassification}:\n                raise InvalidWorkflow("Email classification is invalid.")',
    "            if False:\n                raise InvalidWorkflow(\"Email classification is invalid.\")",
    AUTOMATION_UNIT,
)
add(
    "p9-27-email-template-variables-bound-weakened",
    AUTOMATION,
    '            if not isinstance(variables, dict) or len(variables) > 20:\n                raise InvalidWorkflow("Email template variables must be a bounded object.")',
    '            if not isinstance(variables, dict) or len(variables) > 200:\n                raise InvalidWorkflow("Email template variables must be a bounded object.")',
    AUTOMATION_UNIT,
)
