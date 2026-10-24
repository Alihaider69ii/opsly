import os
import subprocess
import sys

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.integration.conftest import (
    BACKEND_DIR,
    TEST_DATABASE_URL,
    alembic_config,
    recreate_database,
)

SCRATCH_URL = (
    make_url(TEST_DATABASE_URL)
    .set(database="bizflow_migration_test")
    .render_as_string(hide_password=False)  # str(url) would mask the password as ***
)


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def test_pgvector_is_enabled_by_the_migration_not_assumed():
    """Start from a brand-new database where the extension demonstrably does not exist."""
    recreate_database(SCRATCH_URL)
    engine = create_engine(SCRATCH_URL)
    try:
        assert scalar(engine, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == 0
        command.upgrade(alembic_config(SCRATCH_URL), "0002")
        assert scalar(engine, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == 0
        command.upgrade(alembic_config(SCRATCH_URL), "head")
        assert scalar(engine, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == 1
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_cycle_preserves_earlier_data():
    recreate_database(SCRATCH_URL)
    cfg = alembic_config(SCRATCH_URL)
    engine = create_engine(SCRATCH_URL)
    try:
        command.upgrade(cfg, "0002")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) "
                    "VALUES ('11111111-1111-1111-1111-111111111111', 'Acme', 'acme')"
                )
            )
        command.upgrade(cfg, "head")
        assert scalar(engine, "SELECT to_regclass('public.knowledge_chunks') IS NOT NULL")

        command.downgrade(cfg, "0002")  # removes only the knowledge tables
        assert scalar(engine, "SELECT to_regclass('public.knowledge_chunks') IS NULL")
        assert scalar(engine, "SELECT to_regclass('public.knowledge_sources') IS NULL")
        assert scalar(engine, "SELECT count(*) FROM organizations WHERE slug = 'acme'") == 1
        # the extension is deliberately left installed (it is database-wide)
        assert scalar(engine, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == 1

        command.upgrade(cfg, "head")
        assert scalar(engine, "SELECT to_regclass('public.knowledge_chunks') IS NOT NULL")
        assert scalar(engine, "SELECT count(*) FROM organizations WHERE slug = 'acme'") == 1
    finally:
        engine.dispose()


def test_there_is_a_single_head_revision():
    script = ScriptDirectory.from_config(alembic_config(TEST_DATABASE_URL))
    assert script.get_heads() == ["0007"]


def test_models_and_migrated_schema_agree(pg_engine):
    """`alembic check` exactly as a developer runs it: a clean interpreter with only the app's
    models loaded (not this process, where test-only stand-in models share the metadata)."""
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "No new upgrade operations detected" in output


def test_schema_has_the_expected_shape(pg_engine):
    tables = {
        r[0] for r in rows(pg_engine, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    }
    assert {"knowledge_sources", "documents", "knowledge_chunks"} <= tables

    column_type = scalar(
        pg_engine,
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding'",
    )
    assert column_type == "vector(1536)"

    for table in ("knowledge_sources", "documents", "knowledge_chunks"):
        indexed = {
            r[0]
            for r in rows(
                pg_engine, "SELECT indexdef FROM pg_indexes WHERE tablename = :t", t=table
            )
        }
        assert any("(organization_id)" in definition for definition in indexed), table

    fks = {
        r[0]: r[1]
        for r in rows(
            pg_engine,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid::regclass::text IN ('documents', 'knowledge_chunks')",
        )
    }
    assert "FOREIGN KEY (knowledge_source_id, organization_id)" in fks["fk_documents_source_org"]
    assert "FOREIGN KEY (document_id, organization_id)" in fks["fk_knowledge_chunks_document_org"]
    assert all("ON DELETE CASCADE" in definition for definition in fks.values())

    checks = {
        r[0] for r in rows(pg_engine, "SELECT conname FROM pg_constraint WHERE contype = 'c'")
    }
    assert {
        "ck_knowledge_sources_type",
        "ck_knowledge_sources_status",
        "ck_documents_status",
    } <= checks


def test_no_ann_index_on_embeddings_by_design(pg_engine):
    """Search is exact within one tenant; an approximate index would under-return when filtered by
    organization (see docs/knowledge-base.md). If this ever changes, update the docs with it."""
    definitions = " ".join(
        r[0]
        for r in rows(
            pg_engine, "SELECT indexdef FROM pg_indexes WHERE tablename = 'knowledge_chunks'"
        )
    ).lower()
    assert "hnsw" not in definitions and "ivfflat" not in definitions


def test_enum_check_constraints_reject_bad_values(pg_engine, pg):
    import pytest
    from sqlalchemy.exc import IntegrityError

    pg.execute(
        text("INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'A', 'a')")
    )
    org_id = pg.scalar(text("SELECT id FROM organizations"))
    with pytest.raises(IntegrityError):
        pg.execute(
            text(
                "INSERT INTO knowledge_sources (id, organization_id, type, name, status) "
                "VALUES (gen_random_uuid(), :o, 'EMAIL', 'x', 'PENDING')"
            ),
            {"o": org_id},
        )
    pg.rollback()


def test_chat_migration_is_additive_and_reversible_without_touching_knowledge_data():
    recreate_database(SCRATCH_URL)
    cfg = alembic_config(SCRATCH_URL)
    engine = create_engine(SCRATCH_URL)
    try:
        command.upgrade(cfg, "0003")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) VALUES ('22222222-2222-2222-2222-222222222222', 'Acme', 'acme')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO knowledge_sources (id, organization_id, type, name, status) "
                    "VALUES ('33333333-3333-3333-3333-333333333333', '22222222-2222-2222-2222-222222222222', 'WEBSITE', 'x', 'COMPLETED')"
                )
            )
        command.upgrade(cfg, "0004")
        assert (
            scalar(engine, "SELECT count(*) FROM knowledge_sources") == 1
        )  # untouched by the upgrade
        for table in ("chat_widgets", "conversations", "messages"):
            assert scalar(engine, f"SELECT to_regclass('public.{table}') IS NOT NULL"), table

        command.downgrade(cfg, "0003")
        assert scalar(engine, "SELECT to_regclass('public.conversations') IS NULL")
        assert (
            scalar(engine, "SELECT count(*) FROM knowledge_sources") == 1
        )  # Phase 2 data survives
        command.upgrade(cfg, "head")
        assert scalar(engine, "SELECT to_regclass('public.messages') IS NOT NULL")
    finally:
        engine.dispose()


