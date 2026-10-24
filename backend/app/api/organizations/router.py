from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentOrg, CurrentUser, DbSession, PathOrg, require_org_role
from app.db.models import OrganizationMember, OrganizationRole
from app.schemas.organization import (
    MemberAdd,
    MemberOut,
    OrganizationCreate,
    OrganizationOut,
    OrganizationWithRole,
)
from app.services.organizations import service
from app.services.organizations.service import OrgContext

router = APIRouter(prefix="/organizations", tags=["organizations"])

ManagerOrg = Annotated[
    OrgContext, Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))
]


def _org_out(ctx: OrgContext) -> OrganizationWithRole:
    base = OrganizationOut.model_validate(ctx.organization)
    return OrganizationWithRole(**base.model_dump(), role=ctx.role)


def _member_out(m: OrganizationMember) -> MemberOut:
    return MemberOut(
        user_id=m.user_id,
        email=m.user.email,
        full_name=m.user.full_name,
        role=m.role,
        joined_at=m.created_at,
    )


@router.post("", response_model=OrganizationWithRole, status_code=status.HTTP_201_CREATED)
def create_organization(
    body: OrganizationCreate, db: DbSession, user: CurrentUser
) -> OrganizationWithRole:
    try:
        membership = service.create_organization(
            db,
            user,
            name=body.name,
            slug=body.slug,
            website=str(body.website) if body.website else None,
            business_type=body.business_type,
        )
    except service.SlugTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, "This slug is not available") from None
    return _org_out(OrgContext(user, membership.organization, membership.role))


@router.get("", response_model=list[OrganizationWithRole])
def list_my_organizations(db: DbSession, user: CurrentUser) -> list[OrganizationWithRole]:
    return [
        _org_out(OrgContext(user, m.organization, m.role))
        for m in service.list_memberships(db, user)
    ]


# Declared before "/{organization_id}" so "current" is not parsed as an id.
@router.get("/current", response_model=OrganizationWithRole)
def get_current_organization(ctx: CurrentOrg) -> OrganizationWithRole:
    return _org_out(ctx)


@router.get("/{organization_id}", response_model=OrganizationWithRole)
def get_organization(ctx: PathOrg) -> OrganizationWithRole:
    return _org_out(ctx)


@router.get("/{organization_id}/members", response_model=list[MemberOut])
def list_members(ctx: ManagerOrg, db: DbSession) -> list[MemberOut]:
    return [_member_out(m) for m in service.list_members(db, ctx)]


@router.post(
    "/{organization_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED
)
def add_member(body: MemberAdd, ctx: ManagerOrg, db: DbSession) -> MemberOut:
    try:
        membership = service.add_member(db, ctx, email=body.email, role=body.role)
    except service.PermissionDenied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions") from None
    except service.UserNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account found for that email") from None
    except service.AlreadyMember:
        raise HTTPException(status.HTTP_409_CONFLICT, "User is already a member") from None
    return _member_out(membership)
