import pytest

from app.db.models import WhatsAppMessageStatus
from app.services.messaging.whatsapp.state import provider_status_to_normalized, should_apply


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sent", WhatsAppMessageStatus.SENT),
        ("delivered", WhatsAppMessageStatus.DELIVERED),
        ("read", WhatsAppMessageStatus.READ),
        ("failed", WhatsAppMessageStatus.FAILED),
        ("SENT", WhatsAppMessageStatus.SENT),  # case-insensitive
        (" sent ", WhatsAppMessageStatus.SENT),  # whitespace-tolerant
    ],
)
def test_known_provider_statuses_normalize(raw, expected):
    assert provider_status_to_normalized(raw) == expected


@pytest.mark.parametrize("raw", ["", "unknown_future_status", "deleted", "warning"])
def test_unrecognized_provider_status_is_none_not_guessed(raw):
    assert provider_status_to_normalized(raw) is None


@pytest.mark.parametrize(
    "current,incoming,expected",
    [
        (WhatsAppMessageStatus.QUEUED, WhatsAppMessageStatus.SENDING, True),
        (WhatsAppMessageStatus.SENDING, WhatsAppMessageStatus.SENT, True),
        (WhatsAppMessageStatus.SENT, WhatsAppMessageStatus.DELIVERED, True),
        (WhatsAppMessageStatus.DELIVERED, WhatsAppMessageStatus.READ, True),
        # Out-of-order / duplicate: never regresses, never re-applies the same rank.
        (WhatsAppMessageStatus.READ, WhatsAppMessageStatus.DELIVERED, False),
        (WhatsAppMessageStatus.READ, WhatsAppMessageStatus.SENT, False),
        (WhatsAppMessageStatus.DELIVERED, WhatsAppMessageStatus.SENT, False),
        (WhatsAppMessageStatus.SENT, WhatsAppMessageStatus.SENT, False),
        (WhatsAppMessageStatus.DELIVERED, WhatsAppMessageStatus.DELIVERED, False),
        # Terminal states never change, in any direction.
        (WhatsAppMessageStatus.FAILED, WhatsAppMessageStatus.SENT, False),
        (WhatsAppMessageStatus.FAILED, WhatsAppMessageStatus.DELIVERED, False),
        (WhatsAppMessageStatus.CANCELLED, WhatsAppMessageStatus.READ, False),
        # A failure reported after a success is still accepted (FAILED is terminal and wins).
        (WhatsAppMessageStatus.DELIVERED, WhatsAppMessageStatus.FAILED, True),
        (WhatsAppMessageStatus.READ, WhatsAppMessageStatus.FAILED, True),
    ],
)
def test_rank_ordering_never_regresses_and_terminal_states_never_change(
    current, incoming, expected
):
    assert should_apply(current, incoming) is expected
