from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.auth.security import create_access_token, verify_password
from app.config import settings as app_settings_config
from app.database import get_db
from app.rate_limit import limiter
from app.models.user import User
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
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    token = create_access_token(user.id, user.role, user.session_token_version)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_api_user)):
    return user
