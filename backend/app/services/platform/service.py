"""Platform Admin service layer (brief sections 6-17): brand (organization) management, platform
KPIs and the platform-level audit ledger. Deliberately thin -- it reuses
``services/organizations/service.py`` for the actual org/owner creation (brief section 1: "do NOT
create a second authentication system") and `services/operations` models for the aggregate counts
it reports, rather than inventing a parallel brand model.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Customer,
    EmailConnection,
    IssueStatus,
    OperationalAuditEvent,
    OperationalIssue,
    OperationalTask,
    Organization,
    OrganizationEnvironment,
    OrganizationMember,
    OrganizationRole,
    PlatformAuditEvent,
    ShopifyConnection,
    ShopifyConnectionStatus,
    SupportConversation,
    SupportStatus,
    TaskStatus,
    User,
    WhatsAppConnection,
    WhatsAppConnectionStatus,
)
from app.services.auth.service import register_user
from app.services.organizations.service import create_organization

MAX_METADATA_CHARS = 4000
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_CLOSED_ISSUE_STATUSES = (IssueStatus.RESOLVED, IssueStatus.DISMISSED)
_CLOSED_TASK_STATUSES = (TaskStatus.RESOLVED, TaskStatus.CANCELLED)
_CLOSED_SUPPORT_STATUSES = (SupportStatus.RESOLVED, SupportStatus.CLOSED)

# Fixed vocabulary (brief section 16) -- keep in sync with every record_audit() call site.
EVENT_TYPES = frozenset(
    {
        "PLATFORM_ADMIN_LOGIN",
        "BRAND_CREATED",
        "BRAND_OWNER_CREATED",
        "BRAND_ACTIVATED",
        "BRAND_DEACTIVATED",
        "PLATFORM_ADMIN_ENTERED_BRAND",
        "PLATFORM_ADMIN_EXITED_BRAND",
    }
)


@dataclass(frozen=True)
class BrandRow:
    id: uuid.UUID
    name: str
    slug: str
    environment: OrganizationEnvironment
    is_active: bool
    owner_email: str | None
    user_count: int
    customer_count: int
    open_issues: int
    open_tasks: int
    created_at: datetime
    last_activity_at: datetime | None


def record_audit(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    event_type: str,
    organization_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    now: datetime | None = None,
) -> PlatformAuditEvent:
    """Flush-only, like services/operations/audit.py:record -- callers commit."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown platform audit event_type: {event_type!r}")
    metadata = metadata or {}
    if len(str(metadata)) > MAX_METADATA_CHARS:
        metadata = {"truncated": True}
    event = PlatformAuditEvent(
        id=uuid.uuid4(),
        actor_user_id=actor_user_id,
        event_type=event_type,
        organization_id=organization_id,
        metadata_=metadata,
        created_at=now or datetime.now(UTC),
    )
    db.add(event)
    db.flush()
    return event


def list_audit(
    db: Session, *, organization_id: uuid.UUID | None = None, limit: int = DEFAULT_LIMIT
) -> list[PlatformAuditEvent]:
    limit = max(1, min(limit, MAX_LIMIT))
    stmt = select(PlatformAuditEvent)
    if organization_id is not None:
        stmt = stmt.where(PlatformAuditEvent.organization_id == organization_id)
    stmt = stmt.order_by(PlatformAuditEvent.created_at.desc()).limit(limit)
    return list(db.scalars(stmt))


def create_brand(
    db: Session,
    *,
    name: str,
    slug: str | None,
    environment: OrganizationEnvironment,
    owner_name: str,
    owner_email: str,
    temporary_password: str,
    actor_user_id: uuid.UUID,
    now: datetime | None = None,
) -> OrganizationMember:
    """Create the brand's owner user, then its organization (reusing the existing org-creation
    transaction, which also creates the OWNER membership), then tag the environment and audit.
    Raises EmailAlreadyRegistered / SlugTaken -- the API layer maps these to 409s."""
    owner = register_user(db, email=owner_email, password=temporary_password, full_name=owner_name)
    membership = create_organization(
        db, owner, name=name, slug=slug, website=None, business_type=None
    )
    org = membership.organization
    org.environment = environment
    db.commit()
    db.refresh(org)
    record_audit(
        db,
        actor_user_id=actor_user_id,
        event_type="BRAND_CREATED",
        organization_id=org.id,
        metadata={"brand_name": org.name, "environment": environment.value},
        now=now,
    )
    record_audit(
        db,
        actor_user_id=actor_user_id,
        event_type="BRAND_OWNER_CREATED",
        organization_id=org.id,
        metadata={"owner_email": owner.email},
        now=now,
    )
    db.commit()
    return membership


def _brand_row(db: Session, org: Organization) -> BrandRow:
    owner_email = db.scalar(
        select(User.email)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.role == OrganizationRole.OWNER,
        )
        .order_by(OrganizationMember.created_at)
        .limit(1)
    )
    user_count = (
        db.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.organization_id == org.id)
        )
        or 0
    )
    customer_count = (
        db.scalar(
            select(func.count()).select_from(Customer).where(Customer.organization_id == org.id)
        )
        or 0
    )
    open_issues = (
        db.scalar(
            select(func.count())
            .select_from(OperationalIssue)
            .where(
                OperationalIssue.organization_id == org.id,
                OperationalIssue.status.not_in(_CLOSED_ISSUE_STATUSES),
            )
        )
        or 0
    )
    open_tasks = (
        db.scalar(
            select(func.count())
            .select_from(OperationalTask)
            .where(
                OperationalTask.organization_id == org.id,
                OperationalTask.status.not_in(_CLOSED_TASK_STATUSES),
            )
        )
        or 0
    )
    last_activity_at = db.scalar(
        select(func.max(OperationalAuditEvent.created_at)).where(
            OperationalAuditEvent.organization_id == org.id
        )
    )
    return BrandRow(
        id=org.id,
        name=org.name,
        slug=org.slug,
        environment=org.environment,
        is_active=org.is_active,
        owner_email=owner_email,
        user_count=user_count,
        customer_count=customer_count,
        open_issues=open_issues,
        open_tasks=open_tasks,
        created_at=org.created_at,
        last_activity_at=last_activity_at,
    )


