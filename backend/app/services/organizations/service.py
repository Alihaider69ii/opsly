import re
import secrets
import unicodedata
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.models import Organization, OrganizationMember, OrganizationRole, User
from app.db.scoping import scoped_select
from app.schemas.organization import MAX_SLUG_LENGTH, MIN_SLUG_LENGTH
from app.services.auth.service import normalize_email

# Slugs may later become URL paths or subdomains, so keep app routes out of the namespace.
RESERVED_SLUGS = frozenset(
    {"admin", "api", "app", "www", "dashboard", "login", "signup", "onboarding", "settings"}
    | {"support", "help", "static", "health"}
)
_SLUG_SUFFIX_ATTEMPTS = 5

MEMBER_MANAGERS = frozenset({OrganizationRole.OWNER, OrganizationRole.ADMIN})
# Which roles each actor may hand out. OWNER is never grantable; it is set once at creation.
GRANTABLE_ROLES: dict[OrganizationRole, frozenset[OrganizationRole]] = {
    OrganizationRole.OWNER: frozenset({OrganizationRole.ADMIN, OrganizationRole.MEMBER}),
    OrganizationRole.ADMIN: frozenset({OrganizationRole.MEMBER}),
    OrganizationRole.MEMBER: frozenset(),
}


class SlugTaken(Exception):
    pass


class AlreadyMember(Exception):
    pass


class UserNotFound(Exception):
    pass


class PermissionDenied(Exception):
    pass


@dataclass(frozen=True)
class OrgContext:
    """A verified (user, organization, role) triple. Only built from a real membership row."""

    user: User
    organization: Organization
    role: OrganizationRole


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:MAX_SLUG_LENGTH].strip("-")
    if not slug:
        return "business"
    return slug if len(slug) >= MIN_SLUG_LENGTH else f"{slug}-biz"


def _generated_candidates(name: str) -> Iterator[str]:
    base = slugify(name)
    yield base
    for _ in range(_SLUG_SUFFIX_ATTEMPTS):
        yield f"{base[: MAX_SLUG_LENGTH - 5]}-{secrets.token_hex(2)}"


def create_organization(
    db: Session,
    user: User,
    *,
    name: str,
    slug: str | None,
    website: str | None,
    business_type: str | None,
) -> OrganizationMember:
    """Create the organization and the creator's OWNER membership in one transaction."""
    candidates = iter([slug]) if slug else _generated_candidates(name)
    for candidate in candidates:
        if candidate in RESERVED_SLUGS:
            continue
        org = Organization(name=name, slug=candidate, website=website, business_type=business_type)
        membership = OrganizationMember(organization=org, user=user, role=OrganizationRole.OWNER)
        db.add(membership)
        try:
            db.commit()
        except IntegrityError:  # slug already taken (also covers a concurrent create)
            db.rollback()
            continue
        db.refresh(org)
        db.refresh(membership)
        return membership
    raise SlugTaken


def list_memberships(db: Session, user: User) -> list[OrganizationMember]:
    stmt = (
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.organization))
        .where(OrganizationMember.user_id == user.id)
        .order_by(OrganizationMember.created_at, OrganizationMember.id)
    )
    return list(db.scalars(stmt).unique())


def resolve_org_context(
    db: Session, user: User, organization_id: uuid.UUID | None = None
) -> OrgContext | None:
    """Resolve the organization a request acts on, proving the user is a member of it.

    With an explicit id, returns None unless this user has a membership row for exactly that
    organization. Without one, falls back to the user's earliest membership.
    """
    stmt = (
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.organization))
        .where(OrganizationMember.user_id == user.id)
    )
    if organization_id is not None:
        stmt = stmt.where(OrganizationMember.organization_id == organization_id)
    else:
        stmt = stmt.order_by(OrganizationMember.created_at, OrganizationMember.id).limit(1)
    membership = db.scalars(stmt).unique().first()
    if membership is None:
        return None
    return OrgContext(user=user, organization=membership.organization, role=membership.role)


def list_members(db: Session, ctx: OrgContext) -> list[OrganizationMember]:
    stmt = (
        scoped_select(OrganizationMember, ctx.organization.id)
        .options(joinedload(OrganizationMember.user))
        .order_by(OrganizationMember.created_at, OrganizationMember.id)
    )
    return list(db.scalars(stmt).unique())


def add_member(
    db: Session, ctx: OrgContext, *, email: str, role: OrganizationRole
) -> OrganizationMember:
    # Authorize before touching user data, so an unauthorized caller learns nothing about accounts.
    if role not in GRANTABLE_ROLES[ctx.role]:
        raise PermissionDenied
    target = db.scalar(
        select(User).where(User.email == normalize_email(email), User.is_active.is_(True))
    )
    if target is None:
        raise UserNotFound
    membership = OrganizationMember(organization=ctx.organization, user=target, role=role)
    db.add(membership)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AlreadyMember from None
    db.refresh(membership)
    return membership
