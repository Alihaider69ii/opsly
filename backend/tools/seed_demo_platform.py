"""Create the 4 demo brands (brief section 17) with a full OWNER/ADMIN/MEMBER team each (section
18), then run the real Phase 10 demo seed (``tools/seed_operations_demo.py``) against each one so
their Operations dashboards are populated by the real detector, not hand-inserted rows.

    cd backend
    python -m tools.seed_demo_platform
    python -m tools.seed_demo_platform --reset-demo   # see below

Idempotent: re-running finds each brand by its reserved slug instead of creating a duplicate, and
``seed_operations_demo.seed()`` is itself idempotent. All 4 brands are tagged
Organization.environment = DEMO; none of the data here is a real person's.

--reset-demo deletes and recreates only the 4 organizations listed in BRANDS below, and only when
already tagged DEMO -- it refuses to touch anything tagged PRODUCTION, even if somehow one of
these slugs were reused for one (brief section 48).

Refuses to run against a production environment unless --i-know-what-im-doing is passed, the same
guard as tools/seed_platform_admin.py, since this seeds a publicly-documented demo password into
whatever database DATABASE_URL points at.
"""

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.database import SessionLocal
from app.db.models import Organization, OrganizationEnvironment, OrganizationRole, User
from app.services.organizations.service import OrgContext, add_member, create_organization
from tools.seed_operations_demo import seed as seed_operations

DEMO_PASSWORD = "Demo12345!"  # synthetic, local/demo use only -- see docs/demo-environments.md

BRANDS = [
    {"name": "UrbanThread", "slug": "urbanthread", "category": "Fashion / Apparel"},
    {"name": "GlowCart", "slug": "glowcart", "category": "Beauty / Skincare"},
    {"name": "TechNest", "slug": "technest", "category": "Electronics"},
    {"name": "NovaHome", "slug": "novahome", "category": "Home / Lifestyle"},
]


def _get_or_create_user(db: Session, *, email: str, full_name: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        return user
    user = User(email=email, password_hash=hash_password(DEMO_PASSWORD), full_name=full_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _get_or_create_brand(db: Session, brand: dict) -> Organization:
    org = db.scalar(select(Organization).where(Organization.slug == brand["slug"]))
    if org is not None:
        return org

    owner = _get_or_create_user(
        db, email=f"owner@{brand['slug']}.demo", full_name=f"{brand['name']} Owner"
    )
    membership = create_organization(
        db,
        owner,
        name=brand["name"],
        slug=brand["slug"],
        website=None,
        business_type=brand["category"],
    )
    org = membership.organization
    org.environment = OrganizationEnvironment.DEMO
    db.commit()
    db.refresh(org)

    owner_ctx = OrgContext(user=owner, organization=org, role=OrganizationRole.OWNER)
    admin = _get_or_create_user(
        db, email=f"admin@{brand['slug']}.demo", full_name=f"{brand['name']} Admin"
    )
    add_member(db, owner_ctx, email=admin.email, role=OrganizationRole.ADMIN)
    support = _get_or_create_user(
        db, email=f"support@{brand['slug']}.demo", full_name=f"{brand['name']} Support"
    )
    add_member(db, owner_ctx, email=support.email, role=OrganizationRole.MEMBER)
    return org


def _reset_demo_brands(db: Session) -> None:
    for brand in BRANDS:
        org = db.scalar(select(Organization).where(Organization.slug == brand["slug"]))
        if org is None:
            continue
        if org.environment is not OrganizationEnvironment.DEMO:
            print(f"SKIP  {brand['slug']} is not tagged DEMO; refusing to touch it")
            continue
        db.delete(org)
    db.commit()


def main(argv: Sequence[str] | None = None, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--reset-demo",
        action="store_true",
        help="delete and recreate the 4 demo brands (never touches a PRODUCTION organization)",
    )
    parser.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help="required to run this against a production environment",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.is_production and not args.i_know_what_im_doing:
        print(
            "FAIL  refusing to run against a production environment without --i-know-what-im-doing"
        )
        return 2

    with session_factory() as db:
        if args.reset_demo:
            _reset_demo_brands(db)

        for brand in BRANDS:
            org = _get_or_create_brand(db, brand)
            counts = seed_operations(db, org.id)
            print(
                f"OK    {brand['name']} ({org.slug}): {counts['customers']} customers, "
                f"{counts['orders']} orders, {counts['issues_created']} issues created, "
                f"{counts['issues_updated']} updated"
            )

    print(
        f"OK    demo credentials use the password {DEMO_PASSWORD!r} (see docs/demo-environments.md)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
