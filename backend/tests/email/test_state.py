from app.db.models import EmailMessageStatus
from app.services.email import state as state_service


def test_provider_status_string_maps_to_normalized_enum():
    assert state_service.provider_status_to_normalized("delivered") == EmailMessageStatus.DELIVERED
    assert state_service.provider_status_to_normalized("  Bounced ") == EmailMessageStatus.BOUNCED


def test_provider_level_suppression_normalizes_to_unsubscribed():
    """Resend reports its own suppression-list hit as "suppressed", not "unsubscribed" -- both
    mean the same thing to us: stop sending to this address."""
    assert state_service.provider_status_to_normalized("suppressed") == EmailMessageStatus.UNSUBSCRIBED


def test_unrecognized_provider_status_is_ignored_not_guessed():
    assert state_service.provider_status_to_normalized("some_new_event_type") is None


def test_forward_rank_progression_is_applied():
    assert state_service.should_apply(EmailMessageStatus.SENT, EmailMessageStatus.DELIVERED) is True
    assert state_service.should_apply(EmailMessageStatus.QUEUED, EmailMessageStatus.SENT) is True


def test_out_of_order_or_duplicate_status_never_regresses_rank():
    assert state_service.should_apply(EmailMessageStatus.DELIVERED, EmailMessageStatus.SENT) is False
    assert state_service.should_apply(EmailMessageStatus.SENT, EmailMessageStatus.SENT) is False


def test_terminal_status_is_sticky_even_against_a_later_delivered_event():
    assert state_service.should_apply(EmailMessageStatus.BOUNCED, EmailMessageStatus.DELIVERED) is False


def test_a_terminal_incoming_status_always_applies_regardless_of_current_rank():
    assert state_service.should_apply(EmailMessageStatus.SENT, EmailMessageStatus.BOUNCED) is True
    assert state_service.should_apply(EmailMessageStatus.QUEUED, EmailMessageStatus.COMPLAINT) is True
