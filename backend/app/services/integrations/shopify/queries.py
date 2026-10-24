"""GraphQL Admin API operations (Shopify's current API; the REST Admin API is legacy).

Page sizes are chosen against Shopify's calculated query cost: a connection costs roughly its
requested size, nested connections multiply, and a single query may not exceed 1000 points
(``MAX_COST_EXCEEDED`` otherwise). 20 products x 25 variants, or 20 orders x 25 line items, is about
520 points. Nodes with more nested rows than fit are completed by the follow-up queries below,
which page the nested connection of one node.

Money is read from ``shopMoney`` (the shop currency) so a store reports one currency throughout.
Customer contact fields use ``defaultEmailAddress`` / ``defaultPhoneNumber``; the older ``email``
and ``phone`` fields are deprecated.
"""

PRODUCT_PAGE_SIZE = 20
VARIANT_PAGE_SIZE = 25
CUSTOMER_PAGE_SIZE = 50
ORDER_PAGE_SIZE = 20
LINE_ITEM_PAGE_SIZE = 25
NESTED_FOLLOW_UP_SIZE = 100

# Rough cost (points) of each operation, used only to decide how long to wait for the bucket.
COST_HINTS = {"products": 600, "customers": 250, "orders": 600, "nested": 150, "small": 20}

SHOP_INFO = """
query ShopInfo {
  shop { name myshopifyDomain currencyCode ianaTimezone }
}
"""

_PRODUCT_FIELDS = """
  id title handle description vendor productType status tags onlineStoreUrl createdAt updatedAt
"""

_VARIANT_FIELDS = """
  id title sku price compareAtPrice inventoryQuantity availableForSale createdAt updatedAt
"""

PRODUCTS_PAGE = (
    """
query Products($first: Int!, $after: String, $variants: Int!) {
  products(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {
"""
    + _PRODUCT_FIELDS
    + """
      variants(first: $variants) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _VARIANT_FIELDS
    + """}
      }
    }
  }
}
"""
)

PRODUCT_BY_ID = (
    """
query Product($id: ID!, $variants: Int!) {
  node(id: $id) {
    ... on Product {
"""
    + _PRODUCT_FIELDS
    + """
      variants(first: $variants) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _VARIANT_FIELDS
    + """}
      }
    }
  }
}
"""
)

PRODUCT_VARIANTS_MORE = (
    """
query ProductVariants($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Product {
      variants(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _VARIANT_FIELDS
    + """}
      }
    }
  }
}
"""
)

_CUSTOMER_FIELDS = """
  id firstName lastName tags createdAt updatedAt state
  defaultEmailAddress { emailAddress marketingState }
  defaultPhoneNumber { phoneNumber }
"""

CUSTOMERS_PAGE = (
    """
query Customers($first: Int!, $after: String) {
  customers(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {"""
    + _CUSTOMER_FIELDS
    + """}
  }
}
"""
)

CUSTOMER_BY_ID = (
    """
query Customer($id: ID!) {
  node(id: $id) {
    ... on Customer {"""
    + _CUSTOMER_FIELDS
    + """}
  }
}
"""
)

_ORDER_FIELDS = """
  id name displayFinancialStatus displayFulfillmentStatus
  cancelledAt createdAt updatedAt currencyCode
  subtotalPriceSet { shopMoney { amount currencyCode } }
  totalPriceSet { shopMoney { amount currencyCode } }
  customer { id }
"""

_LINE_ITEM_FIELDS = """
  id title name sku quantity
  originalUnitPriceSet { shopMoney { amount currencyCode } }
  product { id }
  variant { id }
"""

ORDERS_PAGE = (
    """
query Orders($first: Int!, $after: String, $items: Int!) {
  orders(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {"""
    + _ORDER_FIELDS
    + """
      lineItems(first: $items) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _LINE_ITEM_FIELDS
    + """}
      }
    }
  }
}
"""
)

ORDER_BY_ID = (
    """
query Order($id: ID!, $items: Int!) {
  node(id: $id) {
    ... on Order {"""
    + _ORDER_FIELDS
    + """
      lineItems(first: $items) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _LINE_ITEM_FIELDS
    + """}
      }
    }
  }
}
"""
)

ORDER_LINE_ITEMS_MORE = (
    """
query OrderLineItems($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Order {
      lineItems(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _LINE_ITEM_FIELDS
    + """}
      }
    }
  }
}
"""
)

WEBHOOK_SUBSCRIPTIONS = """
query WebhookSubscriptions($first: Int!, $after: String) {
  webhookSubscriptions(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id topic uri }
  }
}
"""

WEBHOOK_SUBSCRIPTION_CREATE = """
mutation WebhookCreate($topic: WebhookSubscriptionTopic!, $uri: URL!) {
  webhookSubscriptionCreate(
    topic: $topic
    webhookSubscription: { uri: $uri }
  ) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""

WEBHOOK_SUBSCRIPTION_DELETE = """
mutation WebhookDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
"""