def test_chat_schema_has_the_expected_constraints(pg_engine):
    fks = {
        r[0]: r[1]
        for r in rows(
            pg_engine,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'f' AND conrelid::regclass::text = 'messages'",
        )
    }
    assert "FOREIGN KEY (conversation_id, organization_id)" in fks["fk_messages_conversation_org"]
    assert all("ON DELETE CASCADE" in d for d in fks.values())
    uniques = {
        r[0] for r in rows(pg_engine, "SELECT conname FROM pg_constraint WHERE contype = 'u'")
    }
    assert {
        "uq_messages_conversation_position",
        "uq_chat_widgets_organization_id",
        "uq_conversations_id_organization_id",
    } <= uniques
    indexes = " ".join(
        r[0]
        for r in rows(
            pg_engine,
            "SELECT indexdef FROM pg_indexes WHERE tablename IN ('chat_widgets', 'conversations', 'messages')",
        )
    )
    assert "(public_key)" in indexes and indexes.count("(organization_id)") >= 2
    checks = {
        r[0] for r in rows(pg_engine, "SELECT conname FROM pg_constraint WHERE contype = 'c'")
    }
    assert {"ck_conversations_channel", "ck_conversations_status", "ck_messages_role"} <= checks


SHOPIFY_TABLES = (
    "shopify_connections",
    "shopify_sync_states",
    "shopify_oauth_states",
    "shopify_webhook_events",
    "products",
    "product_variants",
    "customers",
    "orders",
    "order_items",
)


