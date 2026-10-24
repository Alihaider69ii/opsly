"""index the foreign keys that deleting a parent row has to search

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23

Additive and reversible: four indexes, no table or column is altered. Each is on a foreign key
that had no index leading with it, so removing a product, a variant, a user or a connection made
PostgreSQL scan the whole child table once per removed parent row (the disconnect purge and a
cascading organization delete do exactly that, and slow down as every tenant's data grows).
"""
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(op.f('ix_order_items_product_id'), 'order_items', ['product_id'], unique=False)
    op.create_index(op.f('ix_order_items_variant_id'), 'order_items', ['variant_id'], unique=False)
    op.create_index(op.f('ix_shopify_oauth_states_user_id'), 'shopify_oauth_states', ['user_id'], unique=False)
    op.create_index(op.f('ix_shopify_webhook_events_connection_id'), 'shopify_webhook_events', ['connection_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_shopify_webhook_events_connection_id'), table_name='shopify_webhook_events')
    op.drop_index(op.f('ix_shopify_oauth_states_user_id'), table_name='shopify_oauth_states')
    op.drop_index(op.f('ix_order_items_variant_id'), table_name='order_items')
    op.drop_index(op.f('ix_order_items_product_id'), table_name='order_items')
