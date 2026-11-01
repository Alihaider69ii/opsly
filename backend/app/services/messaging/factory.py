"""Builds the configured messaging provider from settings (mirrors app/services/ai/factory.py).

Production refuses to even start if WHATSAPP_MESSAGING_PROVIDER names a non-production provider
(enforced in ``app.core.config.Settings``, the startup gate); this is a second, defense-in-depth
check at the point of use.
"""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import MessagingProvider
from app.services.messaging.registry import default_registry


def _check_environment(provider_id: str, settings: Settings) -> None:
    spec = default_registry.spec(provider_id)
    if not spec.production_allowed and settings.environment.lower() == "production":
        raise MessagingError(
            MessagingErrorCode.UNCONFIGURED,
            f"messaging provider '{spec.id}' is not allowed in production",
        )


def build_messaging_provider(settings: Settings) -> MessagingProvider:
    _check_environment(settings.whatsapp_messaging_provider, settings)
    return default_registry.build(settings.whatsapp_messaging_provider)


@lru_cache
def _cached(provider_id: str) -> MessagingProvider:
    return default_registry.build(provider_id)


def get_messaging_provider() -> MessagingProvider:
    settings = get_settings()
    _check_environment(settings.whatsapp_messaging_provider, settings)
    return _cached(settings.whatsapp_messaging_provider)
