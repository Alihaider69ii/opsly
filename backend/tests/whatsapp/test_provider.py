"""Direct coverage of the Meta Graph API adapter (``providers/whatsapp_cloud.py`` and
``providers/_http.py``). Every other WhatsApp test exercises ``FakeMessagingProvider`` instead, so
without this file the actual wire-format and error-classification code that would talk to a real
Meta endpoint has zero direct test coverage.
"""

import httpx
import pytest

from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import SendRequest, TemplateComponent
from app.services.messaging.providers._http import classify_http_status
from app.services.messaging.providers.whatsapp_cloud import (
    WhatsAppCloudProvider,
    fetch_phone_info,
    fetch_templates,
)

NO_SLEEP = lambda seconds: None  # noqa: E731


def provider(handler, **kwargs) -> WhatsAppCloudProvider:
    kwargs.setdefault("sleep", NO_SLEEP)
    return WhatsAppCloudProvider(transport=httpx.MockTransport(handler), **kwargs)


def text_request(**overrides) -> SendRequest:
    defaults = dict(
        to_e164="+14155551234",
        kind="text",
        idempotency_key="k1",
        text="hello",
        phone_number_id="PNID",
        access_token="secret-token",
        api_version="v26.0",
    )
    defaults.update(overrides)
    return SendRequest(**defaults)


def ok_send_response(message_id: str = "wamid.TEST123") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": "+14155551234", "wa_id": "14155551234"}],
            "messages": [{"id": message_id}],
        },
    )


# --- request shape: exactly what Meta's docs specify -------------------------------------------


def test_text_send_payload_matches_the_documented_shape():
    seen = {}

    def handler(request: httpx.Request):
        import json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        seen["json"] = json.loads(request.content)
        return ok_send_response()

    result = provider(handler).send_text(text_request())
    assert seen["url"] == "https://graph.facebook.com/v26.0/PNID/messages"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["content_type"] == "application/json"
    assert seen["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+14155551234",
        "type": "text",
        "text": {"body": "hello", "preview_url": False},
    }
    assert result.provider_message_id == "wamid.TEST123"
    assert result.provider == "whatsapp_cloud"


def test_template_send_payload_uses_positional_body_parameters():
    seen = {}

    def handler(request: httpx.Request):
        import json

        seen["json"] = json.loads(request.content)
        return ok_send_response("wamid.TPL1")

    request = text_request(
        kind="template",
        text=None,
        template_name="order_shipped",
        template_language="en_US",
        template_components=(
            TemplateComponent(
                type="body",
                parameters=({"type": "text", "text": "Ali"}, {"type": "text", "text": "ORD-1"}),
            ),
        ),
    )
    result = provider(handler).send_template(request)
    assert seen["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+14155551234",
        "type": "template",
        "template": {
            "name": "order_shipped",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Ali"},
                        {"type": "text", "text": "ORD-1"},
                    ],
                }
            ],
        },
    }
    assert result.provider_message_id == "wamid.TPL1"


def test_template_send_without_variables_omits_the_components_key():
    seen = {}

    def handler(request: httpx.Request):
        import json

        seen["json"] = json.loads(request.content)
        return ok_send_response()

    request = text_request(
        kind="template", text=None, template_name="static_notice", template_language="en_US"
    )
    provider(handler).send_template(request)
    assert "components" not in seen["json"]["template"]


def test_a_malformed_success_body_is_an_unknown_provider_error():
    with pytest.raises(MessagingError) as exc:
        provider(lambda r: httpx.Response(200, json={"messaging_product": "whatsapp"})).send_text(
            text_request()
        )
    assert exc.value.code == MessagingErrorCode.UNKNOWN_PROVIDER_ERROR


def test_missing_credentials_never_makes_a_network_call():
    calls = []

    def handler(request):
        calls.append(1)
        return ok_send_response()

    with pytest.raises(MessagingError) as exc:
        provider(handler).send_text(text_request(access_token=""))
    assert exc.value.code == MessagingErrorCode.UNCONFIGURED
    assert calls == []


