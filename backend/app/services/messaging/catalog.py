"""The messaging provider catalog: which providers BizFlow knows, as plain data (mirrors
``app/services/ai/catalog.py``). Adding a channel BizFlow does not yet support (SMS, email,
Instagram, ...) is one new adapter class plus one row here; nothing that calls
``MessagingProvider`` changes.
"""

import enum
from dataclasses import dataclass


class Channel(enum.StrEnum):
    WHATSAPP = "whatsapp"


@dataclass(frozen=True)
class MessagingProviderSpec:
    id: str
    label: str
    channel: Channel
    adapter: str
    production_allowed: bool = True


BUILTIN_MESSAGING_PROVIDERS: tuple[MessagingProviderSpec, ...] = (
    MessagingProviderSpec(
        id="whatsapp_cloud",
        label="Meta WhatsApp Cloud API",
        channel=Channel.WHATSAPP,
        adapter="whatsapp_cloud",
    ),
    MessagingProviderSpec(
        id="fake",
        label="Fake (offline/test only)",
        channel=Channel.WHATSAPP,
        adapter="fake",
        production_allowed=False,
    ),
)


def non_production_provider_ids() -> frozenset[str]:
    return frozenset(p.id for p in BUILTIN_MESSAGING_PROVIDERS if not p.production_allowed)
