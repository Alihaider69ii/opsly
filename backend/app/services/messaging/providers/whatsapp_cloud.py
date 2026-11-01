"""Meta WhatsApp Cloud API adapter: the only module that knows a Meta HTTP endpoint shape. The
automation engine and the WhatsApp send service never see it directly; they depend on
``MessagingProvider``.

Re-verified against current official Meta documentation in Phase 7 (see docs/whatsapp.md
"Verified against Meta's official documentation" for the full sourced diff):

* Sending: ``POST https://graph.facebook.com/<version>/<phone_number_id>/messages``, bearer token,
  JSON body ``{"messaging_product":"whatsapp","recipient_type":"individual","to":...,"type":...}``.
  https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
* Success response is acceptance only, not delivery:
  ``{"messaging_product":"whatsapp","contacts":[{"input":...,"wa_id":...}],
  "messages":[{"id":"wamid....","message_status":"accepted"|...}]}``. This module reads only
  ``id``; a successful-but-``held_for_quality_assessment``/``paused`` ``message_status`` is a
  known, deliberately deferred gap (see docs/whatsapp.md "Known Limitations") -- it still means
  "accepted", so recording SENT for it is not wrong, just not maximally informative.
* Templates: name/language/category/status/components, fetched from the Business Management API.
  https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates
* Error classification (``providers/_http.py``) now reads the vendor's ``error.code`` from the
  response body, not just the HTTP status: Meta returns most business-logic errors -- a
  transient throughput error exactly like a permanent validation error -- as plain HTTP 400, so
  the status alone cannot distinguish a retryable condition from a permanent one.
  https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes

NOT independently verified in this session (best-effort, documented elsewhere by Meta but not
re-checked here): the exact ``quality_rating`` field shape on a phone number, the mark-as-read
request shape, and the ``message_templates`` list pagination cursor shape. Each is implemented
defensively (missing/differently-shaped fields degrade to ``None`` rather than crash).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import SendRequest, SendResult
from app.services.messaging.providers._http import Endpoint

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://graph.facebook.com"
MAX_TEMPLATE_PAGES = 20


@dataclass(frozen=True)
class PhoneInfo:
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None


@dataclass(frozen=True)
class RemoteTemplate:
    name: str
    language: str
    category: str
    status: str
    provider_template_id: str | None
    components: list[dict] = field(default_factory=list)


def _endpoint(
    *,
    access_token: str,
    base_url: str,
    timeout: float,
    transport: httpx.BaseTransport | None,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Endpoint:
    return Endpoint(
        provider_id="whatsapp_cloud",
        access_token=access_token,
        base_url=base_url or DEFAULT_BASE_URL,
        timeout=timeout,
        transport=transport,
        max_attempts=max_attempts,
        sleep=sleep,
    )


def _require_credentials(request: SendRequest) -> None:
    if not request.phone_number_id or not request.access_token or not request.api_version:
        raise MessagingError(MessagingErrorCode.UNCONFIGURED, provider="whatsapp_cloud")


class WhatsAppCloudProvider:
    name = "whatsapp_cloud"

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep

    def _send(self, request: SendRequest, payload: dict) -> SendResult:
        _require_credentials(request)
        endpoint = _endpoint(
            access_token=request.access_token,
            base_url=request.base_url,
            timeout=self._timeout,
            sleep=self._sleep,
            transport=self._transport,
        )
        try:
            response = endpoint.post(
                f"/{request.api_version}/{request.phone_number_id}/messages", payload
            )
        finally:
            endpoint.close()
        try:
            body = response.json()
            message_id = body["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise MessagingError(
                MessagingErrorCode.UNKNOWN_PROVIDER_ERROR, provider=self.name
            ) from None
        if not isinstance(message_id, str) or not message_id:
            raise MessagingError(MessagingErrorCode.UNKNOWN_PROVIDER_ERROR, provider=self.name)
        return SendResult(provider_message_id=message_id, provider=self.name)

    def send_text(self, request: SendRequest) -> SendResult:
        if not request.text:
            raise MessagingError(MessagingErrorCode.INVALID_REQUEST, provider=self.name)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": request.to_e164,
            "type": "text",
            "text": {"body": request.text, "preview_url": False},
        }
        return self._send(request, payload)

    def send_template(self, request: SendRequest) -> SendResult:
        if not request.template_name or not request.template_language:
            raise MessagingError(MessagingErrorCode.INVALID_REQUEST, provider=self.name)
        components = [
            {"type": c.type, "parameters": list(c.parameters)} for c in request.template_components
        ]
        template: dict = {
            "name": request.template_name,
            "language": {"code": request.template_language},
        }
        if components:
            template["components"] = components
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": request.to_e164,
            "type": "template",
            "template": template,
        }
        return self._send(request, payload)

    def mark_read(
        self,
        *,
        provider_message_id: str,
        phone_number_id: str,
        access_token: str,
        api_version: str,
        base_url: str = "",
    ) -> None:
        if not phone_number_id or not access_token or not api_version:
            raise MessagingError(MessagingErrorCode.UNCONFIGURED, provider=self.name)
        endpoint = _endpoint(
            access_token=access_token,
            base_url=base_url,
            timeout=self._timeout,
            transport=self._transport,
            max_attempts=1,  # best-effort: never worth retrying or blocking a workflow on
        )
        try:
            endpoint.post(
                f"/{api_version}/{phone_number_id}/messages",
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": provider_message_id,
                },
            )
        finally:
            endpoint.close()

    @classmethod
    def from_config(cls) -> "WhatsAppCloudProvider":
        return cls()


def fetch_phone_info(
    *,
    phone_number_id: str,
    access_token: str,
    api_version: str,
    base_url: str = "",
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> PhoneInfo:
    """Confirm a phone_number_id/access_token pair is usable and read its display metadata. Used
    only by the connect flow, never by workflow execution."""
    endpoint = _endpoint(
        access_token=access_token,
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        max_attempts=2,
    )
    try:
        response = endpoint.get(
            f"/{api_version}/{phone_number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating"},
        )
    finally:
        endpoint.close()
    try:
        body = response.json()
    except ValueError:
        raise MessagingError(
            MessagingErrorCode.UNKNOWN_PROVIDER_ERROR, provider="whatsapp_cloud"
        ) from None
    if not isinstance(body, dict):
        raise MessagingError(MessagingErrorCode.UNKNOWN_PROVIDER_ERROR, provider="whatsapp_cloud")
    quality = body.get("quality_rating")
    return PhoneInfo(
        display_phone_number=body.get("display_phone_number"),
        verified_name=body.get("verified_name"),
        quality_rating=quality if isinstance(quality, str) else None,
    )


def fetch_templates(
    *,
    waba_id: str,
    access_token: str,
    api_version: str,
    base_url: str = "",
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> list[RemoteTemplate]:
    """All templates for a WhatsApp Business Account, following pagination up to a bounded number
    of pages (a runaway or malformed cursor can never cause an unbounded loop)."""
    templates: list[RemoteTemplate] = []
    after: str | None = None
    for _ in range(MAX_TEMPLATE_PAGES):
        endpoint = _endpoint(
            access_token=access_token,
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            max_attempts=2,
        )
        params = {"fields": "name,language,category,status,id,components", "limit": "100"}
        if after:
            params["after"] = after
        try:
            response = endpoint.get(f"/{api_version}/{waba_id}/message_templates", params=params)
        finally:
            endpoint.close()
        try:
            body = response.json()
        except ValueError:
            raise MessagingError(
                MessagingErrorCode.UNKNOWN_PROVIDER_ERROR, provider="whatsapp_cloud"
            ) from None
        if not isinstance(body, dict):
            raise MessagingError(
                MessagingErrorCode.UNKNOWN_PROVIDER_ERROR, provider="whatsapp_cloud"
            )
        for item in body.get("data") or []:
            if not isinstance(item, dict) or not item.get("name") or not item.get("language"):
                continue
            components = item.get("components")
            templates.append(
                RemoteTemplate(
                    name=str(item["name"]),
                    language=str(item["language"]),
                    category=str(item.get("category") or ""),
                    status=str(item.get("status") or ""),
                    provider_template_id=str(item["id"]) if item.get("id") else None,
                    components=components if isinstance(components, list) else [],
                )
            )
        next_after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
        has_next = bool((body.get("paging") or {}).get("next"))
        if not has_next or not isinstance(next_after, str) or next_after == after:
            break
        after = next_after
    return templates
