"""Deterministic offline/test email provider. Makes no network call of any kind (same precedent
as ``app.services.messaging.providers.fake.FakeMessagingProvider``) and never claims real
delivery: callers (tests, E2E) must simulate delivery/bounce by posting a signed status webhook,
exactly as a real provider delivery would arrive.

Refused in production: see ``non_production_provider_ids`` in ``email/catalog.py``.
"""

import hashlib
import json
from typing import cast

import redis

from app.core.config import get_settings
from app.services.email.errors import EmailError, EmailErrorCode
from app.services.email.provider import InboundEmailContent, SendRequest, SendResult

# Test/E2E-controlled: a test or the E2E suite simulating an inbound webhook registers the full
# content here first (there is no real provider to fetch it from), keyed by the id the simulated
# webhook announces. Redis-backed, not an in-process dict: the API process that might register a
# fixture and the worker process that calls ``fetch_inbound`` are different OS processes (the E2E
# stack runs them separately, exactly like production), so an in-process dict would never be
# visible across that boundary. Never used in production (this provider is refused there, see
# catalog.py).
_PREFIX = "bizflow:fake-email-inbound:"
_TTL_SECONDS = 3600


def _client() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
        decode_responses=True,
    )


def synthetic_message_id(idempotency_key: str) -> str:
    """A stable, fake-looking provider message id derived only from the idempotency key, so the
    same send retried (same key) is recognizably the same message, and tests can predict it to
    simulate a matching status webhook."""
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"fake-email-{digest}"


def register_fake_inbound(provider_message_id: str, content: InboundEmailContent) -> None:
    payload = json.dumps(
        {
            "text_body": content.text_body,
            "html_body": content.html_body,
            "message_id_header": content.message_id_header,
            "in_reply_to_header": content.in_reply_to_header,
            "references_header": content.references_header,
        }
    )
    _client().set(f"{_PREFIX}{provider_message_id}", payload, ex=_TTL_SECONDS)


def reset_fake_inbound() -> None:
    client = _client()
    for key in client.scan_iter(f"{_PREFIX}*"):
        client.delete(key)


class FakeEmailProvider:
    name = "fake"

    def send(self, request: SendRequest) -> SendResult:
        return SendResult(
            provider_message_id=synthetic_message_id(request.idempotency_key), provider=self.name
        )

    def fetch_inbound(self, *, provider_message_id: str) -> InboundEmailContent:
        try:
            raw = cast(str | None, _client().get(f"{_PREFIX}{provider_message_id}"))
        except redis.RedisError as exc:
            raise EmailError(
                EmailErrorCode.PROVIDER_UNAVAILABLE,
                "the fake provider's shared inbound registry is unreachable",
                provider=self.name,
            ) from exc
        if raw is None:
            raise EmailError(
                EmailErrorCode.UNKNOWN_PROVIDER_ERROR,
                "no fake inbound content registered for this id",
                provider=self.name,
            )
        data = json.loads(raw)
        return InboundEmailContent(**data)

    @classmethod
    def from_config(cls) -> "FakeEmailProvider":
        return cls()
