"""The messaging provider registry: provider id -> adapter (mirrors
``app/services/ai/registry.py``). This is the only code that turns a provider id into an
implementation; the WhatsApp send service and the automation engine depend on
``MessagingProvider`` and never on a vendor.

Unlike the AI registry, no per-request base URL/model resolution happens here: WhatsApp
credentials are per-organization (decrypted from the stored connection), not a single
process-wide setting, so they travel on each ``SendRequest`` instead (see ``provider.py``).
"""

from collections.abc import Callable

from app.services.messaging.catalog import BUILTIN_MESSAGING_PROVIDERS, MessagingProviderSpec
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import MessagingProvider
from app.services.messaging.providers.fake import FakeMessagingProvider
from app.services.messaging.providers.whatsapp_cloud import WhatsAppCloudProvider

ProviderFactory = Callable[[], MessagingProvider]


class MessagingProviderRegistry:
    def __init__(
        self, specs: tuple[MessagingProviderSpec, ...] = BUILTIN_MESSAGING_PROVIDERS
    ) -> None:
        self._specs: dict[str, MessagingProviderSpec] = {spec.id: spec for spec in specs}
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider_id: str, factory: ProviderFactory) -> None:
        self._factories[provider_id] = factory

    def spec(self, provider_id: str) -> MessagingProviderSpec:
        spec = self._specs.get(provider_id)
        if spec is None:
            known = ", ".join(sorted(self._specs))
            raise MessagingError(
                MessagingErrorCode.UNCONFIGURED,
                f"unknown messaging provider '{provider_id}'; known: {known}",
            )
        return spec

    def build(self, provider_id: str) -> MessagingProvider:
        self.spec(provider_id)
        factory = self._factories.get(provider_id)
        if factory is None:
            raise MessagingError(
                MessagingErrorCode.UNCONFIGURED, f"no adapter registered for '{provider_id}'"
            )
        return factory()


def _builtin_registry() -> MessagingProviderRegistry:
    registry = MessagingProviderRegistry()
    registry.register("whatsapp_cloud", WhatsAppCloudProvider.from_config)
    registry.register("fake", FakeMessagingProvider.from_config)
    return registry


default_registry = _builtin_registry()
