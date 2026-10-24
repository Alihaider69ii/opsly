from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)
from app.db.models import User
from app.schemas.auth import TokenResponse
from app.schemas.user import UserOut


class AuthenticationError(Exception):
    """Credentials or token rejected. Deliberately carries no detail about why."""


class EmailAlreadyRegistered(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


def register_user(db: Session, *, email: str, password: str, full_name: str | None) -> User:
    email = normalize_email(email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise EmailAlreadyRegistered
    user = User(email=email, password_hash=hash_password(password), full_name=full_name or None)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent registration of the same email
        db.rollback()
        raise EmailAlreadyRegistered from None
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    # Always run one Argon2 verification so unknown emails cost the same as wrong passwords.
    password_ok = verify_password(password, user.password_hash if user else dummy_password_hash())
    if user is None or not password_ok or not user.is_active:
        raise AuthenticationError
    return user


def login(db: Session, *, email: str, password: str) -> TokenResponse:
    user = authenticate_user(db, email=email, password=password)
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=get_settings().access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


def get_user_from_token(db: Session, token: str) -> User:
    try:
        user_id = decode_access_token(token)
    except InvalidTokenError:
        raise AuthenticationError from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError
    return user
