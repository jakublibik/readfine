from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.auth.security import create_access_token, verify_password_async
from app.config import settings as app_settings_config
from app.database import get_db
from app.rate_limit import (
    check_login_lockout,
    clear_failed_logins,
    get_client_ip,
    limiter,
    record_failed_login,
)
from app.models.user import User
from app.utils.email_validate import normalize_email
from app.schemas.user import LoginRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["api-auth"])


@router.post("/token")
@limiter.limit(app_settings_config.rate_limit_login)
async def get_token(
    request: Request,
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange credentials for a short-lived JWT (60 min), invalidated on password change.

    For long-lived, individually revocable programmatic access, create an API token
    in Settings → API Tokens and send it as a Bearer header instead.
    """
    email = normalize_email(payload.email)
    # Same checks as web login, so the API is no way around the lockout or verification.
    ip = get_client_ip(request)
    if check_login_lockout(ip, email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in 15 minutes.",
        )

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # No user still costs a (dummy) verify: don't leak existence via timing.
    if not await verify_password_async(payload.password, user.password_hash if user else None):
        record_failed_login(ip, email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email not verified")

    clear_failed_logins(ip, email)
    token = create_access_token(user.id, user.role, user.session_token_version)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_api_user)):
    return user
