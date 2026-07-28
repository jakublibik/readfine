"""Web routes for the settings stats page and its partials (feeds, AI cost)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_ai_enabled
from app.database import get_db
from app.models.user import User
from app.services.stats_service import (
    get_ai_cost_stats,
    get_ai_stats,
    get_feed_stats,
    get_label_stats,
    get_reading_stats,
)
from app.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/stats", response_class=HTMLResponse)
async def settings_stats(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.settings import AppSettings as _AS
    app_ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    reading = await get_reading_stats(user.id, db)
    labels = await get_label_stats(user.id, db)
    ai = await get_ai_stats(user.id, db) if app_ai_on else None
    return templates.TemplateResponse(request, "settings/stats.html", {
        "reading": reading,
        "labels": labels,
        "ai": ai,
        "ai_enabled": bool(app_ai_on),
    })


@router.get("/feeds-stats-partial", response_class=HTMLResponse)
async def settings_feeds_stats_partial(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.settings import AppSettings as _AS
    app_ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    feed_stats = await get_feed_stats(user.id, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_stats.html", {
        "feed_stats": feed_stats,
        "ai_enabled": bool(app_ai_on),
    })


@router.get("/ai/cost-partial", response_class=HTMLResponse)
async def settings_ai_cost_partial(
    request: Request,
    days: int = 30,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    days = 7 if days == 7 else 30
    cost_stats = await get_ai_cost_stats(user.id, db, days=days)
    return templates.TemplateResponse(request, "settings/partials/ai_cost_table.html", {
        "cost_stats": cost_stats,
        "active_days": days,
    })