# --- error classification: Meta's own documented codes, not just HTTP status -------------------
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes (verified Phase 7)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, None, MessagingErrorCode.AUTHENTICATION_ERROR),
        (403, None, MessagingErrorCode.AUTHENTICATION_ERROR),
        (429, None, MessagingErrorCode.RATE_LIMITED),
        (500, None, MessagingErrorCode.PROVIDER_UNAVAILABLE),
        (503, None, MessagingErrorCode.PROVIDER_UNAVAILABLE),
        (400, None, MessagingErrorCode.INVALID_REQUEST),
        (
            400,
            {"error": {"code": 132001, "message": "template not approved"}},
            MessagingErrorCode.INVALID_REQUEST,
        ),
        # The important case: Meta reports throughput/rate-limit conditions as plain HTTP 400 --
        # status alone would (wrongly) classify these as permanent, non-retryable failures.
        (
            400,
            {"error": {"code": 130429, "message": "throughput reached"}},
            MessagingErrorCode.RATE_LIMITED,
        ),
        (
            400,
            {"error": {"code": 80007, "message": "WABA rate limit"}},
            MessagingErrorCode.RATE_LIMITED,
        ),
        (400, {"error": {"code": 4, "message": "app rate limit"}}, MessagingErrorCode.RATE_LIMITED),
        (
            400,
            {"error": {"code": 131056, "message": "too many to this recipient"}},
            MessagingErrorCode.RATE_LIMITED,
        ),
        # Meta's own "temporary issue" codes, also delivered as HTTP 400/500 variants.
        (
            400,
            {"error": {"code": 1, "message": "invalid request / possible server error"}},
            MessagingErrorCode.PROVIDER_UNAVAILABLE,
        ),
        (
            400,
            {"error": {"code": 2, "message": "temporary service issue"}},
            MessagingErrorCode.PROVIDER_UNAVAILABLE,
        ),
        # An expired/revoked token reported inside a 400 body (not just via 401/403).
        (
            400,
            {"error": {"code": 190, "message": "access token expired"}},
            MessagingErrorCode.AUTHENTICATION_ERROR,
        ),
        (
            400,
            {"error": {"code": 0, "message": "unable to authenticate"}},
            MessagingErrorCode.AUTHENTICATION_ERROR,
        ),
        # A window-expired rejection (131047) has no special mapping: it is permanent for this
        # send and must not be blindly retried as text, so it stays INVALID_REQUEST.
        (
            400,
            {"error": {"code": 131047, "message": "24 hours have passed"}},
            MessagingErrorCode.INVALID_REQUEST,
        ),
        # Malformed/missing body must not crash classification.
        (400, {}, MessagingErrorCode.INVALID_REQUEST),
        (400, {"error": "not even an object"}, MessagingErrorCode.INVALID_REQUEST),
    ],
)
def test_classify_http_status_uses_the_vendor_error_code_not_just_status(status, body, expected):
    assert classify_http_status(status, body) == expected


def test_a_rate_limit_error_inside_a_400_is_retried_not_failed_closed():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(400, json={"error": {"code": 130429, "message": "throughput"}})
        return ok_send_response()

    result = provider(handler, timeout=5.0).send_text(text_request())
    assert len(attempts) == 2  # retried once, then succeeded
    assert result.provider_message_id == "wamid.TEST123"


def test_a_permanent_validation_error_inside_a_400_is_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(400, json={"error": {"code": 132001, "message": "not approved"}})

    with pytest.raises(MessagingError) as exc:
        provider(handler).send_text(text_request())
    assert exc.value.code == MessagingErrorCode.INVALID_REQUEST
    assert len(attempts) == 1  # never retried


def test_retry_after_header_is_honored_and_capped():
    delays = []

    def handler(request):
        if len(delays) == 0:
            return httpx.Response(429, headers={"Retry-After": "9999"})
        return ok_send_response()

    sleeps = []
    result = provider(
        handler, timeout=5.0, sleep=lambda s: sleeps.append(s) or delays.append(1)
    ).send_text(text_request())
    assert result.provider_message_id == "wamid.TEST123"
    assert sleeps == [10.0]  # MAX_RETRY_AFTER_SECONDS cap, not the literal 9999


# --- phone info / template fetch: used only by the connect/sync flows ---------------------------


def test_fetch_phone_info_reads_display_fields():
    def handler(request: httpx.Request):
        assert request.url.path == "/v26.0/PNID"
        return httpx.Response(
            200,
            json={
                "display_phone_number": "+1 415-555-1234",
                "verified_name": "Acme Co",
                "quality_rating": "GREEN",
            },
        )

    info = fetch_phone_info(
        phone_number_id="PNID",
        access_token="tok",
        api_version="v26.0",
        transport=httpx.MockTransport(handler),
    )
    assert info.display_phone_number == "+1 415-555-1234"
    assert info.verified_name == "Acme Co"
    assert info.quality_rating == "GREEN"


def test_fetch_templates_follows_pagination_to_a_bounded_end():
    pages = [
        {
            "data": [
                {
                    "name": "t1",
                    "language": "en_US",
                    "category": "UTILITY",
                    "status": "APPROVED",
                    "id": "1",
                }
            ],
            "paging": {"cursors": {"after": "CURSOR1"}, "next": "https://x/next"},
        },
        {
            "data": [
                {
                    "name": "t2",
                    "language": "en_US",
                    "category": "MARKETING",
                    "status": "PENDING",
                    "id": "2",
                }
            ],
            "paging": {"cursors": {"after": "CURSOR2"}},
        },
    ]
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=pages[len(calls) - 1])

    templates = fetch_templates(
        waba_id="WABA1",
        access_token="tok",
        api_version="v26.0",
        transport=httpx.MockTransport(handler),
    )
    assert [t.name for t in templates] == ["t1", "t2"]
    assert len(calls) == 2
    assert "after" not in calls[0]
    assert calls[1]["after"] == "CURSOR1"