def test_shopify_migration_is_additive_and_reversible_without_touching_earlier_data():
    recreate_database(SCRATCH_URL)
    cfg = alembic_config(SCRATCH_URL)
    engine = create_engine(SCRATCH_URL)
    try:
        command.upgrade(cfg, "0004")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) VALUES ('44444444-4444-4444-4444-444444444444', 'Acme', 'acme')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO knowledge_sources (id, organization_id, type, name, status) "
                    "VALUES ('55555555-5555-5555-5555-555555555555', '44444444-4444-4444-4444-444444444444', 'WEBSITE', 'x', 'COMPLETED')"
                )
            )
        for table in SHOPIFY_TABLES:
            assert scalar(engine, f"SELECT to_regclass('public.{table}') IS NULL"), table

        command.upgrade(cfg, "0005")
        for table in SHOPIFY_TABLES:
            assert scalar(engine, f"SELECT to_regclass('public.{table}') IS NOT NULL"), table
        assert scalar(engine, "SELECT count(*) FROM knowledge_sources") == 1

        command.downgrade(cfg, "0004")
        for table in SHOPIFY_TABLES:
            assert scalar(engine, f"SELECT to_regclass('public.{table}') IS NULL"), table
        assert scalar(engine, "SELECT count(*) FROM knowledge_sources") == 1
        assert scalar(engine, "SELECT count(*) FROM organizations") == 1
        command.upgrade(cfg, "head")
        assert scalar(engine, "SELECT to_regclass('public.order_items') IS NOT NULL")
    finally:
        engine.dispose()


def _constraints(pg_engine, kind):
    return {
        r[0]: r[1]
        for r in rows(
            pg_engine,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = :k",
            k=kind,
        )
    }


def test_shopify_tables_use_uuid_keys_and_are_tenant_indexed(pg_engine):
    for table in SHOPIFY_TABLES:
        key_type = scalar(
            pg_engine,
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary",
            t=table,
        )
        assert key_type == "uuid", table
    for table in SHOPIFY_TABLES:
        if table in ("shopify_webhook_events",):
            continue  # organization_id is nullable there (a delivery for an unknown shop)
        definitions = " ".join(
            r[0]
            for r in rows(
                pg_engine, "SELECT indexdef FROM pg_indexes WHERE tablename = :t", t=table
            )
        )
        assert "(organization_id)" in definitions or "organization_id" in definitions, table


def test_shopify_schema_has_the_expected_constraints(pg_engine):
    uniques = _constraints(pg_engine, "u")
    assert {
        "uq_products_org_shopify_product",
        "uq_product_variants_org_shopify_variant",
        "uq_customers_org_shopify_customer",
        "uq_orders_org_shopify_order",
        "uq_order_items_org_shopify_line_item",
        "uq_shopify_connections_organization_id",
        "uq_shopify_sync_states_connection_resource",
        "uq_shopify_webhook_events_shop_webhook",
    } <= set(uniques)
    assert "(organization_id, shopify_product_id)" in uniques["uq_products_org_shopify_product"]
    assert "(shop_domain, webhook_id)" in uniques["uq_shopify_webhook_events_shop_webhook"]

    fks = _constraints(pg_engine, "f")
    expect = {
        "fk_product_variants_product_org": "FOREIGN KEY (product_id, organization_id)",
        "fk_order_items_order_org": "FOREIGN KEY (order_id, organization_id)",
        "fk_order_items_product_org": "FOREIGN KEY (product_id, organization_id)",
        "fk_order_items_variant_org": "FOREIGN KEY (variant_id, organization_id)",
        "fk_orders_customer_org": "FOREIGN KEY (customer_id, organization_id)",
        "fk_shopify_sync_states_connection_org": "FOREIGN KEY (connection_id, organization_id)",
        "fk_shopify_webhook_events_connection_org": "FOREIGN KEY (connection_id, organization_id)",
    }
    for name, fragment in expect.items():
        assert fragment in fks[name], name
    assert "ON DELETE CASCADE" in fks["fk_order_items_order_org"]
    assert "ON DELETE CASCADE" in fks["fk_product_variants_product_org"]

    active_shop = scalar(
        pg_engine,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_shopify_connections_active_shop'",
    )
    assert "UNIQUE" in active_shop and "(shop_domain)" in active_shop
    assert "DISCONNECTED" in active_shop  # a store belongs to one organization at a time


def test_no_shopify_table_has_a_plaintext_secret_column(pg_engine):
    columns = {
        (r[0], r[1])
        for r in rows(
            pg_engine,
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(:t)",
            t=list(SHOPIFY_TABLES),
        )
    }
    names = {c for _, c in columns}
    assert {"access_token_encrypted", "refresh_token_encrypted"} <= names
    for forbidden in (
        "access_token",
        "refresh_token",
        "client_secret",
        "token",
        "api_key",
        "password",
    ):
        assert forbidden not in names


