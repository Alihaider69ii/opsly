"""The outbound message status state machine.

API acceptance is not SENT; SENT is not DELIVERED; DELIVERED is not READ. Status webhook events
are applied through ``apply_status`` only, which enforces a strict rank order so an out-of-order
or duplicate delivery can never regress a message: once READ, a later DELIVERED (or SENT) event is
a no-op, not a downgrade. FAILED and CANCELLED are terminal regardless of rank.
"""

from app.db.models.whatsapp import WhatsAppMessageStatus

# Only these map from a Meta status webhook value. Anything else -- including "played" (a real,
# current Meta status for voice messages, verified in Phase 7:
# https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/status
# -- is treated as unrecognized by ``provider_status_to_normalized`` below and the caller ignores
# it (informational only), never errors or guesses a mapping. Meta has changed and added status
# values before and may again; failing open to "ignored" rather than "reject the whole webhook" is
# the deliberately safe default.
_FROM_PROVIDER_STATUS = {
    "sent": WhatsAppMessageStatus.SENT,
    "delivered": WhatsAppMessageStatus.DELIVERED,
    "read": WhatsAppMessageStatus.READ,
    "failed": WhatsAppMessageStatus.FAILED,
}

_RANK = {
    WhatsAppMessageStatus.QUEUED: 0,
    WhatsAppMessageStatus.SENDING: 1,
    WhatsAppMessageStatus.SENT: 2,
    WhatsAppMessageStatus.DELIVERED: 3,
    WhatsAppMessageStatus.READ: 4,
}
_TERMINAL = frozenset({WhatsAppMessageStatus.FAILED, WhatsAppMessageStatus.CANCELLED})


def provider_status_to_normalized(value: str) -> WhatsAppMessageStatus | None:
    """A Meta ``statuses[].status`` value, normalized; ``None`` for anything unrecognized (the
    caller must treat that as ignored, never guess a mapping)."""
    return _FROM_PROVIDER_STATUS.get(value.strip().lower())


def should_apply(current: WhatsAppMessageStatus, incoming: WhatsAppMessageStatus) -> bool:
    """Whether a status transition may be written. Terminal states never change. A non-terminal
    rank only ever moves forward; an equal or lower rank (a duplicate or out-of-order delivery) is
    a no-op, not an error."""
    if current in _TERMINAL:
        return False
    if incoming in _TERMINAL:
        return True
    return _RANK.get(incoming, -1) > _RANK.get(current, -1)
