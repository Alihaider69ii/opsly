"""Deterministic offline/test messaging provider. Makes no network call of any kind (same
precedent as ``app.services.ai.providers.fake.FakeLLMProvider``) and never claims real delivery:
callers (tests, E2E) must simulate delivery/read by posting a signed status webhook, exactly as a
real Meta delivery would arrive.

Refused in production: see ``non_production_provider_ids`` in ``messaging/catalog.py``.
"""

import hashlib

from app.services.messaging.provider import SendRequest, SendResult


def synthetic_wamid(idempotency_key: str) -> str:
    """A stable, fake-looking message id derived only from the idempotency key, so the same send
    retried (same key) is recognizably the same message, and tests can predict it to simulate a
    matching status webhook."""
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"wamid.FAKE{digest}"


class FakeMessagingProvider:
    name = "fake"

    def send_text(self, request: SendRequest) -> SendResult:
        return SendResult(
            provider_message_id=synthetic_wamid(request.idempotency_key), provider=self.name
        )

    def send_template(self, request: SendRequest) -> SendResult:
        return SendResult(
            provider_message_id=synthetic_wamid(request.idempotency_key), provider=self.name
        )

    def mark_read(
        self,
        *,
        provider_message_id: str,
        phone_number_id: str,
        access_token: str,
        api_version: str = "",
        base_url: str = "",
    ) -> None:
        return None

    @classmethod
    def from_config(cls) -> "FakeMessagingProvider":
        return cls()
