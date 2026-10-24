"""The Admin API version is explicit, centralized and checked against Shopify's support policy."""

import logging
import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.services.integrations.shopify import versioning
from app.services.integrations.shopify.factory import check_api_version

APP = Path(__file__).resolve().parents[2] / "app"


def test_the_default_version_is_defined_once_and_the_settings_use_it():
    assert versioning.CURRENT_API_VERSION == "2026-10"
    assert versioning.is_quarterly_version(versioning.CURRENT_API_VERSION)
    assert Settings.model_fields["shopify_api_version"].default == versioning.CURRENT_API_VERSION
    assert get_settings().shopify_api_version == versioning.CURRENT_API_VERSION


def test_no_application_code_hard_codes_an_api_version():
    """A version written anywhere but versioning.py would drift out of sync on the next upgrade."""
    pattern = re.compile(r"""["']20\d\d-(?:01|04|07|10)["']""")
    allowed = {APP / "services" / "integrations" / "shopify" / "versioning.py"}
    offenders = [
        f"{path.relative_to(APP)}:{number}"
        for path in sorted(APP.rglob("*.py"))
        if path not in allowed and "migrations" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("value", ["2026-10", "2027-01", "2026-04", "2026-07", " 2026-10 "])
def test_quarterly_versions_are_accepted(value):
    assert versioning.is_quarterly_version(value)


@pytest.mark.parametrize(
    "value",
    ["2026-11", "2026-13", "26-10", "2026-1", "2026-10-01", "latest", "unstable", "", "2026_10"],
)
def test_anything_else_is_rejected_by_the_configuration(value, monkeypatch):
    assert not versioning.is_quarterly_version(value)
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite://", jwt_secret_key="x" * 40, shopify_api_version=value)  # type: ignore[call-arg]


def test_release_dates_and_age():
    assert versioning.release_date("2026-10") == date(2026, 10, 1)
    assert versioning.age_in_months("2026-10", date(2026, 10, 22)) == 0
    assert versioning.age_in_months("2026-10", date(2027, 10, 1)) == 12
    with pytest.raises(ValueError):
        versioning.release_date("2026-11")


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 30), "unreleased"),  # a release candidate: may still change
        (date(2026, 10, 1), "current"),
        (date(2027, 6, 30), "current"),  # eight months in
        (date(2027, 7, 1), "ageing"),  # nine months: three months of guaranteed support left
        (date(2027, 9, 30), "ageing"),
        (date(2027, 10, 1), "unsupported"),  # twelve months: past the guaranteed window
        (date(2030, 1, 1), "unsupported"),
    ],
)
def test_support_status_follows_shopifys_twelve_month_window(today, expected):
    assert versioning.support_status("2026-10", today) == expected


def test_an_older_but_supported_version_is_not_called_current():
    assert versioning.support_status("2026-07", date(2026, 10, 22)) == "supported"


@pytest.mark.parametrize("today", [date(2027, 8, 1), date(2028, 1, 1), date(2026, 9, 1)])
def test_a_version_near_or_past_its_support_window_warns_at_startup(today, caplog):
    caplog.set_level(logging.WARNING)
    assert check_api_version("2026-10", today) != "current"
    assert "shopify api version 2026-10" in caplog.text


def test_a_healthy_version_is_silent(caplog):
    caplog.set_level(logging.WARNING)
    assert check_api_version("2026-10", date(2026, 11, 1)) == "current"
    assert caplog.text == ""
