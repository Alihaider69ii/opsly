"""The outbound email status state machine (mirrors
``app/services/messaging/whatsapp/state.py``).

API acceptance is not SENT; SENT is not DELIVERED. Status webhook events are applied through
``should_apply`` only, which enforces a strict rank order so an out-of-order or duplicate delivery
can never regress a message. BOUNCED/FAILED/COMPLAINT/UNSUBSCRIBED are terminal regardless of
rank.
"""

from app.db.models.email import EmailMessageStatus

# Only these map from a provider status webhook value. Anything else is unrecognized and the
# caller ignores it (informational only), never errors or guesses a mapping -- the provider may
# add new event types without warning.
_FROM_PROVIDER_STATUS = {
    "sent": EmailMessageStatus.SENT,
    "delivered": EmailMessageStatus.DELIVERED,
    "bounced": EmailMessageStatus.BOUNCED,
    "complained": EmailMessageStatus.COMPLAINT,
    "unsubscribed": EmailMessageStatus.UNSUBSCRIBED,
    # Resend reports a provider-level suppression (prior hard bounce/complaint recorded on its own
    # suppression list) as "suppressed", not "unsubscribed" -- functionally the same outcome for
    # us (stop sending to this address), so it is normalized the same way.
    "suppressed": EmailMessageStatus.UNSUBSCRIBED,
    "failed": EmailMessageStatus.FAILED,
}

_RANK = {
    EmailMessageStatus.QUEUED: 0,
    EmailMessageStatus.SENDING: 1,
    EmailMessageStatus.SENT: 2,
    EmailMessageStatus.DELIVERED: 3,
}
_TERMINAL = frozenset(
    {
        EmailMessageStatus.BOUNCED,
        EmailMessageStatus.FAILED,
        EmailMessageStatus.COMPLAINT,
        EmailMessageStatus.UNSUBSCRIBED,
    }
)


def provider_status_to_normalized(value: str) -> EmailMessageStatus | None:
    return _FROM_PROVIDER_STATUS.get(value.strip().lower())


def should_apply(current: EmailMessageStatus, incoming: EmailMessageStatus) -> bool:
    if current in _TERMINAL:
        return False
    if incoming in _TERMINAL:
        return True
    return _RANK.get(incoming, -1) > _RANK.get(current, -1)
