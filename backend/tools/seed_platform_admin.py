"""Create or update the platform admin user from PLATFORM_ADMIN_EMAIL / PLATFORM_ADMIN_PASSWORD
(see app/core/config.py), or explicit --email/--password overrides.

    cd backend
    python -m tools.seed_platform_admin
    python -m tools.seed_platform_admin --email founder@bizflow.test --password "..."

Idempotent: re-running with the same email updates that user's password and sets
is_platform_admin=True rather than creating a duplicate. Never prints the password. Refuses to
run against a production environment unless --i-know-what-im-doing is passed, since this seed
is meant for local/demo use (brief section 5).
"""

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.database import SessionLocal
from app.db.models import User


def ensure_platform_admin(
    db, *, email: str, password: str, full_name: str = "Platform Admin"
) -> User:
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, password_hash=hash_password(password), full_name=full_name)
        db.add(user)
    else:
        user.password_hash = hash_password(password)
    user.is_platform_admin = True
    user.is_active = True
    db.commit()
    db.refresh(user)
    return user


def main(argv: Sequence[str] | None = None, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--email", help="overrides PLATFORM_ADMIN_EMAIL")
    parser.add_argument("--password", help="overrides PLATFORM_ADMIN_PASSWORD")
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

    email = args.email or settings.platform_admin_email
    password = args.password or (
        settings.platform_admin_password.get_secret_value()
        if settings.platform_admin_password
        else None
    )
    if not email or not password:
        print(
            "FAIL  set PLATFORM_ADMIN_EMAIL and PLATFORM_ADMIN_PASSWORD (.env) or pass "
            "--email/--password"
        )
        return 2

    with session_factory() as db:
        user = ensure_platform_admin(db, email=email, password=password)

    print(f"OK    platform admin ready: {user.email} (user_id={user.id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
