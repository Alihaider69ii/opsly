import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.db.models import DocumentStatus, KnowledgeSourceStatus, KnowledgeSourceType


class SourceCreate(BaseModel):
    type: KnowledgeSourceType = KnowledgeSourceType.WEBSITE
    # Validated and canonicalized by the service (SSRF-aware), not by pydantic's URL type.
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
        | None
    ) = None


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: KnowledgeSourceType
    name: str
    url: str | None
    status: KnowledgeSourceStatus
    document_count: int
    last_crawled_at: datetime | None
    crawl_started_at: datetime | None
    error_code: str | None
    error_message: str | None
    crawl_stats: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    url: str
    title: str | None
    status: DocumentStatus
    character_count: int
    chunk_count: int
    content_preview: str
    updated_at: datetime


class DocumentDetail(BaseModel):
    id: uuid.UUID
    url: str
    title: str | None
    status: DocumentStatus
    character_count: int
    chunk_count: int
    content: str
    updated_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int
