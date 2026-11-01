import uuid

import pytest
from argon2 import PasswordHasher

from app.db.models import Organization, OrganizationMember, OrganizationRole, User

_HASHER = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)


@pytest.fixture()
def org_id(session_factory) -> uuid.UUID:
    oid = uuid.uuid4()
    with session_factory() as db:
        db.add(Organization(id=oid, name="Acme", slug=f"acme-{oid.hex[:8]}"))
        db.commit()
    return oid


def make_member(session_factory, org_id: uuid.UUID, *, role: OrganizationRole = OrganizationRole.MEMBER) -> uuid.UUID:
    uid = uuid.uuid4()
    with session_factory() as db:
        db.add(
            User(
                id=uid,
                email=f"{uid.hex[:8]}@example.com",
                password_hash=_HASHER.hash("irrelevant-password"),
            )
        )
        db.add(OrganizationMember(organization_id=org_id, user_id=uid, role=role))
        db.commit()
    return uid


@pytest.fixture()
def member_id(session_factory, org_id) -> uuid.UUID:
    return make_member(session_factory, org_id)
