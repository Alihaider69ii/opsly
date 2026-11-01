import uuid
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher

from app.db.models import (
    Organization,
    OrganizationEnvironment,
    OrganizationMember,
    OrganizationRole,
    User,
)
from app.services.auth.service import EmailAlreadyRegistered
from app.services.organizations.service import (
    resolve_org_context,
    resolve_platform_admin_org_context,
)
from app.services.platform import service

_HASHER = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def make_user(db, *, email: str, is_platform_admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=_HASHER.hash("irrelevant"),
        is_platform_admin=is_platform_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_create_brand_creates_org_owner_and_two_audit_events(db):
    admin = make_user(db, email="admin@platform.test", is_platform_admin=True)

    membership = service.create_brand(
        db,
        name="UrbanThread",
        slug="urbanthread-test",
        environment=OrganizationEnvironment.DEMO,
        owner_name="Owner Name",
        owner_email="owner@urbanthread-test.demo",
        temporary_password="Demo12345!",
        actor_user_id=admin.id,
        now=NOW,
    )

    assert membership.role is OrganizationRole.OWNER
    org = membership.organization
    assert org.environment is OrganizationEnvironment.DEMO
    assert org.is_active is True

    events = service.list_audit(db, organization_id=org.id)
    event_types = {e.event_type for e in events}
    assert event_types == {"BRAND_CREATED", "BRAND_OWNER_CREATED"}
    assert all(e.actor_user_id == admin.id for e in events)


def test_create_brand_with_an_already_registered_owner_email_raises(db):
    admin = make_user(db, email="admin2@platform.test", is_platform_admin=True)
    make_user(db, email="taken@example.com")

    with pytest.raises(EmailAlreadyRegistered):
        service.create_brand(
            db,
            name="GlowCart",
            slug=None,
            environment=OrganizationEnvironment.DEMO,
            owner_name="Owner",
            owner_email="taken@example.com",
            temporary_password="Demo12345!",
            actor_user_id=admin.id,
        )


def test_set_brand_active_toggles_and_audits(db):
    admin = make_user(db, email="admin3@platform.test", is_platform_admin=True)
    membership = service.create_brand(
        db,
        name="TechNest",
        slug="technest-test",
        environment=OrganizationEnvironment.DEMO,
        owner_name="Owner",
        owner_email="owner@technest-test.demo",
        temporary_password="Demo12345!",
        actor_user_id=admin.id,
    )
    org_id = membership.organization_id

    deactivated = service.set_brand_active(
        db, organization_id=org_id, is_active=False, actor_user_id=admin.id, now=NOW
    )
    assert deactivated is not None
    assert deactivated.is_active is False

    reactivated = service.set_brand_active(
        db, organization_id=org_id, is_active=True, actor_user_id=admin.id, now=NOW
    )
    assert reactivated is not None
    assert reactivated.is_active is True

    events = [e.event_type for e in service.list_audit(db, organization_id=org_id)]
    assert "BRAND_DEACTIVATED" in events
    assert "BRAND_ACTIVATED" in events


def test_set_brand_active_on_missing_org_returns_none(db):
    admin = make_user(db, email="admin4@platform.test", is_platform_admin=True)
    result = service.set_brand_active(
        db, organization_id=uuid.uuid4(), is_active=False, actor_user_id=admin.id
    )
    assert result is None


def test_resolve_platform_admin_org_context_needs_no_membership(db):
    admin = make_user(db, email="admin5@platform.test", is_platform_admin=True)
    org = Organization(id=uuid.uuid4(), name="NovaHome", slug="novahome-test")
    db.add(org)
    db.commit()

    # The admin has zero OrganizationMember rows for this org (brief section 0).
    assert (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == org.id, OrganizationMember.user_id == admin.id
        )
        .first()
        is None
    )

    ctx = resolve_platform_admin_org_context(db, admin, org.id)
    assert ctx is not None
    assert ctx.user.id == admin.id  # the real admin identity, never a synthetic brand user
    assert ctx.role is OrganizationRole.OWNER
    assert ctx.organization.id == org.id


def test_resolve_platform_admin_org_context_returns_none_for_a_missing_org(db):
    admin = make_user(db, email="admin6@platform.test", is_platform_admin=True)
    assert resolve_platform_admin_org_context(db, admin, uuid.uuid4()) is None


