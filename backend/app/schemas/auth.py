from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, StringConstraints

from app.schemas.user import UserOut

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128  # bounds Argon2 work per request


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = (
        None
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds
    user: UserOut