FK_WITHOUT_INDEX = """
SELECT c.conrelid::regclass::text AS child, c.conname,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS columns
FROM pg_constraint c
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND i.indisvalid
      AND (i.indkey::int2[])[0] = ANY (
        COALESCE(
          NULLIF(
            ARRAY(
              SELECT k FROM unnest(c.conkey) AS k
              WHERE k <> COALESCE(
                (SELECT attnum FROM pg_attribute
                  WHERE attrelid = c.conrelid AND attname = 'organization_id'), -1)
            ),
            '{}'::int2[]
          ),
          c.conkey
        )
      )
  )
ORDER BY 1, 2
"""


def test_every_foreign_key_has_an_index_leading_with_a_column_other_than_the_tenant(pg_engine):
    """Without one, deleting a parent row scans the whole child table (once per deleted row), and
    cascades, tenant purges and referential checks degrade as other tenants' data grows. (The keys
    are UUIDs, so an index on the referencing id alone is selective enough; one on the tenant
    column alone is not, because it spans all of that tenant's rows.)"""
    unindexed = [
        (child, name, list(cols)) for child, name, cols in rows(pg_engine, FK_WITHOUT_INDEX)
    ]
    assert unindexed == []


FK_INDEXES_0006 = (
    "ix_order_items_product_id",
    "ix_order_items_variant_id",
    "ix_shopify_oauth_states_user_id",
    "ix_shopify_webhook_events_connection_id",
)


def test_the_foreign_key_index_migration_is_additive_reversible_and_keeps_data():
    recreate_database(SCRATCH_URL)
    cfg = alembic_config(SCRATCH_URL)
    engine = create_engine(SCRATCH_URL)
    try:
        command.upgrade(cfg, "0005")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) "
                    "VALUES ('66666666-6666-6666-6666-666666666666', 'Acme', 'acme')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash) "
                    "VALUES ('77777777-7777-7777-7777-777777777777', 'a@example.com', 'x')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO shopify_oauth_states (id, organization_id, user_id, shop_domain, "
                    "state_hash, expires_at) VALUES ('88888888-8888-8888-8888-888888888888', "
                    "'66666666-6666-6666-6666-666666666666', '77777777-7777-7777-7777-777777777777', "
                    "'x.myshopify.com', 'h', now())"
                )
            )
        for name in FK_INDEXES_0006:
            assert scalar(engine, "SELECT to_regclass(:n) IS NULL", n=name), name

        command.upgrade(cfg, "0006")
        for name in FK_INDEXES_0006:
            assert scalar(engine, "SELECT to_regclass(:n) IS NOT NULL", n=name), name
        assert scalar(engine, "SELECT count(*) FROM shopify_oauth_states") == 1

        command.downgrade(cfg, "0005")
        for name in FK_INDEXES_0006:
            assert scalar(engine, "SELECT to_regclass(:n) IS NULL", n=name), name
        assert scalar(engine, "SELECT count(*) FROM shopify_oauth_states") == 1
        assert scalar(engine, "SELECT count(*) FROM users") == 1

        command.upgrade(cfg, "head")
        assert scalar(engine, "SELECT to_regclass('ix_order_items_product_id') IS NOT NULL")
    finally:
        engine.dispose()


def test_the_whole_chain_runs_up_down_and_up_again_on_an_empty_database():
    recreate_database(SCRATCH_URL)
    cfg = alembic_config(SCRATCH_URL)
    engine = create_engine(SCRATCH_URL)
    try:
        command.upgrade(cfg, "head")
        tables_at_head = {
            r[0]
            for r in rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert {
            "users",
            "organizations",
            "products",
            "shopify_connections",
            "automation_workflows",
            "automation_events",
            "automation_executions",
            "automation_execution_steps",
            "website_leads",
            "automation_tasks",
        } <= tables_at_head

        command.downgrade(cfg, "base")
        left = {
            r[0]
            for r in rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert left <= {"alembic_version"}, f"downgrade left behind: {sorted(left)}"

        command.upgrade(cfg, "head")
        again = {
            r[0]
            for r in rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert again == tables_at_head
        assert scalar(engine, "SELECT version_num FROM alembic_version") == "0007"
    finally:
        engine.dispose()
