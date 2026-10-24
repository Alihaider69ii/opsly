"""Shopify errors. ``code`` is a stable machine-readable reason; messages are safe to log (they
never carry tokens, response bodies or customer data). What merchants see comes only from
``USER_MESSAGES``, a fixed catalogue keyed by code, never from exception text."""


class ShopifyError(Exception):
    code = "shopify_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message or self.code)


class InvalidShopDomain(ShopifyError):
    code = "invalid_shop"


class ShopifyAuthError(ShopifyError):
    """Shopify rejected the access token (revoked, expired beyond refresh, app uninstalled)."""

    code = "auth_failed"


class ShopifyAccessDenied(ShopifyError):
    """The app is not permitted to read this data (missing scope or protected customer data)."""

    code = "access_denied"


class ShopifyThrottled(ShopifyError):
    """Rate limited and out of retries."""

    code = "throttled"


class ShopifyUnavailable(ShopifyError):
    """Shopify (or the network) failed and retries are exhausted."""

    code = "unavailable"


class ShopifyRequestError(ShopifyError):
    """A permanent failure: the request itself was rejected. Never retried."""

    code = "request_failed"


class OAuthError(ShopifyError):
    """The OAuth flow failed. ``code`` says how: not_configured, invalid_state, state_expired,
    invalid_signature, shop_mismatch, scope_missing, exchange_failed, already_connected."""

    code = "oauth_failed"


USER_MESSAGES: dict[str, str] = {
    "invalid_shop": "Enter your store address, like your-store.myshopify.com.",
    "not_configured": "Shopify is not configured on this server.",
    "secrets_not_configured": "Secure storage is not configured on this server, so a store cannot "
    "be connected yet.",
    "invalid_state": "The Shopify authorization could not be verified. Please start again.",
    "state_expired": "The Shopify authorization took too long. Please start again.",
    "invalid_signature": "The Shopify authorization could not be verified. Please start again.",
    "shop_mismatch": "The Shopify authorization was for a different store. Please start again.",
    "scope_missing": "Shopify did not grant the permissions BizFlow needs. Please reconnect and "
    "approve the requested access.",
    "exchange_failed": "Shopify did not complete the connection. Please try again.",
    "already_connected": "That store is already connected to another BizFlow account.",
    "different_store": "Disconnect your current store before connecting a different one.",
    "auth_failed": "Shopify rejected the saved credentials. Please reconnect your store.",
    "reauthorization_required": "The Shopify authorization expired. Please reconnect your store.",
    "access_denied": "Shopify did not allow access to some of this data. Customer and order "
    "details may need protected customer data approval for your Shopify app.",
    "shop_unavailable": "Shopify says this store is unavailable (it may be frozen, locked or "
    "deleted).",
    "throttled": "Shopify is rate limiting requests. The sync will retry automatically.",
    "unavailable": "Shopify could not be reached. The sync will retry automatically.",
    "request_failed": "Shopify rejected a request. If this keeps happening, contact support.",
    "queue_unavailable": "The background job queue is unavailable. Please try again shortly.",
    "interrupted": "The sync was interrupted and will resume.",
    "internal_error": "Something went wrong during the sync. Please try again.",
}


def user_message(code: str | None) -> str | None:
    if code is None:
        return None
    return USER_MESSAGES.get(code, USER_MESSAGES["internal_error"])
