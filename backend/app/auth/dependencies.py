from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token, hash_token
from app.database import get_db
from app.models.user import User
from app.models.auth import ApiToken
from app.utils.datetime_format import current_viewer_tz
from app.utils.formats import current_viewer_format
from app.utils.request_context import current_viewer_is_admin, current_viewer_ai_error

bearer_scheme = HTTPBearer(auto_error=False)


async def _get_user_by_id(user_id: int, db: AsyncSession) -> User | None:
    result = await db.execute(
        select(User)
        .options(selectinload(User.settings))
        .where(User.id == user_id, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        # Carry the viewer's timezone for server-side date formatting.
        tz = user.settings.timezone if user.settings else None
        current_viewer_tz.set(tz or "UTC")
        # Carry the viewer's number/date format profile.
        fmt = user.settings.format_profile if user.settings else None
        current_viewer_format.set(fmt or "iso")
        # Carry admin status for template-level cross-navigation links.
        current_viewer_is_admin.set(user.role == "admin")
        # Carry unresolved-AI-error status for the nav badge.
        current_viewer_ai_error.set(bool(user.settings and user.settings.last_ai_error))
    return user


async def _auth_by_bearer(
    credentials: HTTPAuthorizationCredentials | None, db: AsyncSession
) -> User | None:
    """Resolve a user from a Bearer credential: JWT first, then a hashed API token.

    Returns None when there is no credential or it doesn't resolve to an active
    user. Shared by both the web (session-or-bearer) and API (bearer-only) deps so
    the token-verification path exists in exactly one place.
    """
    if not credentials:
        return None
    token = credentials.credentials

    # Try JWT first
    payload = decode_access_token(token)
    if payload:
        user = await _get_user_by_id(int(payload["sub"]), db)
        if user and payload.get("tv", 0) == user.session_token_version:
            return user

    # Then API token (hashed lookup)
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
    return None


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
        if user and request.session.get("tv", 0) == user.session_token_version:
            return user

    # 2. Try Bearer token (API)
    user = await _auth_by_bearer(credentials, db)
    if user is not None:
        return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def get_api_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Bearer-only auth for API endpoints. Session cookies are not accepted."""
    user = await _auth_by_bearer(credentials, db)
    if user is not None:
        return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


async def require_ai_enabled(db: AsyncSession = Depends(get_db)) -> None:
    from app.services.ai_jobs import ai_enabled_globally
    if not await ai_enabled_globally(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="AI features are disabled by the administrator")
