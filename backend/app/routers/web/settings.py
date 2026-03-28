"""Web routes for settings: feeds, folders, labels, and filters management."""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import select

from app.auth.security import hash_password, verify_password

logger = logging.getLogger(__name__)

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.feed import Folder, UserFeed
from app.models.user import User, UserSettings
from app.schemas.filter import FilterActionCreate, FilterConditionCreate, FilterCreate, FilterUpdate
from app.schemas.label import LabelCreate, LabelUpdate
from app.services.feed import list_user_feeds, subscribe, unsubscribe
from app.services.filter_service import (
    apply_filter_retroactively,
    create_filter,
    delete_filter,
    get_filter,
    list_filters,
    test_filter,
    update_filter,
)
from app.services.label_service import (
    create_label,
    delete_label,
    list_labels,
    update_label,
)

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


def _safe_int(value, default=None) -> int | None:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


# ── Labels ────────────────────────────────────────────────────────────────────

@router.get("/labels", response_class=HTMLResponse)
async def settings_labels(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/labels.html", {"labels": labels})


@router.post("/labels", response_class=HTMLResponse)
async def settings_labels_create(
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await create_label(user, LabelCreate(name=name, color=color), db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})


@router.post("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_update(
    label_id: int,
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await update_label(user, label_id, LabelUpdate(name=name, color=color), db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})


@router.delete("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_delete(
    label_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_label(user, label_id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})


# ── Feeds ─────────────────────────────────────────────────────────────────────

async def _get_feeds_context(user, db):
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return user_feeds, folders


@router.get("/feeds", response_class=HTMLResponse)
async def settings_feeds(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/feeds.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "error": None,
        "subscribe_url": "",
    })


@router.post("/feeds", response_class=HTMLResponse)
async def settings_feeds_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    url = form.get("url", "").strip()
    custom_title = form.get("custom_title", "").strip() or None
    folder_id_raw = form.get("folder_id")
    folder_id = _safe_int(folder_id_raw)

    user_feeds, folders = await _get_feeds_context(user, db)
    error = None
    try:
        await subscribe(user=user, url=url, folder_id=folder_id,
                        custom_title=custom_title, fetch_auth_user=None,
                        fetch_auth_pass=None, db=db)
        return RedirectResponse("/settings/feeds", status_code=303)
    except ValueError as e:
        error = str(e)
    except Exception as e:
        logger.error("Unexpected error during feed subscribe (url=%s): %s", url, e)
        error = "Could not subscribe to feed. Please check the URL and try again."

    # Re-fetch after failed subscribe (no commit happened)
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/feeds.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "error": error,
        "subscribe_url": url,
    })


@router.get("/feeds/{user_feed_id}/edit", response_class=HTMLResponse)
async def settings_feed_edit(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserFeed)
        .where(UserFeed.id == user_feed_id, UserFeed.user_id == user.id)
        .options(selectinload(UserFeed.feed))
    )
    uf = result.scalar_one_or_none()
    if not uf:
        return HTMLResponse("<p class='text-red-500 p-4'>Feed not found.</p>", status_code=404)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return templates.TemplateResponse(request, "settings/feed_edit.html", {
        "uf": uf,
        "folders": folders,
    })


@router.post("/feeds/{user_feed_id}/edit", response_class=HTMLResponse)
async def settings_feed_update(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserFeed)
        .where(UserFeed.id == user_feed_id, UserFeed.user_id == user.id)
        .options(selectinload(UserFeed.feed))
    )
    uf = result.scalar_one_or_none()
    if not uf:
        return HTMLResponse("<p class='text-red-500 p-4'>Feed not found.</p>", status_code=404)

    form = await request.form()
    custom_title = form.get("custom_title", "").strip() or None
    folder_id_raw = form.get("folder_id")
    folder_id = _safe_int(folder_id_raw)

    if folder_id is not None:
        folder_check = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
        )
        if not folder_check.scalar_one_or_none():
            folder_id = None

    uf.custom_title = custom_title
    uf.folder_id = folder_id
    await db.commit()
    return RedirectResponse("/settings/feeds", status_code=303)


