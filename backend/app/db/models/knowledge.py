import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Dimension of the embedding column. Fixed per column by pgvector. Changing the embedding model to
# one with a different native size needs a migration plus a re-embed (see docs/knowledge-base.md);
# OpenAI's text-embedding-3 models can all be requested at 1536 dimensions.
EMBEDDING_DIM = 1536


class KnowledgeSourceType(enum.StrEnum):
    """Where knowledge comes from. Only WEBSITE is implemented; the rest are reserved so that
    adding a new source kind does not need a schema redesign."""

    WEBSITE = "WEBSITE"
    PDF = "PDF"
    FAQ = "FAQ"
    MANUAL = "MANUAL"
    PRODUCT_CATALOG = "PRODUCT_CATALOG"
    OTHER = "OTHER"


class KnowledgeSourceStatus(enum.StrEnum):
    PENDING = "PENDING"
    CRAWLING = "CRAWLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DocumentStatus(enum.StrEnum):
    PENDING = "PENDING"  # content stored, chunks/embeddings not yet written
    READY = "READY"
    FAILED = "FAILED"


class KnowledgeSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A place knowledge is ingested from (today: one website). Tenant-owned."""

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        # Target of the composite foreign keys below, which tie children to the same organization.
        UniqueConstraint("id", "organization_id", name="uq_knowledge_sources_id_organization_id"),
        UniqueConstraint(
            "organization_id", "type", "url", name="uq_knowledge_sources_org_type_url"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # VARCHAR + CHECK rather than a native PG enum (same rule as organization roles).
    type: Mapped[KnowledgeSourceType] = mapped_column(
        Enum(KnowledgeSourceType, native_enum=False, length=30, create_constraint=True, name="type")
    )
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[KnowledgeSourceStatus] = mapped_column(
        Enum(
            KnowledgeSourceStatus,
            native_enum=False,
            length=20,
            create_constraint=True,
            name="status",
        ),
        default=KnowledgeSourceStatus.PENDING,
        index=True,
    )
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crawl_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Liveness signal for the reaper: a CRAWLING source whose heartbeat stops is failed.
    crawl_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Safe, user-facing failure information. Never raw exception text or stack traces:
    # error_message always comes from a fixed catalogue keyed by error_code.
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(500))
    # Counters from the last crawl (pages fetched, skipped, why it stopped, ...).
    crawl_stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One logical page/document discovered from a source. Holds cleaned text, never raw HTML."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_documents_id_organization_id"),
        UniqueConstraint("knowledge_source_id", "url", name="uq_documents_source_url"),
        # A document can only point at a source of its own organization.
        ForeignKeyConstraint(
            ["knowledge_source_id", "organization_id"],
            ["knowledge_sources.id", "knowledge_sources.organization_id"],
            ondelete="CASCADE",
            name="fk_documents_source_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    # SHA-256 of the cleaned content: skips re-embedding unchanged pages and de-duplicates pages.
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=20, create_constraint=True, name="status"),
        default=DocumentStatus.PENDING,
    )


class KnowledgeChunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A retrievable slice of a document, with its embedding."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_document_index"),
        # A chunk can only point at a document of its own organization.
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            ondelete="CASCADE",
            name="fk_knowledge_chunks_document_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    character_count: Mapped[int] = mapped_column(Integer)
    # JSON stand-in on SQLite keeps the fast unit-test tier running without Postgres; vector
    # operations themselves are only used (and tested) on PostgreSQL.
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")
    )
    # "<provider>:<model>" that produced the vector. Search only compares vectors from the
    # same model, so changing models never silently mixes incompatible vector spaces.
    embedding_model: Mapped[str] = mapped_column(String(100))
