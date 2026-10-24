"""Shopify dashboard API schemas.

Responses are built from explicit field lists. Nothing that is a secret (access or refresh tokens,
the client secret, ciphertext) or personal data (customer names, emails, phones) has a field here,
so it cannot be serialized by accident. Request bodies forbid unknown fields (no mass assignment).
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.db.models import (
    ShopifyConnectionStatus,
    SyncResource,
    SyncStatus,
    WebhookRegistration,
)


class ShopifyConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated and normalized by the service (strictly *.myshopify.com), not by a URL type.
    shop: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class AuthorizationOut(BaseModel):
    authorization_url: str


class SyncStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resource: SyncResource
    status: SyncStatus
    items_synced: int
    items_skipped: int
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None


class CountsOut(BaseModel):
    products: int
    variants: int
    customers: int
    orders: int


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    shop_domain: str
    shop_name: str | None
    currency: str | None
    timezone: str | None
    api_version: str
    status: ShopifyConnectionStatus
    installed_at: datetime
    last_sync_at: datetime | None
    webhook_status: WebhookRegistration
    error_code: str | None
    error_message: str | None


class ShopifyStatusOut(BaseModel):
    # Whether this server has Shopify app credentials / can encrypt tokens / can register webhooks.
    available: bool
    secure_storage: bool
    webhooks_configured: bool
    connection: ConnectionOut | None
    scopes: list[str]
    sync: list[SyncStateOut]
    counts: CountsOut


class CallbackOut(BaseModel):
    status: str
    shop_domain: str
