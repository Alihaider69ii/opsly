import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import OrganizationRole, User
from app.services.auth.service import AuthenticationError, get_user_from_token
from app.services.organizations.service import (
    OrgContext,
    resolve_org_context,
    resolve_platform_admin_org_context,
)

DbSession = Annotated[Session, Depends(get_db)]

# auto_error=False so a missing header yields our 401 (not the framework default).
_bearer = HTTPBearer(auto_error=False, description="Access token from POST /api/auth/login")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Authenticate the request. Every protected route depends on this, directly or indirectly."""
    if credentials is None:
        raise _unauthorized()
    try:
        return get_user_from_token(db, credentials.credentials)
    except AuthenticationError:
        raise _unauthorized() from None


CurrentUser = Annotated[User, Depends(current_user)]


def platform_admin_user(user: CurrentUser) -> User:
    """Gate for every /api/platform/* route. Enforced server-side only -- the frontend hiding
    /admin from a non-admin's nav is cosmetic, this is the actual control (brief section 4)."""
    if not user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin access required")
    return user


PlatformAdmin = Annotated[User, Depends(platform_admin_user)]


def current_org(
    db: DbSession,
    user: CurrentUser,
    x_organization_id: Annotated[uuid.UUID | None, Header()] = None,
    x_platform_view_organization_id: Annotated[uuid.UUID | None, Header()] = None,
) -> OrgContext:
    """The organization this request acts on, always verified against the user's memberships.

    The X-Organization-ID header only *selects among* organizations the user already belongs to;
    an id the user is not a member of is indistinguishable from one that does not exist (404).
    Without the header, the user's earliest membership is used.

    X-Platform-View-Organization-Id (view-as-brand, brief sections 12-14) is a SEPARATE path: it
    only ever activates when the authenticated user's own ``is_platform_admin`` column is true --
    a normal brand user sending this header has no effect, since resolve_org_context is still what
    runs for them. The header can never widen a non-admin's access.
    """
    if user.is_platform_admin and x_platform_view_organization_id is not None:
        ctx = resolve_platform_admin_org_context(db, user, x_platform_view_organization_id)
        if ctx is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
        return ctx
    ctx = resolve_org_context(db, user, x_organization_id)
    if ctx is None:
        if x_organization_id is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User does not belong to an organization")
    return ctx


CurrentOrg = Annotated[OrgContext, Depends(current_org)]


def require_current_org_role(*allowed: OrganizationRole) -> Callable[[OrgContext], OrgContext]:
    """Like require_org_role, for routes that resolve the organization via current_org (no id in
    the path): verified membership plus one of the allowed roles."""

    def dependency(ctx: CurrentOrg) -> OrgContext:
        if ctx.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return ctx

    return dependency


def org_from_path(organization_id: uuid.UUID, db: DbSession, user: CurrentUser) -> OrgContext:
    """For routes shaped /organizations/{organization_id}/...: authenticate, verify membership,
    then resolve. Non-members get 404, never confirming that the organization exists.

    A platform admin reaches any organization this way too (view-as-brand) -- the id is already
    explicit in the path, so no extra header is needed for this shape.
    """
    if user.is_platform_admin:
        ctx = resolve_platform_admin_org_context(db, user, organization_id)
    else:
        ctx = resolve_org_context(db, user, organization_id)
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return ctx


PathOrg = Annotated[OrgContext, Depends(org_from_path)]


def require_org_role(*allowed: OrganizationRole) -> Callable[[OrgContext], OrgContext]:
    """Dependency factory: membership (via org_from_path) plus one of the allowed roles."""

    def dependency(ctx: PathOrg) -> OrgContext:
        if ctx.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return ctx

    return dependency
