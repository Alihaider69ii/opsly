"""The Shopify Admin API version BizFlow targets: the one place the string is written.

Shopify (verified against https://shopify.dev/docs/api/usage/versioning, October 2026):

* a new stable version is released every three months, on the first day of January, April, July
  and October at 5 pm UTC (``2026-10`` was released on 1 October 2026);
* each stable version is supported for a minimum of 12 months, with at least nine months of overlap
  between consecutive versions;
* a request for a version that is no longer served is "fallen forward": Shopify answers it with the
  oldest version it still supports and says so in the ``X-Shopify-API-Version`` response header.
  Nothing fails loudly, so behaviour can change underneath an app that never looks at that header.

Upgrade strategy (see docs/shopify.md): move ``CURRENT_API_VERSION`` forward deliberately, at most
once a quarter and always before the configured version is nine months old; run the test suite and
the live-check tool against a development store first; ``SHOPIFY_API_VERSION`` can override it per
deployment so an upgrade can be staged. The client compares the version Shopify reports having used
with the configured one and logs a warning when they differ; startup and the worker log a warning
when the configured version is within three months of the end of its minimum support window.

This module imports nothing from the application so configuration can read it.
"""

import re
from datetime import date

# The Admin API version used when SHOPIFY_API_VERSION is not set. Change it here and nowhere else.
CURRENT_API_VERSION = "2026-10"

SUPPORT_MONTHS = 12  # Shopify's documented minimum support per stable version
WARN_AFTER_MONTHS = 9  # warn when no more than three months of guaranteed support remain

_QUARTERLY = re.compile(r"^(20\d\d)-(01|04|07|10)$")


def is_quarterly_version(value: str) -> bool:
    return bool(_QUARTERLY.match(value.strip()))


def release_date(version: str) -> date:
    """First day of the quarter the version is named after."""
    match = _QUARTERLY.match(version.strip())
    if match is None:
        raise ValueError(f"not a quarterly Shopify API version: {version!r}")
    return date(int(match.group(1)), int(match.group(2)), 1)


def age_in_months(version: str, today: date) -> int:
    released = release_date(version)
    return (today.year - released.year) * 12 + (today.month - released.month)


def support_status(version: str, today: date) -> str:
    """``"current"`` (newest we know), ``"supported"``, ``"ageing"`` (support window nearly over),
    ``"unsupported"`` (past the guaranteed window: Shopify may already fall forward) or
    ``"unreleased"`` (a release candidate: it may change before release)."""
    months = age_in_months(version, today)
    if months < 0:
        return "unreleased"
    if months >= SUPPORT_MONTHS:
        return "unsupported"
    if months >= WARN_AFTER_MONTHS:
        return "ageing"
    return "current" if version == CURRENT_API_VERSION else "supported"
