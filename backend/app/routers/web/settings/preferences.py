"""Web routes for reading/display preferences in settings."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User, UserCatchupConfig
from app.services.briefing_service import compute_next_send_at
from app.templating import templates
from app.utils.datetime_format import is_valid_timezone
from app.utils.formats import is_valid_format
from app.utils.parsing import safe_int

from .common import _get_or_create_settings

router = APIRouter(prefix="/settings", tags=["settings"])

_DENSITY_VALUES = {"compact", "comfortable", "summary"}
_SORT_VALUES = {"newest", "oldest"}
_FONT_SIZE_VALUES = {"sm", "md", "lg"}
_FONT_FAMILY_VALUES = {"sans", "serif"}


async def _reschedule_briefings(user_id: int, tz_str: str, db: AsyncSession) -> None:
    """Recompute next-send time for the user's active briefings after a tz change."""
    configs = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.user_id == user_id,
            UserCatchupConfig.briefing_enabled == True,
        )
    )).scalars().all()
    for cfg in configs:
        if cfg.briefing_interval and cfg.briefing_time:
            cfg.briefing_next_send_at = compute_next_send_at(
                cfg.briefing_interval, cfg.briefing_day, cfg.briefing_time, tz_str
            )


@router.get("/preferences", response_class=HTMLResponse)
async def settings_preferences(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_or_create_settings(user, db)
    return templates.TemplateResponse(request, "settings/preferences.html", {"s": s})


@router.post("/preferences", response_class=HTMLResponse)
async def settings_preferences_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    s = await _get_or_create_settings(user, db)

    density_web = form.get("list_density_web", "comfortable")
    if density_web not in _DENSITY_VALUES:
        density_web = "comfortable"
    s.list_density_web = density_web

    density_mobile = form.get("list_density_mobile", "compact")
    if density_mobile not in _DENSITY_VALUES:
        density_mobile = "compact"
    s.list_density_mobile = density_mobile

    sort_order = form.get("default_sort_order", "newest")
    if sort_order not in _SORT_VALUES:
        sort_order = "newest"
    s.default_sort_order = sort_order

    unread_filter = form.get("unread_filter", "adaptive")
    if unread_filter not in {"show_all", "unread_only", "adaptive"}:
        unread_filter = "adaptive"
    s.unread_filter = unread_filter

    s.mark_read_on_scroll = form.get("mark_read_on_scroll") == "on"
    s.mark_read_auto_advance = form.get("mark_read_auto_advance") == "on"

    label_display = form.get("label_display", "indicator")
    if label_display not in {"none", "indicator", "dots"}:
        label_display = "indicator"
    s.label_display = label_display

    articles_per_page = safe_int(form.get("articles_per_page"), 50)
    if articles_per_page is not None:
        s.articles_per_page = max(10, min(200, articles_per_page))

    bucket_small_max = safe_int(form.get("bucket_small_max"), 640)
    bucket_medium_max = safe_int(form.get("bucket_medium_max"), 1100)
    if bucket_small_max is not None and bucket_medium_max is not None:
        bucket_small_max = max(320, min(1000, bucket_small_max))
        bucket_medium_max = max(bucket_small_max + 100, min(2000, bucket_medium_max))
        s.bucket_small_max = bucket_small_max
        s.bucket_medium_max = bucket_medium_max

    font_size = form.get("reading_font_size", "md")
    if font_size not in _FONT_SIZE_VALUES:
        font_size = "md"
    s.reading_font_size = font_size

    font_family = form.get("reading_font_family", "sans")
    if font_family not in _FONT_FAMILY_VALUES:
        font_family = "sans"
    s.reading_font_family = font_family

    tz_value = (form.get("timezone") or "").strip()
    if is_valid_timezone(tz_value) and tz_value != s.timezone:
        s.timezone = tz_value
        await _reschedule_briefings(user.id, tz_value, db)

    fmt_value = (form.get("format_profile") or "").strip()
    if is_valid_format(fmt_value):
        s.format_profile = fmt_value

    await db.commit()
    return templates.TemplateResponse(request, "settings/preferences.html", {
        "s": s,
        "saved": True,
    })
