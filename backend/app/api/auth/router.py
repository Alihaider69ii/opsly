from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.core.rate_limit import rate_limit
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserOut
from app.services.auth import service
from app.services.platform import service as platform_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register"))],
)
def register(body: RegisterRequest, db: DbSession) -> UserOut:
    try:
        user = service.register_user(
            db, email=body.email, password=body.password, full_name=body.full_name
        )
    except service.EmailAlreadyRegistered:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with this email already exists"
        ) from None
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(rate_limit("login"))])
def login(body: LoginRequest, db: DbSession) -> TokenResponse:
    try:
        result = service.login(db, email=body.email, password=body.password)
    except service.AuthenticationError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if result.user.is_platform_admin:
        platform_service.record_audit(
            db, actor_user_id=result.user.id, event_type="PLATFORM_ADMIN_LOGIN"
        )
        db.commit()
    return result


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
