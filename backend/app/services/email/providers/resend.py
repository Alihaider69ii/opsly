"""Resend (https://resend.com) adapter: a single REST endpoint, Bearer-token authenticated. One
of several possible real email providers; chosen because its API is a single trivial POST with no
vendor SDK required (consistent with how ``whatsapp_cloud.py`` calls Meta's Graph API directly
over ``httpx``, never a vendor SDK).

Credentials are environment-based (``EMAIL_API_KEY``), never stored per organization -- see
``app/db/models/email.py:EmailConnection`` docstring.
"""

from app.core.config import Settings, get_settings
from app.services.email.errors import EmailError, EmailErrorCode
from app.services.email.provider import InboundEmailContent, SendRequest, SendResult
from app.services.email.providers._http import Endpoint

DEFAULT_BASE_URL = "https://api.resend.com"
MAX_REFERENCES = 50


class ResendProvider:
    name = "resend"

    def __init__(self, *, api_key: str, base_url: str, timeout: float, max_retries: int) -> None:
        self._endpoint = Endpoint(
            provider_id=self.name,
            api_key=api_key,
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            max_attempts=max(1, max_retries),
        )

    def send(self, request: SendRequest) -> SendResult:
        if not request.text_body and not request.html_body:
            raise EmailError(EmailErrorCode.INVALID_REQUEST, "an email needs a text or html body")
        payload: dict[str, object] = {
            "from": f"{request.from_name} <{request.from_address}>"
            if request.from_name
            else request.from_address,
            "to": [request.to],
            "subject": request.subject,
        }
        if request.text_body:
            payload["text"] = request.text_body
        if request.html_body:
            payload["html"] = request.html_body
        if request.reply_to:
            payload["reply_to"] = request.reply_to
        if request.cc:
            payload["cc"] = list(request.cc)
        if request.headers:
            payload["headers"] = dict(request.headers)
        response = self._endpoint.post("/emails", payload)
        from app.services.email.providers._http import safe_json

        body = safe_json(response)
        message_id = body.get("id") if body else None
        if not isinstance(message_id, str) or not message_id:
            raise EmailError(
                EmailErrorCode.UNKNOWN_PROVIDER_ERROR,
                "the email provider accepted the request but returned no message id",
                provider=self.name,
            )
        return SendResult(provider_message_id=message_id, provider=self.name)

    def fetch_inbound(self, *, provider_message_id: str) -> InboundEmailContent:
        """GET /emails/receiving/{id} -- the inbound webhook carries only metadata; this fetches
        the body and headers it omits (verified against Resend's "Get email content" docs, Oct
        2026: https://resend.com/docs/dashboard/receiving/get-email-content)."""
        from app.services.email.providers._http import safe_json

        response = self._endpoint.get(f"/emails/receiving/{provider_message_id}")
        body = safe_json(response)
        if body is None:
            raise EmailError(
                EmailErrorCode.UNKNOWN_PROVIDER_ERROR,
                "the email provider returned an unreadable response",
                provider=self.name,
            )
        headers = body.get("headers")
        header_map: dict[str, str] = {}
        if isinstance(headers, list):
            for item in headers:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    value = item.get("value")
                    if isinstance(value, str):
                        header_map[item["name"].lower()] = value
        references = body.get("references")
        references_header = (
            " ".join(r for r in references[:MAX_REFERENCES] if isinstance(r, str))
            if isinstance(references, list)
            else header_map.get("references")
        )
        raw_text, raw_html = body.get("text"), body.get("html")
        raw_message_id, raw_in_reply_to = body.get("message_id"), body.get("in_reply_to")
        return InboundEmailContent(
            text_body=raw_text if isinstance(raw_text, str) else "",
            html_body=raw_html if isinstance(raw_html, str) else "",
            message_id_header=(
                raw_message_id if isinstance(raw_message_id, str) else header_map.get("message-id")
            ),
            in_reply_to_header=(
                raw_in_reply_to
                if isinstance(raw_in_reply_to, str)
                else header_map.get("in-reply-to")
            ),
            references_header=references_header,
        )

    @classmethod
    def from_config(cls, settings: Settings | None = None) -> "ResendProvider":
        settings = settings or get_settings()
        api_key = settings.email_api_key.get_secret_value() if settings.email_api_key else ""
        if not api_key:
            raise EmailError(EmailErrorCode.UNCONFIGURED, "EMAIL_API_KEY is not set")
        return cls(
            api_key=api_key,
            base_url=settings.email_base_url,
            timeout=settings.email_request_timeout_seconds,
            max_retries=settings.email_max_retries,
        )
