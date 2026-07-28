"""Web routes for API token management in settings."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import generate_token, hash_token
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.auth import ApiToken
from app.models.user import User
from app.rate_limit import limiter
from app.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])


async def _list_tokens(user_id: int, db: AsyncSession) -> list[ApiToken]:
    result = await db.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at == None)
        .order_by(ApiToken.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/tokens", response_class=HTMLResponse)
async def settings_tokens(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tokens = await _list_tokens(user.id, db)
    return templates.TemplateResponse(request, "settings/tokens.html", {"tokens": tokens})


@router.post("/tokens", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_api_tokens)
async def settings_tokens_create(
    request: Request,
    name: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = name.strip()
    if not name:
        tokens = await _list_tokens(user.id, db)
        return templates.TemplateResponse(request, "settings/tokens.html", {
            "tokens": tokens,
            "error": "Token name cannot be empty.",
        }, status_code=422)

    raw_token = generate_token()
    token_prefix = raw_token[:8]
    token_hash = hash_token(raw_token)

    db.add(ApiToken(
        user_id=user.id,
        name=name,
        token_hash=token_hash,
        token_prefix=token_prefix,
    ))
    await db.commit()

    tokens = await _list_tokens(user.id, db)
    return templates.TemplateResponse(request, "settings/tokens.html", {
        "tokens": tokens,
        "new_token": raw_token,
        "new_token_name": name,
    })


@router.post("/tokens/{token_id}/revoke", response_class=HTMLResponse)
async def settings_tokens_revoke(
    token_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user.id)
    )
    token = result.scalar_one_or_none()
    if token and token.revoked_at is None:
        token.revoked_at = datetime.now(timezone.utc)
        await db.commit()

    tokens = await _list_tokens(user.id, db)
    return templates.TemplateResponse(request, "settings/partials/tokens_list.html", {"tokens": tokens})
