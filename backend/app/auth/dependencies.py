from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token, hash_token
from app.database import get_db
from app.models.user import User
from app.models.auth import ApiToken

bearer_scheme = HTTPBearer(auto_error=False)


async def _get_user_by_id(user_id: int, db: AsyncSession) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    return result.scalar_one_or_none()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolves current user from session (web) or Bearer token (API)."""
    # 1. Try session cookie (web)
    user_id = request.session.get("user_id")
    if user_id:
        user = await _get_user_by_id(user_id, db)
        if user:
            return user

    # 2. Try Bearer token (API)
    if credentials:
        token = credentials.credentials

        # Try JWT first
        payload = decode_access_token(token)
        if payload:
            user = await _get_user_by_id(int(payload["sub"]), db)
            if user:
                return user

        # Try API token (hashed lookup)
        token_hash = hash_token(token)
        result = await db.execute(
            select(ApiToken).where(
                ApiToken.token_hash == token_hash,
                ApiToken.revoked_at == None,
            )
        )
        api_token = result.scalar_one_or_none()
        if api_token:
            user = await _get_user_by_id(api_token.user_id, db)
            if user:
                api_token.last_used_at = datetime.now(timezone.utc)
                await db.commit()
                return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def get_api_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Bearer-only auth for API endpoints. Session cookies are not accepted."""
    if credentials:
        token = credentials.credentials

        payload = decode_access_token(token)
        if payload:
            user = await _get_user_by_id(int(payload["sub"]), db)
            if user:
                return user

        token_hash = hash_token(token)
        result = await db.execute(
            select(ApiToken).where(
                ApiToken.token_hash == token_hash,
                ApiToken.revoked_at == None,
            )
        )
        api_token = result.scalar_one_or_none()
        if api_token:
            user = await _get_user_by_id(api_token.user_id, db)
            if user:
                api_token.last_used_at = datetime.now(timezone.utc)
                await db.commit()
                return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user
