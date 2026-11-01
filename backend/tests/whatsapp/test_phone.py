import pytest

from app.services.messaging.phone import (
    InvalidPhoneNumber,
    is_known_region,
    normalize_phone,
    wa_id_from_e164,
)


def test_e164_with_plus_normalizes_regardless_of_formatting():
    assert normalize_phone("+1 (415) 555-1234") == "+14155551234"
    assert normalize_phone("+14155551234") == "+14155551234"


def test_local_number_resolves_with_an_explicit_default_country():
    assert normalize_phone("4155551234", default_country="US") == "+14155551234"
    assert normalize_phone("4155551234", default_country="us") == "+14155551234"


def test_ambiguous_number_without_default_country_is_rejected_not_guessed():
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone("4155551234")


def test_unknown_default_country_is_rejected():
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone("4155551234", default_country="ZZ")


def test_garbage_input_is_rejected():
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone("not-a-phone-number", default_country="US")
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone("")
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone("+1")  # too short to be a valid number


def test_non_string_input_is_rejected():
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(123456789)  # type: ignore[arg-type]


def test_duplicate_contacts_from_formatting_differences_collapse_to_one_canonical_form():
    variants = ["+14155551234", "+1 415 555 1234", "+1-415-555-1234", "+1 (415) 555-1234"]
    assert len({normalize_phone(v) for v in variants}) == 1


def test_wa_id_strips_the_leading_plus():
    assert wa_id_from_e164("+14155551234") == "14155551234"


def test_is_known_region():
    assert is_known_region("US")
    assert is_known_region("us")
    assert not is_known_region("ZZ")
    assert not is_known_region("")
