"""Canonical phone number normalization, backed by ``phonenumbers`` (the Python port of Google's
libphonenumber). A single E.164 form is the only thing ever matched, queried or deduplicated on —
never a raw string with its country code, leading '+', spaces or dashes handled ad hoc.

Ambiguous (non-international) input is resolved only against an organization's explicit default
country (``WhatsAppConnection.default_country``); a number this cannot confidently normalize is
rejected, never guessed, per the Phase 6 brief.
"""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


class InvalidPhoneNumber(ValueError):
    pass


def is_known_region(code: str) -> bool:
    """Whether ``code`` (expected ISO 3166-1 alpha-2, e.g. "US") is a region phonenumbers knows."""
    return bool(code) and phonenumbers.country_code_for_region(code.upper()) != 0


def normalize_phone(raw: str, *, default_country: str | None = None) -> str:
    """The canonical E.164 form (e.g. "+14155551234").

    A leading '+' is parsed as an explicit international number regardless of
    ``default_country``. Without one, ``default_country`` is required and must be a region
    phonenumbers recognizes; there is no guessing between candidate countries.
    """
    if not isinstance(raw, str):
        raise InvalidPhoneNumber("phone number must be a string")
    candidate = raw.strip()
    if not candidate:
        raise InvalidPhoneNumber("phone number is empty")
    region: str | None = None
    if not candidate.startswith("+"):
        if not default_country or not is_known_region(default_country):
            raise InvalidPhoneNumber(
                "ambiguous phone number: no leading '+' and no default country is configured "
                "for this organization"
            )
        region = default_country.upper()
    try:
        parsed = phonenumbers.parse(candidate, region)
    except NumberParseException as exc:
        raise InvalidPhoneNumber(f"could not parse phone number (error {exc.error_type})") from None
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumber("phone number is not a valid number for its region")
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def wa_id_from_e164(e164: str) -> str:
    """WhatsApp's ``wa_id`` form, as seen in webhook payloads: E.164 digits without the leading
    '+'. Used only to recognize an inbound contact; ``phone_e164`` (with the '+') is what BizFlow
    stores and matches on."""
    return e164.lstrip("+")
