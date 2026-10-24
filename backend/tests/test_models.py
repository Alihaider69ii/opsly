import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Organization, OrganizationMember, OrganizationRole, User


def _user(email="a@example.com"):
    return User(email=email, password_hash="x")


def test_membership_links_user_and_org(db):
    user, org = _user(), Organization(name="Acme", slug="acme")
    db.add(OrganizationMember(user=user, organization=org, role=OrganizationRole.OWNER))
    db.commit()

    assert org.members[0].user.email == "a@example.com"
    assert user.memberships[0].organization.slug == "acme"
    assert user.id is not None and user.created_at is not None


def test_user_belongs_to_multiple_organizations(db):
    user = _user()
    for slug in ("one", "two"):
        db.add(OrganizationMember(user=user, organization=Organization(name=slug, slug=slug)))
    db.commit()
    assert len(user.memberships) == 2


def test_duplicate_membership_rejected(db):
    user, org = _user(), Organization(name="Acme", slug="acme")
    db.add_all([user, org])
    db.commit()
    db.add(OrganizationMember(user_id=user.id, organization_id=org.id))
    db.commit()
    db.add(OrganizationMember(user_id=user.id, organization_id=org.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_unique_email_and_slug(db):
    db.add_all([_user(), Organization(name="A", slug="a")])
    db.commit()
    db.add(_user())
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(Organization(name="B", slug="a"))
    with pytest.raises(IntegrityError):
        db.commit()