def list_brands(
    db: Session,
    *,
    environment: OrganizationEnvironment | None = None,
    is_active: bool | None = None,
    search: str | None = None,
) -> list[BrandRow]:
    stmt = select(Organization)
    if environment is not None:
        stmt = stmt.where(Organization.environment == environment)
    if is_active is not None:
        stmt = stmt.where(Organization.is_active.is_(is_active))
    if search:
        stmt = stmt.where(Organization.name.ilike(f"%{search}%"))
    stmt = stmt.order_by(Organization.created_at.desc())
    orgs = list(db.scalars(stmt))
    return [_brand_row(db, org) for org in orgs]


def get_brand(db: Session, organization_id: uuid.UUID) -> BrandRow | None:
    org = db.get(Organization, organization_id)
    if org is None:
        return None
    return _brand_row(db, org)


def set_brand_active(
    db: Session,
    *,
    organization_id: uuid.UUID,
    is_active: bool,
    actor_user_id: uuid.UUID,
    now: datetime | None = None,
) -> Organization | None:
    org = db.get(Organization, organization_id)
    if org is None:
        return None
    org.is_active = is_active
    record_audit(
        db,
        actor_user_id=actor_user_id,
        event_type="BRAND_ACTIVATED" if is_active else "BRAND_DEACTIVATED",
        organization_id=org.id,
        now=now,
    )
    db.commit()
    db.refresh(org)
    return org


def enter_brand(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    organization_id: uuid.UUID,
    now: datetime | None = None,
) -> Organization | None:
    """Audits the entry. Authorization itself happens in api/deps.py -- this never grants access
    on its own, it only records that access already granted was exercised."""
    org = db.get(Organization, organization_id)
    if org is None:
        return None
    record_audit(
        db,
        actor_user_id=actor_user_id,
        event_type="PLATFORM_ADMIN_ENTERED_BRAND",
        organization_id=org.id,
        now=now,
    )
    db.commit()
    return org


def exit_brand(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    organization_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    record_audit(
        db,
        actor_user_id=actor_user_id,
        event_type="PLATFORM_ADMIN_EXITED_BRAND",
        organization_id=organization_id,
        now=now,
    )
    db.commit()


def dashboard_kpis(db: Session) -> dict[str, int]:
    total_brands = db.scalar(select(func.count()).select_from(Organization)) or 0
    active_brands = (
        db.scalar(
            select(func.count()).select_from(Organization).where(Organization.is_active.is_(True))
        )
        or 0
    )
    demo_brands = (
        db.scalar(
            select(func.count())
            .select_from(Organization)
            .where(Organization.environment == OrganizationEnvironment.DEMO)
        )
        or 0
    )
    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    total_customers = db.scalar(select(func.count()).select_from(Customer)) or 0
    total_conversations = db.scalar(select(func.count()).select_from(SupportConversation)) or 0
    open_support_issues = (
        db.scalar(
            select(func.count())
            .select_from(SupportConversation)
            .where(SupportConversation.status.not_in(_CLOSED_SUPPORT_STATUSES))
        )
        or 0
    )
    open_operational_issues = (
        db.scalar(
            select(func.count())
            .select_from(OperationalIssue)
            .where(OperationalIssue.status.not_in(_CLOSED_ISSUE_STATUSES))
        )
        or 0
    )
    open_operational_tasks = (
        db.scalar(
            select(func.count())
            .select_from(OperationalTask)
            .where(OperationalTask.status.not_in(_CLOSED_TASK_STATUSES))
        )
        or 0
    )
    ai_queries_recent = (
        db.scalar(
            select(func.count())
            .select_from(OperationalAuditEvent)
            .where(OperationalAuditEvent.event_type == "AI_QUERY_ANSWERED")
        )
        or 0
    )
    connected_integrations = (
        (
            db.scalar(
                select(func.count())
                .select_from(ShopifyConnection)
                .where(ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED)
            )
            or 0
        )
        + (
            db.scalar(
                select(func.count())
                .select_from(WhatsAppConnection)
                .where(WhatsAppConnection.status == WhatsAppConnectionStatus.CONNECTED)
            )
            or 0
        )
        + (db.scalar(select(func.count()).select_from(EmailConnection)) or 0)
    )
    return {
        "total_brands": total_brands,
        "active_brands": active_brands,
        "demo_brands": demo_brands,
        "production_brands": total_brands - demo_brands,
        "total_users": total_users,
        "total_customers": total_customers,
        "total_conversations": total_conversations,
        "open_support_issues": open_support_issues,
        "open_operational_issues": open_operational_issues,
        "open_operational_tasks": open_operational_tasks,
        "ai_queries_recent": ai_queries_recent,
        "connected_integrations": connected_integrations,
    }
