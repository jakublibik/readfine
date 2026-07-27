"""Shared helpers used across the app sub-routers.

Only helpers referenced by 2+ areas live here; single-area helpers stay in their
own module.
"""
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UserSettings
from app.services.ai_jobs import ai_enabled_globally

_BADGE_UNREAD = '<span class="mark-read-badge ml-auto flex-shrink-0 text-xs font-medium bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full">{}</span>'
_BADGE_TOTAL  = '<span class="mark-read-badge ml-auto flex-shrink-0 text-xs text-gray-400 px-1.5 py-0.5">{}</span>'


def _badge_html(unread: int, total: int) -> str:
    return _BADGE_UNREAD.format(unread) if unread > 0 else _BADGE_TOTAL.format(total)


def _catchup_available(ai_on: bool, settings: UserSettings | None) -> bool:
    if not ai_on or not settings:
        return False
    return bool(settings.ai_fast_provider or settings.ai_quality_provider)


async def _ai_availability(settings: UserSettings | None, db: AsyncSession) -> SimpleNamespace:
    """AI feature gates for the current user: the admin kill-switch AND a configured
    quality model (``quality``), plus the chat and catch-me-up sub-gates."""
    ai_on = bool(await ai_enabled_globally(db))
    quality = bool(ai_on and settings and settings.ai_quality_provider and settings.ai_quality_model)
    return SimpleNamespace(
        ai_on=ai_on,
        quality=quality,
        chat=bool(quality and getattr(settings, "ai_chat_enabled", False)),
        catchup=_catchup_available(ai_on, settings),
    )
