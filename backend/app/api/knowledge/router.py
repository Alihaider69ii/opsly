import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.core.config import Settings, get_settings
from app.db.models import OrganizationRole
from app.schemas.knowledge import (
    DocumentDetail,
    DocumentOut,
    DocumentPage,
    SourceCreate,
    SourceOut,
)
from app.services.knowledge import service
from app.services.organizations.service import OrgContext
from app.workers.queue import JobQueue, get_job_queue

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# Anyone in the organization can read its knowledge base. Adding, re-crawling and deleting sources
# spends crawl/embedding budget and removes data, so those need OWNER or ADMIN.
ManagerOrg = Annotated[
    OrgContext,
    Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
]
Queue = Annotated[JobQueue, Depends(get_job_queue)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _source_out(view: service.SourceView) -> SourceOut:
    fields = {
        name: getattr(view.source, name)
        for name in SourceOut.model_fields
        if name != "document_count"
    }
    return SourceOut(**fields, document_count=view.document_count)


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge source not found")


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    body: SourceCreate, ctx: ManagerOrg, db: DbSession, queue: Queue, settings: AppSettings
) -> SourceOut:
    try:
        view = service.create_website_source(
            db,
            ctx,
            source_type=body.type,
            url=body.url,
            name=body.name,
            queue=queue,
            settings=settings,
        )
    except service.UnsupportedSourceType:
        raise HTTPException(422, "This source type is not supported yet") from None
    except service.InvalidSourceUrl as exc:
        raise HTTPException(422, exc.message) from None
    except service.DuplicateSource:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This website has already been added"
        ) from None
    except service.TooManySources:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your organization has reached its limit of knowledge sources",
        ) from None
    return _source_out(view)


@router.get("/sources", response_model=list[SourceOut])
def list_sources(ctx: CurrentOrg, db: DbSession) -> list[SourceOut]:
    return [_source_out(v) for v in service.list_sources(db, ctx)]


@router.get("/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> SourceOut:
    try:
        return _source_out(service.get_source(db, ctx, source_id))
    except service.SourceNotFound:
        raise _not_found() from None


@router.post(
    "/sources/{source_id}/crawl", response_model=SourceOut, status_code=status.HTTP_202_ACCEPTED
)
def crawl_source(
    source_id: uuid.UUID, ctx: ManagerOrg, db: DbSession, queue: Queue, settings: AppSettings
) -> SourceOut:
    try:
        return _source_out(
            service.request_crawl(db, ctx, source_id, queue=queue, settings=settings)
        )
    except service.SourceNotFound:
        raise _not_found() from None
    except service.CrawlInProgress:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A crawl for this source is already in progress"
        ) from None
    except service.CrawlTooSoon as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "This source was crawled very recently. Please wait a moment before crawling again.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> Response:
    try:
        service.delete_source(db, ctx, source_id)
    except service.SourceNotFound:
        raise _not_found() from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sources/{source_id}/documents", response_model=DocumentPage)
def list_documents(
    source_id: uuid.UUID,
    ctx: CurrentOrg,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentPage:
    try:
        rows, total = service.list_documents(db, ctx, source_id, limit=limit, offset=offset)
    except service.SourceNotFound:
        raise _not_found() from None
    return DocumentPage(
        items=[
            DocumentOut(
                id=r.id,
                url=r.url,
                title=r.title,
                status=r.status,  # type: ignore[arg-type]
                character_count=r.character_count,
                chunk_count=r.chunk_count,
                content_preview=r.preview,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/sources/{source_id}/documents/{document_id}", response_model=DocumentDetail)
def get_document(
    source_id: uuid.UUID, document_id: uuid.UUID, ctx: CurrentOrg, db: DbSession
) -> DocumentDetail:
    try:
        document, chunk_count = service.get_document(db, ctx, source_id, document_id)
    except service.DocumentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found") from None
    return DocumentDetail(
        id=document.id,
        url=document.url,
        title=document.title,
        status=document.status,
        character_count=len(document.content),
        chunk_count=chunk_count,
        content=document.content,
        updated_at=document.updated_at,
    )
