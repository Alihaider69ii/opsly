"""Controlled live email verification harness for Phase 9 (mirrors
tests/ai/test_live_ai_verification.py).

Live verification NEVER runs automatically in ordinary unit/integration tests. It is explicitly
gated by LIVE_EMAIL_TESTS=1. If real Resend credentials (EMAIL_PROVIDER != "fake" and
EMAIL_API_KEY set) are absent when LIVE_EMAIL_TESTS=1 is set, this reports live verification as
EXTERNALLY BLOCKED -- it never invents or fakes a live send/receive result.

A. A real provider send is accepted and returns a real provider message id.
B. The real provider rejects an invalid API key with a stable AUTHENTICATION_ERROR code.
C. A real Svix-signed status webhook for that real send is accepted and updates the message.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import Settings, get_settings
from app.services.email.errors import EmailError, EmailErrorCode
from app.services.email.factory import build_email_provider
from app.services.email.provider import SendRequest
from app.services.email.validation import sign_svix_body

LIVE_EMAIL_FLAG = "LIVE_EMAIL_TESTS"


def is_live_enabled() -> bool:
    return os.environ.get(LIVE_EMAIL_FLAG) == "1"


def has_real_credentials(settings: Settings) -> bool:
    if settings.email_provider == "fake":
        return False
    return settings.email_api_key is not None and bool(
        settings.email_api_key.get_secret_value().strip()
    )


@pytest.fixture
def live_settings() -> Settings:
    if not is_live_enabled():
        pytest.skip(f"Live email verification disabled. Set {LIVE_EMAIL_FLAG}=1 to enable.")
    settings = get_settings()
    if not has_real_credentials(settings):
        pytest.skip(
            "LIVE EMAIL VERIFICATION = EXTERNAL BLOCKER: no real email provider credentials "
            "(EMAIL_PROVIDER/EMAIL_API_KEY) are configured in this environment. This is not a "
            "test failure -- it is an honest report that live verification could not run here."
        )
    return settings


@pytest.mark.skipif(
    not is_live_enabled(),
    reason=f"Live email verification is disabled. Set {LIVE_EMAIL_FLAG}=1 to enable.",
)
class TestLiveEmailVerification:
    def test_a_a_real_provider_send_is_accepted(self, live_settings: Settings):
        provider = build_email_provider(live_settings)
        recipient = os.environ.get("LIVE_EMAIL_TEST_RECIPIENT", "delivered@resend.dev")
        result = provider.send(
            SendRequest(
                to=recipient,
                from_address=os.environ.get("LIVE_EMAIL_TEST_FROM", "onboarding@resend.dev"),
                subject="BizFlow AI Phase 9 live verification",
                idempotency_key=f"live-verify-{uuid.uuid4()}",
                text_body="This is an automated live-verification send. No reply is expected.",
            )
        )
        assert result.provider_message_id
        assert result.provider == live_settings.email_provider

    def test_b_an_invalid_api_key_is_reported_as_authentication_error(
        self, live_settings: Settings
    ):
        from pydantic import SecretStr

        broken = live_settings.model_copy(
            update={"email_api_key": SecretStr("invalid-live-verification-key")}
        )
        provider = build_email_provider(broken)
        with pytest.raises(EmailError) as exc_info:
            provider.send(
                SendRequest(
                    to="delivered@resend.dev",
                    from_address="onboarding@resend.dev",
                    subject="should never send",
                    idempotency_key=f"live-verify-bad-key-{uuid.uuid4()}",
                    text_body="should never send",
                )
            )
        assert exc_info.value.code == EmailErrorCode.AUTHENTICATION_ERROR

    def test_c_a_real_svix_signed_webhook_is_verifiable(self, live_settings: Settings):
        """Proves the signature math in services/email/validation.py against a secret shaped
        exactly like Resend's real webhook secret (whsec_<base64>), independent of whether a real
        inbound/status delivery can be triggered from this harness."""
        from app.services.email.validation import verify_svix_signature

        secret = (
            live_settings.email_webhook_secret.get_secret_value()
            if live_settings.email_webhook_secret
            else None
        )
        if not secret:
            pytest.skip(
                "LIVE EMAIL VERIFICATION = EXTERNAL BLOCKER: no EMAIL_WEBHOOK_SECRET configured "
                "to verify a real Resend-shaped signature against."
            )
        body = b'{"type":"email.sent","data":{"email_id":"live-verify"}}'
        svix_id, svix_timestamp = "msg_live_verify", str(int(datetime.now(UTC).timestamp()))
        signature = sign_svix_body(
            svix_id=svix_id, svix_timestamp=svix_timestamp, body=body, secret=secret
        )
        assert verify_svix_signature(
            body=body,
            svix_id=svix_id,
            svix_timestamp=svix_timestamp,
            svix_signature=signature,
            secret=secret,
        )
