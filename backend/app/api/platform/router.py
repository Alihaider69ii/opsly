import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, PlatformAdmin
from app.db.models import Organization, OrganizationEnvironment, User
from app.schemas.platform import (
    BrandCreate,
    BrandCreated,
    BrandOut,
    EnterBrandResult,
    PlatformAuditEventOut,
    PlatformDashboard,
)
from app.services.auth.service import EmailAlreadyRegistered
from app.services.organizations.service import SlugTaken
from app.services.platform import service

router = APIRouter(prefix="/platform", tags=["platform"])


def _brand_out(row: service.BrandRow) -> BrandOut:
    return BrandOut.model_validate(row)


@router.get("/dashboard", response_model=PlatformDashboard)
def get_dashboard(db: DbSession, _admin: PlatformAdmin) -> PlatformDashboard:
    return PlatformDashboard(**service.dashboard_kpis(db))


@router.get("/brands", response_model=list[BrandOut])
def list_brands(
    db: DbSession,
    _admin: PlatformAdmin,
    environment: OrganizationEnvironment | None = None,
    is_active: bool | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> list[BrandOut]:
    rows = service.list_brands(db, environment=environment, is_active=is_active, search=search)
    return [_brand_out(r) for r in rows]


@router.post("/brands", response_model=BrandCreated, status_code=status.HTTP_201_CREATED)
def create_brand(payload: BrandCreate, db: DbSession, admin: PlatformAdmin) -> BrandCreated:
    try:
        membership = service.create_brand(
            db,
            name=payload.name,
            slug=payload.slug,
            environment=payload.environment,
            owner_name=payload.owner_name,
            owner_email=payload.owner_email,
            temporary_password=payload.temporary_password,
            actor_user_id=admin.id,
        )
    except EmailAlreadyRegistered:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A user with that email already exists"
        ) from None
    except SlugTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, "This slug is not available") from None
    row = service.get_brand(db, membership.organization_id)
    assert row is not None  # just created, in the same transaction
    return BrandCreated(brand=_brand_out(row), owner_email=payload.owner_email)


@router.get("/brands/{organization_id}", response_model=BrandOut)
def get_brand(organization_id: uuid.UUID, db: DbSession, _admin: PlatformAdmin) -> BrandOut:
    row = service.get_brand(db, organization_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brand not found")
    return _brand_out(row)


@router.post("/brands/{organization_id}/activate", response_model=BrandOut)
def activate_brand(organization_id: uuid.UUID, db: DbSession, admin: PlatformAdmin) -> BrandOut:
    org = service.set_brand_active(
        db, organization_id=organization_id, is_active=True, actor_user_id=admin.id
    )
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brand not found")
    row = service.get_brand(db, organization_id)
    assert row is not None
    return _brand_out(row)


@router.post("/brands/{organization_id}/deactivate", response_model=BrandOut)
def deactivate_brand(organization_id: uuid.UUID, db: DbSession, admin: PlatformAdmin) -> BrandOut:
    org = service.set_brand_active(
        db, organization_id=organization_id, is_active=False, actor_user_id=admin.id
    )
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brand not found")
    row = service.get_brand(db, organization_id)
    assert row is not None
    return _brand_out(row)


@router.post("/brands/{organization_id}/enter", response_model=EnterBrandResult)
def enter_brand(
    organization_id: uuid.UUID, db: DbSession, admin: PlatformAdmin
) -> EnterBrandResult:
    """Audits the entry (brief section 46). Authorization for the subsequent workspace calls is
    enforced per-request by api/deps.py's view-as-brand resolver, not by anything issued here --
    there is no token/session minted by this endpoint to trust."""
    org = service.enter_brand(db, actor_user_id=admin.id, organization_id=organization_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brand not found")
    row = service.get_brand(db, organization_id)
    assert row is not None
    return EnterBrandResult(organization=_brand_out(row))


@router.post("/brands/{organization_id}/exit", status_code=status.HTTP_204_NO_CONTENT)
def exit_brand(organization_id: uuid.UUID, db: DbSession, admin: PlatformAdmin) -> None:
    service.exit_brand(db, actor_user_id=admin.id, organization_id=organization_id)


def _audit_out(db: DbSession, event, *, _cache: dict) -> PlatformAuditEventOut:
    actor_email = None
    if event.actor_user_id is not None:
        users = _cache.setdefault("users", {})
        if event.actor_user_id not in users:
            user = db.get(User, event.actor_user_id)
            users[event.actor_user_id] = user.email if user else None
        actor_email = users[event.actor_user_id]

    org_name = None
    if event.organization_id is not None:
        orgs = _cache.setdefault("orgs", {})
        if event.organization_id not in orgs:
            org = db.get(Organization, event.organization_id)
            orgs[event.organization_id] = org.name if org else None
        org_name = orgs[event.organization_id]

    return PlatformAuditEventOut(
        id=event.id,
        actor_user_id=event.actor_user_id,
        actor_email=actor_email,
        event_type=event.event_type,
        organization_id=event.organization_id,
        organization_name=org_name,
        metadata=event.metadata_,
        created_at=event.created_at,
    )


@router.get("/audit", response_model=list[PlatformAuditEventOut])
def list_audit(
    db: DbSession,
    _admin: PlatformAdmin,
    organization_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PlatformAuditEventOut]:
    events = service.list_audit(db, organization_id=organization_id, limit=limit)
    cache: dict = {}
    return [_audit_out(db, e, _cache=cache) for e in events]
