"""The email provider registry: provider id -> adapter (mirrors
``app/services/messaging/registry.py``). This is the only code that turns a provider id into an
implementation; ``send.py`` depends on ``EmailProvider`` and never on a vendor.
"""

from collections.abc import Callable

from app.services.email.catalog import BUILTIN_EMAIL_PROVIDERS, EmailProviderSpec
from app.services.email.errors import EmailError, EmailErrorCode
from app.services.email.provider import EmailProvider
from app.services.email.providers.fake import FakeEmailProvider
from app.services.email.providers.resend import ResendProvider

ProviderFactory = Callable[[], EmailProvider]


class EmailProviderRegistry:
    def __init__(self, specs: tuple[EmailProviderSpec, ...] = BUILTIN_EMAIL_PROVIDERS) -> None:
        self._specs: dict[str, EmailProviderSpec] = {spec.id: spec for spec in specs}
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider_id: str, factory: ProviderFactory) -> None:
        self._factories[provider_id] = factory

    def spec(self, provider_id: str) -> EmailProviderSpec:
        spec = self._specs.get(provider_id)
        if spec is None:
            known = ", ".join(sorted(self._specs))
            raise EmailError(
                EmailErrorCode.UNCONFIGURED,
                f"unknown email provider '{provider_id}'; known: {known}",
            )
        return spec

    def build(self, provider_id: str) -> EmailProvider:
        self.spec(provider_id)
        factory = self._factories.get(provider_id)
        if factory is None:
            raise EmailError(
                EmailErrorCode.UNCONFIGURED, f"no adapter registered for '{provider_id}'"
            )
        return factory()


def _builtin_registry() -> EmailProviderRegistry:
    registry = EmailProviderRegistry()
    registry.register("resend", ResendProvider.from_config)
    registry.register("fake", FakeEmailProvider.from_config)
    return registry


default_registry = _builtin_registry()
