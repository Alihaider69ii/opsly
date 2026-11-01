"""Builds the configured email provider from settings (mirrors
``app/services/messaging/factory.py``).

Production refuses to even start if EMAIL_PROVIDER names a non-production provider (enforced in
``app.core.config.Settings``, the startup gate); this is a second, defense-in-depth check at the
point of use.
"""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.email.errors import EmailError, EmailErrorCode
from app.services.email.provider import EmailProvider
from app.services.email.registry import default_registry


def _check_environment(provider_id: str, settings: Settings) -> None:
    spec = default_registry.spec(provider_id)
    if not spec.production_allowed and settings.environment.lower() == "production":
        raise EmailError(
            EmailErrorCode.UNCONFIGURED, f"email provider '{spec.id}' is not allowed in production"
        )


def build_email_provider(settings: Settings) -> EmailProvider:
    _check_environment(settings.email_provider, settings)
    return default_registry.build(settings.email_provider)


@lru_cache
def _cached(provider_id: str) -> EmailProvider:
    return default_registry.build(provider_id)


def get_email_provider() -> EmailProvider:
    settings = get_settings()
    _check_environment(settings.email_provider, settings)
    return _cached(settings.email_provider)
