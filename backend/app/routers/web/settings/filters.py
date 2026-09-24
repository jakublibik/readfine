"""Web routes for filter CRUD, testing, and retroactive apply in settings."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.feed import Folder
from app.models.settings import AppSettings
from app.models.user import User, UserSettings
from app.schemas.filter import FilterActionCreate, FilterConditionCreate, FilterCreate, FilterUpdate
from app.services.feed import list_user_feeds
from app.services.filter_service import (
    apply_filter_retroactively,
    create_filter,
    delete_filter,
    get_filter,
    list_filters,
    preview_filter_retroactive,
    test_filter,
    update_filter,
)
from app.services.folder_service import FOLDER_ORDER_DEFAULT, folder_order_clause
from app.services.label_service import list_labels
from app.templating import templates
from app.utils.parsing import safe_int

router = APIRouter(prefix="/settings", tags=["settings"])


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
        "score_sources": await _user_score_sources(user, db),
    })


@router.get("/filters/new", response_class=HTMLResponse)
async def settings_filter_new(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _filter_form_context(user, db)
    ctx["filter"] = None
    return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)


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
    ctx = await _filter_form_context(user, db)
    ctx["filter"] = f
    return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)


async def _user_score_sources(user, db) -> list[str]:
    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    user_s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    return score_sources(app_s, user_s)


async def _filter_form_context(user, db):
    labels = await list_labels(user, db)
    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    user_s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    folder_order = user_s.folder_order if user_s else FOLDER_ORDER_DEFAULT
    user_feeds = await list_user_feeds(user, db, folder_order=folder_order)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(*folder_order_clause(folder_order))
    )
    return {
        "labels": labels,
        "user_feeds": user_feeds,
        "folders": folders_result.scalars().all(),
        "score_sources": score_sources(app_s, user_s),
    }


# The editor's "Score" field and its source, against the stored condition field.
SCORE_SOURCE_FIELDS = {"ai": "ai_score", "basic": "basic_score", "relevance": "relevance_score"}


def score_sources(app_s, user_s) -> list[str]:
    """Score sources a new condition may pick: the scorers this reader has running.

    A saved condition keeps its source even when that scorer is off (the editor
    shows it marked as off), so turning AI scoring off never rewrites a filter.
    """
    ai = bool(app_s and app_s.ai_enabled and user_s and user_s.ai_scoring_enabled_default)
    basic = bool(user_s and user_s.basic_scoring_enabled and (user_s.relevance_terms or "").strip())
    sources = []
    if ai:
        sources.append("ai")
    if basic:
        sources.append("basic")
    if ai or basic:
        sources.append("relevance")
    return sources


@router.post("/filters", response_class=HTMLResponse)
async def settings_filter_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    save_and_test = form.get("save_and_test") == "1"
    payload = _parse_filter_form(form)
    try:
        f = await create_filter(user.id, payload, db)
    except ValueError as e:
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": None, "form_values": payload, "error": str(e)})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)
    if save_and_test:
        test_result = await test_filter(user.id, f.id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": f, "test_result": test_result})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)
    return RedirectResponse("/settings/filters", status_code=303)


@router.post("/filters/{filter_id}/edit", response_class=HTMLResponse)
async def settings_filter_update(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    save_and_test = form.get("save_and_test") == "1"
    payload = _parse_filter_form(form)
    try:
        await update_filter(user.id, filter_id, FilterUpdate(**payload.model_dump()), db)
    except ValueError as e:
        existing = await get_filter(user.id, filter_id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": existing, "form_values": payload, "error": str(e)})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)
    if save_and_test:
        updated = await get_filter(user.id, filter_id, db)
        test_result = await test_filter(user.id, filter_id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": updated, "test_result": test_result})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)
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
        "score_sources": await _user_score_sources(user, db),
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


@router.post("/filters/{filter_id}/apply/preview", response_class=HTMLResponse)
async def settings_filter_apply_preview(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    preview = await preview_filter_retroactive(user.id, filter_id, db)
    if preview is None:
        return HTMLResponse("<p class='text-red-500'>Filter not found.</p>", status_code=404)
    return templates.TemplateResponse(request, "settings/partials/filter_apply_preview.html", {
        "filter_id": filter_id,
        **preview,
    })


@router.post("/filters/{filter_id}/apply", response_class=HTMLResponse)
async def settings_filter_apply(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    # Whitelist mode; fall back to the conservative "skip" (no scoring) on anything else.
    enqueue_scoring = form.get("mode") == "score"
    matched, changed, scoring_queued = await apply_filter_retroactively(
        user.id, filter_id, db, enqueue_scoring=enqueue_scoring
    )
    return templates.TemplateResponse(request, "settings/partials/filter_apply_result.html", {
        "matched": matched,
        "changed": changed,
        "scoring_queued": scoring_queued,
    })


def _parse_filter_form(form) -> FilterCreate:
    """Parse multi-value filter form into FilterCreate."""
    conditions = []
    fields = form.getlist("cond_field")
    operators = form.getlist("cond_operator")
    values = form.getlist("cond_value")
    positions = form.getlist("cond_position")
    sources = form.getlist("cond_source")
    for i, (field, op, val) in enumerate(zip(fields, operators, values)):
        val = val.strip()
        if field == "score":
            source = sources[i] if i < len(sources) else ""
            field = SCORE_SOURCE_FIELDS.get(source, "")
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

    scope_include = [v for v in form.getlist("scope_include") if v]
    scope_except = [v for v in form.getlist("scope_except") if v]

    return FilterCreate(
        name=form.get("name", ""),
        is_active=form.get("is_active") == "true",
        match_operator=form.get("match_operator", "AND"),
        position=safe_int(form.get("position"), 0),
        stop_on_match=form.get("stop_on_match") == "true",
        scope_include=scope_include,
        scope_except=scope_except,
        conditions=conditions,
        actions=actions,
    )