@router.delete("/feeds/{user_feed_id}", response_class=HTMLResponse)
async def settings_feed_delete(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await unsubscribe(user, user_feed_id, db)
    except ValueError:
        pass
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


# ── Folders ───────────────────────────────────────────────────────────────────

@router.post("/folders", response_class=HTMLResponse)
async def settings_folder_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        existing = await db.execute(
            select(Folder).where(Folder.user_id == user.id, Folder.name == name)
        )
        if not existing.scalar_one_or_none():
            db.add(Folder(user_id=user.id, name=name))
            await db.commit()
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


@router.delete("/folders/{folder_id}", response_class=HTMLResponse)
async def settings_folder_delete(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if folder:
        await db.delete(folder)
        await db.commit()
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


# ── Filters ───────────────────────────────────────────────────────────────────

@router.get("/filters", response_class=HTMLResponse)
async def settings_filters(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = await list_filters(user.id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/filters.html", {
        "filters": filters,
        "labels": labels,
    })


@router.get("/filters/new", response_class=HTMLResponse)
async def settings_filter_new(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    labels = await list_labels(user, db)
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return templates.TemplateResponse(request, "settings/filter_edit.html", {
        "filter": None,
        "labels": labels,
        "user_feeds": user_feeds,
        "folders": folders,
    })


@router.get("/filters/{filter_id}/edit", response_class=HTMLResponse)
async def settings_filter_edit(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    f = await get_filter(user.id, filter_id, db)
    if not f:
        return HTMLResponse("<p class='text-red-500 p-4'>Filter not found.</p>", status_code=404)
    labels = await list_labels(user, db)
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return templates.TemplateResponse(request, "settings/filter_edit.html", {
        "filter": f,
        "labels": labels,
        "user_feeds": user_feeds,
        "folders": folders,
    })


async def _filter_form_context(user, db):
    labels = await list_labels(user, db)
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    return {"labels": labels, "user_feeds": user_feeds, "folders": folders_result.scalars().all()}


@router.post("/filters", response_class=HTMLResponse)
async def settings_filter_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    payload = _parse_filter_form(form)
    try:
        await create_filter(user.id, payload, db)
    except ValueError as e:
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": None, "error": str(e)})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx, status_code=422)
    return RedirectResponse("/settings/filters", status_code=303)


@router.post("/filters/{filter_id}/edit", response_class=HTMLResponse)
async def settings_filter_update(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    payload = _parse_filter_form(form)
    try:
        await update_filter(user.id, filter_id, FilterUpdate(**payload.model_dump()), db)
    except ValueError as e:
        existing = await get_filter(user.id, filter_id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": existing, "error": str(e)})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx, status_code=422)
    return RedirectResponse("/settings/filters", status_code=303)


@router.delete("/filters/{filter_id}", response_class=HTMLResponse)
async def settings_filter_delete(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_filter(user.id, filter_id, db)
    filters = await list_filters(user.id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/filters_list.html", {
        "filters": filters,
        "labels": labels,
    })


@router.post("/filters/{filter_id}/test", response_class=HTMLResponse)
async def settings_filter_test(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await test_filter(user.id, filter_id, db)
    if not result:
        return HTMLResponse("<p class='text-red-500'>Filter not found.</p>", status_code=404)
    return templates.TemplateResponse(request, "settings/partials/filter_test_result.html", {
        "result": result,
    })


@router.post("/filters/{filter_id}/apply", response_class=HTMLResponse)
async def settings_filter_apply(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await apply_filter_retroactively(user.id, filter_id, db)
    return templates.TemplateResponse(request, "settings/partials/filter_apply_result.html", {
        "count": count,
    })


# ── Form parsing helpers ──────────────────────────────────────────────────────

def _parse_filter_form(form) -> FilterCreate:
    """Parse multi-value filter form into FilterCreate."""
    conditions = []
    fields = form.getlist("cond_field")
    operators = form.getlist("cond_operator")
    values = form.getlist("cond_value")
    positions = form.getlist("cond_position")
    for i, (field, op, val) in enumerate(zip(fields, operators, values)):
        val = val.strip()
        if field and op and val:
            conditions.append(FilterConditionCreate(
                field=field, operator=op, value=val,
                position=int(positions[i]) if i < len(positions) else i,
            ))

    actions = []
    action_types = form.getlist("action_type")
    action_values = form.getlist("action_value")
    for a_type, a_val in zip(action_types, action_values):
        if a_type:
            actions.append(FilterActionCreate(
                action_type=a_type,
                action_value=a_val or None,
            ))

    scope_type = form.get("scope_type", "all")
    scope_feed_id_raw = form.get("scope_feed_id")
    scope_folder_id_raw = form.get("scope_folder_id")

    return FilterCreate(
        name=form.get("name", ""),
        is_active=form.get("is_active") == "true",
        match_operator=form.get("match_operator", "AND"),
        position=_safe_int(form.get("position"), 0),
        stop_on_match=form.get("stop_on_match") == "true",
        scope_type=scope_type,
        scope_feed_id=_safe_int(scope_feed_id_raw),
        scope_folder_id=_safe_int(scope_folder_id_raw),
        conditions=conditions,
        actions=actions,
    )


# ── Preferences ───────────────────────────────────────────────────────────────

_DENSITY_VALUES = {"compact", "comfortable", "summary"}
_SORT_VALUES = {"newest", "oldest"}


async def _get_or_create_settings(user: User, db: AsyncSession) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    s = result.scalar_one_or_none()
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
        await db.flush()
    return s


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

    articles_per_page = _safe_int(form.get("articles_per_page"), 50)
    if articles_per_page is not None:
        s.articles_per_page = max(10, min(200, articles_per_page))

    await db.commit()
    return templates.TemplateResponse(request, "settings/preferences.html", {
        "s": s,
        "saved": True,
    })


@router.post("/password", response_class=HTMLResponse)
async def settings_password_change(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    current = form.get("current_password", "")
    new_pw = form.get("new_password", "")
    confirm = form.get("confirm_password", "")

    s = await _get_or_create_settings(user, db)

    if not verify_password(current, user.password_hash):
        return templates.TemplateResponse(request, "settings/preferences.html", {
            "s": s,
            "pw_error": "Current password is incorrect.",
        })
    if len(new_pw) < 8:
        return templates.TemplateResponse(request, "settings/preferences.html", {
            "s": s,
            "pw_error": "New password must be at least 8 characters.",
        })
    if new_pw != confirm:
        return templates.TemplateResponse(request, "settings/preferences.html", {
            "s": s,
            "pw_error": "Passwords do not match.",
        })

    user.password_hash = hash_password(new_pw)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    await db.commit()
    return templates.TemplateResponse(request, "settings/preferences.html", {
        "s": s,
        "pw_saved": True,
    })