def test_a_deactivated_brand_is_invisible_to_resolve_org_context_for_its_own_member(db):
    org = Organization(
        id=uuid.uuid4(), name="Deactivated Co", slug="deactivated-co", is_active=False
    )
    db.add(org)
    user = make_user(db, email="member@deactivated-co.test")
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=OrganizationRole.OWNER))
    db.commit()

    assert resolve_org_context(db, user, org.id) is None
    assert (
        resolve_org_context(db, user) is None
    )  # also absent from the "earliest membership" default


def test_a_deactivated_brand_is_still_visible_to_a_platform_admin(db):
    admin = make_user(db, email="admin7@platform.test", is_platform_admin=True)
    org = Organization(
        id=uuid.uuid4(), name="Health Check Co", slug="health-check-co", is_active=False
    )
    db.add(org)
    db.commit()

    ctx = resolve_platform_admin_org_context(db, admin, org.id)
    assert ctx is not None
    assert ctx.organization.is_active is False


def test_deleting_a_user_with_audit_history_sets_actor_user_id_null_not_an_error(db, session_factory):
    """actor_user_id's FK is ondelete=SET NULL (the audit trail must outlive the actor account,
    like organization_id does) -- it must also be a nullable column, or Postgres/SQLite reject the
    SET NULL with a NOT NULL violation instead of actually nulling it out. Caught live against
    real Postgres while cleaning up a throwaway platform admin account; regression-tested here
    against SQLite (FK enforcement is on in tests/conftest.py's engine fixture) so it never comes
    back silently."""
    admin = make_user(db, email="disposable-admin@example.com", is_platform_admin=True)
    event = service.record_audit(db, actor_user_id=admin.id, event_type="PLATFORM_ADMIN_LOGIN")
    db.commit()

    with session_factory() as other_db:
        user = other_db.get(User, admin.id)
        other_db.delete(user)
        other_db.commit()  # must not raise

    db.refresh(event)
    assert event.actor_user_id is None


def test_record_audit_rejects_an_unknown_event_type(db):
    admin = make_user(db, email="admin8@platform.test", is_platform_admin=True)
    with pytest.raises(ValueError):
        service.record_audit(db, actor_user_id=admin.id, event_type="NOT_A_REAL_EVENT")


def test_dashboard_kpis_count_real_rows_not_fabricated_numbers(db):
    admin = make_user(db, email="admin9@platform.test", is_platform_admin=True)
    before = service.dashboard_kpis(db)

    service.create_brand(
        db,
        name="KPI Demo Brand",
        slug="kpi-demo-brand",
        environment=OrganizationEnvironment.DEMO,
        owner_name="Owner",
        owner_email="owner@kpi-demo-brand.demo",
        temporary_password="Demo12345!",
        actor_user_id=admin.id,
    )

    after = service.dashboard_kpis(db)
    assert after["total_brands"] == before["total_brands"] + 1
    assert after["demo_brands"] == before["demo_brands"] + 1
    assert after["active_brands"] == before["active_brands"] + 1
    assert after["total_users"] == before["total_users"] + 1  # the new owner


def test_list_brands_filters_by_environment_and_search(db):
    admin = make_user(db, email="admin10@platform.test", is_platform_admin=True)
    service.create_brand(
        db,
        name="Searchable Demo",
        slug="searchable-demo",
        environment=OrganizationEnvironment.DEMO,
        owner_name="Owner",
        owner_email="owner@searchable-demo.demo",
        temporary_password="Demo12345!",
        actor_user_id=admin.id,
    )
    service.create_brand(
        db,
        name="Real Production Co",
        slug="real-production-co",
        environment=OrganizationEnvironment.PRODUCTION,
        owner_name="Owner",
        owner_email="owner@real-production-co.test",
        temporary_password="Demo12345!",
        actor_user_id=admin.id,
    )

    demo_only = service.list_brands(db, environment=OrganizationEnvironment.DEMO)
    assert all(r.environment is OrganizationEnvironment.DEMO for r in demo_only)
    assert any(r.slug == "searchable-demo" for r in demo_only)
    assert not any(r.slug == "real-production-co" for r in demo_only)

    found = service.list_brands(db, search="Searchable")
    assert {r.slug for r in found} == {"searchable-demo"}
