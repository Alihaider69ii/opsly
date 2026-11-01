"""The email provider catalog: which providers BizFlow knows, as plain data (mirrors
``app/services/messaging/catalog.py``). Adding a provider (Postmark, SES, SendGrid, ...) is one
new adapter class plus one row here; nothing that calls ``EmailProvider`` changes.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailProviderSpec:
    id: str
    label: str
    adapter: str
    production_allowed: bool = True


BUILTIN_EMAIL_PROVIDERS: tuple[EmailProviderSpec, ...] = (
    EmailProviderSpec(id="resend", label="Resend", adapter="resend"),
    EmailProviderSpec(
        id="fake", label="Fake (offline/test only)", adapter="fake", production_allowed=False
    ),
)


def non_production_provider_ids() -> frozenset[str]:
    return frozenset(p.id for p in BUILTIN_EMAIL_PROVIDERS if not p.production_allowed)
