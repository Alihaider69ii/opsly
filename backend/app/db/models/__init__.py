"""Import every model here so Base.metadata (used by Alembic) sees all tables."""

from app.db.models.chat import (
    ChatWidget,
    Conversation,
    ConversationChannel,
    ConversationStatus,
    Message,
    MessageRole,
)
from app.db.models.commerce import Customer, Order, OrderItem, Product, ProductVariant
from app.db.models.knowledge import (
    Document,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
)
from app.db.models.organization import Organization
from app.db.models.organization_member import OrganizationMember, OrganizationRole
from app.db.models.shopify import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifyOAuthState,
    ShopifySyncState,
    ShopifyWebhookEvent,
    SyncResource,
    SyncStatus,
    WebhookEventStatus,
    WebhookRegistration,
)
from app.db.models.user import User

__all__ = [
    "ChatWidget",
    "Conversation",
    "ConversationChannel",
    "ConversationStatus",
    "Customer",
    "Document",
    "DocumentStatus",
    "KnowledgeChunk",
    "KnowledgeSource",
    "KnowledgeSourceStatus",
    "KnowledgeSourceType",
    "Message",
    "MessageRole",
    "Organization",
    "OrganizationMember",
    "OrganizationRole",
    "Order",
    "OrderItem",
    "Product",
    "ProductVariant",
    "ShopifyConnection",
    "ShopifyConnectionStatus",
    "ShopifyOAuthState",
    "ShopifySyncState",
    "ShopifyWebhookEvent",
    "SyncResource",
    "SyncStatus",
    "User",
    "WebhookEventStatus",
    "WebhookRegistration",
]
