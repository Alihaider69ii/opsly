"""shopify integration and commerce data

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21

Additive only: creates shopify_connections, shopify_sync_states, shopify_oauth_states,
shopify_webhook_events, products, product_variants, customers, orders and order_items. No existing
table is altered, so Phase 1-3 data is untouched.
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('customers',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('shopify_customer_id', sa.BigInteger(), nullable=True),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('phone', sa.String(length=50), nullable=True),
    sa.Column('first_name', sa.String(length=255), nullable=True),
    sa.Column('last_name', sa.String(length=255), nullable=True),
    sa.Column('accepts_marketing', sa.Boolean(), nullable=True),
    sa.Column('shopify_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('shopify_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_customers_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers')),
    sa.UniqueConstraint('id', 'organization_id', name='uq_customers_id_organization_id'),
    sa.UniqueConstraint('organization_id', 'shopify_customer_id', name='uq_customers_org_shopify_customer')
    )
    op.create_index(op.f('ix_customers_organization_id'), 'customers', ['organization_id'], unique=False)
    op.create_table('products',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('shopify_product_id', sa.BigInteger(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('handle', sa.String(length=255), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('vendor', sa.String(length=255), nullable=True),
    sa.Column('product_type', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('product_url', sa.String(length=2048), nullable=True),
    sa.Column('currency', sa.String(length=10), nullable=True),
    sa.Column('shopify_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('shopify_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_products_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products')),
    sa.UniqueConstraint('id', 'organization_id', name='uq_products_id_organization_id'),
    sa.UniqueConstraint('organization_id', 'shopify_product_id', name='uq_products_org_shopify_product')
    )
    op.create_index(op.f('ix_products_organization_id'), 'products', ['organization_id'], unique=False)
    op.create_table('shopify_connections',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('shop_domain', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('CONNECTED', 'SYNCING', 'ERROR', 'DISCONNECTED', name='status', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('access_token_encrypted', sa.Text(), nullable=True),
    sa.Column('refresh_token_encrypted', sa.Text(), nullable=True),
    sa.Column('access_token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refresh_token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scopes', sa.String(length=1000), nullable=False),
    sa.Column('api_version', sa.String(length=20), nullable=False),
    sa.Column('shop_name', sa.String(length=255), nullable=True),
    sa.Column('currency', sa.String(length=10), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('installed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('webhook_status', sa.Enum('NOT_CONFIGURED', 'REGISTERED', 'PARTIAL', 'FAILED', name='webhook_status', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.String(length=500), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_shopify_connections_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shopify_connections')),
    sa.UniqueConstraint('id', 'organization_id', name='uq_shopify_connections_id_organization_id'),
    sa.UniqueConstraint('organization_id', name='uq_shopify_connections_organization_id')
    )
    op.create_index(op.f('ix_shopify_connections_organization_id'), 'shopify_connections', ['organization_id'], unique=False)
    op.create_index(op.f('ix_shopify_connections_status'), 'shopify_connections', ['status'], unique=False)
    op.create_index('uq_shopify_connections_active_shop', 'shopify_connections', ['shop_domain'], unique=True, postgresql_where="status <> 'DISCONNECTED'", sqlite_where="status <> 'DISCONNECTED'")
    op.create_table('shopify_oauth_states',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('shop_domain', sa.String(length=255), nullable=False),
    sa.Column('state_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_shopify_oauth_states_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_shopify_oauth_states_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shopify_oauth_states')),
    sa.UniqueConstraint('state_hash', name=op.f('uq_shopify_oauth_states_state_hash'))
    )
    op.create_index(op.f('ix_shopify_oauth_states_expires_at'), 'shopify_oauth_states', ['expires_at'], unique=False)
    op.create_index(op.f('ix_shopify_oauth_states_organization_id'), 'shopify_oauth_states', ['organization_id'], unique=False)
    op.create_table('orders',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('shopify_order_id', sa.BigInteger(), nullable=False),
    sa.Column('order_number', sa.String(length=50), nullable=True),
    sa.Column('customer_id', sa.Uuid(), nullable=True),
    sa.Column('financial_status', sa.String(length=40), nullable=True),
    sa.Column('fulfillment_status', sa.String(length=40), nullable=True),
    sa.Column('currency', sa.String(length=10), nullable=True),
    sa.Column('subtotal', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('total', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('created_at_shopify', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at_shopify', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id', 'organization_id'], ['customers.id', 'customers.organization_id'], name='fk_orders_customer_org'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_orders_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('id', 'organization_id', name='uq_orders_id_organization_id'),
    sa.UniqueConstraint('organization_id', 'shopify_order_id', name='uq_orders_org_shopify_order')
    )
    op.create_index(op.f('ix_orders_customer_id'), 'orders', ['customer_id'], unique=False)
    op.create_index(op.f('ix_orders_organization_id'), 'orders', ['organization_id'], unique=False)
    op.create_table('product_variants',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('product_id', sa.Uuid(), nullable=False),
    sa.Column('shopify_variant_id', sa.BigInteger(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('sku', sa.String(length=255), nullable=True),
    sa.Column('price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('compare_at_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('currency', sa.String(length=10), nullable=True),
    sa.Column('inventory_quantity', sa.Integer(), nullable=True),
    sa.Column('available', sa.Boolean(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_product_variants_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id', 'organization_id'], ['products.id', 'products.organization_id'], name='fk_product_variants_product_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_product_variants')),
    sa.UniqueConstraint('id', 'organization_id', name='uq_product_variants_id_organization_id'),
    sa.UniqueConstraint('organization_id', 'shopify_variant_id', name='uq_product_variants_org_shopify_variant')
    )
    op.create_index(op.f('ix_product_variants_organization_id'), 'product_variants', ['organization_id'], unique=False)
    op.create_index(op.f('ix_product_variants_product_id'), 'product_variants', ['product_id'], unique=False)
    op.create_table('shopify_sync_states',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('connection_id', sa.Uuid(), nullable=False),
    sa.Column('resource', sa.Enum('PRODUCTS', 'CUSTOMERS', 'ORDERS', name='resource', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('status', sa.Enum('IDLE', 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED', name='status', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('cursor', sa.Text(), nullable=True),
    sa.Column('items_synced', sa.Integer(), nullable=False),
    sa.Column('items_skipped', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('run_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.String(length=500), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['connection_id', 'organization_id'], ['shopify_connections.id', 'shopify_connections.organization_id'], name='fk_shopify_sync_states_connection_org', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_shopify_sync_states_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shopify_sync_states')),
    sa.UniqueConstraint('connection_id', 'resource', name='uq_shopify_sync_states_connection_resource')
    )
    op.create_index(op.f('ix_shopify_sync_states_connection_id'), 'shopify_sync_states', ['connection_id'], unique=False)
    op.create_index(op.f('ix_shopify_sync_states_organization_id'), 'shopify_sync_states', ['organization_id'], unique=False)
    op.create_index(op.f('ix_shopify_sync_states_status'), 'shopify_sync_states', ['status'], unique=False)
    op.create_table('shopify_webhook_events',
    sa.Column('shop_domain', sa.String(length=255), nullable=False),
    sa.Column('webhook_id', sa.String(length=128), nullable=False),
    sa.Column('event_id', sa.String(length=128), nullable=True),
    sa.Column('topic', sa.String(length=100), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=True),
    sa.Column('connection_id', sa.Uuid(), nullable=True),
    sa.Column('resource_id', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.Enum('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED', 'IGNORED', name='status', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['connection_id', 'organization_id'], ['shopify_connections.id', 'shopify_connections.organization_id'], name='fk_shopify_webhook_events_connection_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_shopify_webhook_events')),
    sa.UniqueConstraint('shop_domain', 'webhook_id', name='uq_shopify_webhook_events_shop_webhook')
    )
    op.create_index(op.f('ix_shopify_webhook_events_organization_id'), 'shopify_webhook_events', ['organization_id'], unique=False)
    op.create_index('ix_shopify_webhook_events_status_received', 'shopify_webhook_events', ['status', 'received_at'], unique=False)
    op.create_table('order_items',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=False),
    sa.Column('product_id', sa.Uuid(), nullable=True),
    sa.Column('variant_id', sa.Uuid(), nullable=True),
    sa.Column('shopify_line_item_id', sa.BigInteger(), nullable=False),
    sa.Column('shopify_product_id', sa.BigInteger(), nullable=True),
    sa.Column('shopify_variant_id', sa.BigInteger(), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('sku', sa.String(length=255), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['order_id', 'organization_id'], ['orders.id', 'orders.organization_id'], name='fk_order_items_order_org', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_order_items_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id', 'organization_id'], ['products.id', 'products.organization_id'], name='fk_order_items_product_org'),
    sa.ForeignKeyConstraint(['variant_id', 'organization_id'], ['product_variants.id', 'product_variants.organization_id'], name='fk_order_items_variant_org'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_items')),
    sa.UniqueConstraint('organization_id', 'shopify_line_item_id', name='uq_order_items_org_shopify_line_item')
    )
    op.create_index(op.f('ix_order_items_order_id'), 'order_items', ['order_id'], unique=False)
    op.create_index(op.f('ix_order_items_organization_id'), 'order_items', ['organization_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_order_items_organization_id'), table_name='order_items')
    op.drop_index(op.f('ix_order_items_order_id'), table_name='order_items')
    op.drop_table('order_items')
    op.drop_index('ix_shopify_webhook_events_status_received', table_name='shopify_webhook_events')
    op.drop_index(op.f('ix_shopify_webhook_events_organization_id'), table_name='shopify_webhook_events')
    op.drop_table('shopify_webhook_events')
    op.drop_index(op.f('ix_shopify_sync_states_status'), table_name='shopify_sync_states')
    op.drop_index(op.f('ix_shopify_sync_states_organization_id'), table_name='shopify_sync_states')
    op.drop_index(op.f('ix_shopify_sync_states_connection_id'), table_name='shopify_sync_states')
    op.drop_table('shopify_sync_states')
    op.drop_index(op.f('ix_product_variants_product_id'), table_name='product_variants')
    op.drop_index(op.f('ix_product_variants_organization_id'), table_name='product_variants')
    op.drop_table('product_variants')
    op.drop_index(op.f('ix_orders_organization_id'), table_name='orders')
    op.drop_index(op.f('ix_orders_customer_id'), table_name='orders')
    op.drop_table('orders')
    op.drop_index(op.f('ix_shopify_oauth_states_organization_id'), table_name='shopify_oauth_states')
    op.drop_index(op.f('ix_shopify_oauth_states_expires_at'), table_name='shopify_oauth_states')
    op.drop_table('shopify_oauth_states')
    op.drop_index('uq_shopify_connections_active_shop', table_name='shopify_connections', postgresql_where="status <> 'DISCONNECTED'", sqlite_where="status <> 'DISCONNECTED'")
    op.drop_index(op.f('ix_shopify_connections_status'), table_name='shopify_connections')
    op.drop_index(op.f('ix_shopify_connections_organization_id'), table_name='shopify_connections')
    op.drop_table('shopify_connections')
    op.drop_index(op.f('ix_products_organization_id'), table_name='products')
    op.drop_table('products')
    op.drop_index(op.f('ix_customers_organization_id'), table_name='customers')
    op.drop_table('customers')
