"""knowledge base: pgvector, knowledge_sources, documents, knowledge_chunks

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1536  # frozen here on purpose: a migration must not change when the model does


def upgrade() -> None:
    # The Docker image only *ships* pgvector; it must be enabled per database. IF NOT EXISTS keeps
    # this safe when an administrator pre-created it (e.g. managed Postgres without CREATE rights).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_sources",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "WEBSITE",
                "PDF",
                "FAQ",
                "MANUAL",
                "PRODUCT_CATALOG",
                "OTHER",
                name="type",
                native_enum=False,
                create_constraint=True,
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "CRAWLING",
                "COMPLETED",
                "FAILED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crawl_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crawl_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("crawl_stats", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_knowledge_sources_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_sources")),
        sa.UniqueConstraint(
            "id", "organization_id", name=op.f("uq_knowledge_sources_id_organization_id")
        ),
        sa.UniqueConstraint(
            "organization_id", "type", "url", name=op.f("uq_knowledge_sources_org_type_url")
        ),
    )
    op.create_index(
        op.f("ix_knowledge_sources_organization_id"),
        "knowledge_sources",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_sources_status"), "knowledge_sources", ["status"], unique=False
    )

    op.create_table(
        "documents",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_source_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "READY",
                "FAILED",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_documents_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_source_id", "organization_id"],
            ["knowledge_sources.id", "knowledge_sources.organization_id"],
            name=op.f("fk_documents_source_org"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("id", "organization_id", name=op.f("uq_documents_id_organization_id")),
        sa.UniqueConstraint("knowledge_source_id", "url", name=op.f("uq_documents_source_url")),
    )
    op.create_index(
        op.f("ix_documents_organization_id"), "documents", ["organization_id"], unique=False
    )
    op.create_index(
        op.f("ix_documents_knowledge_source_id"),
        "documents",
        ["knowledge_source_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_knowledge_chunks_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            name=op.f("fk_knowledge_chunks_document_org"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name=op.f("uq_knowledge_chunks_document_index")
        ),
    )
    op.create_index(
        op.f("ix_knowledge_chunks_organization_id"),
        "knowledge_chunks",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_chunks_document_id"), "knowledge_chunks", ["document_id"], unique=False
    )
    # Deliberately NO ANN (HNSW/IVFFlat) index on `embedding`. Retrieval is always filtered to one
    # organization, and an approximate index applies that filter *after* its graph scan, so it can
    # return fewer than top_k rows. Exact search over a single tenant's rows (via the
    # organization_id index above) is correct and fast at this scale. See docs/knowledge-base.md.


def downgrade() -> None:
    op.drop_index(op.f("ix_knowledge_chunks_document_id"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_organization_id"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index(op.f("ix_documents_knowledge_source_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_organization_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_knowledge_sources_status"), table_name="knowledge_sources")
    op.drop_index(op.f("ix_knowledge_sources_organization_id"), table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
    # The vector extension is intentionally left installed: it is database-wide, other objects may
    # use it, and dropping an extension is not something a table migration should do implicitly.
