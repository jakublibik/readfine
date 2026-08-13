"""Catch me up: the digest page and its generation, saved configs and the
scheduled-briefing settings attached to them."""
import html as html_module
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.services.ai_jobs import ai_enabled_globally
from app.services.label_service import list_labels
from app.templating import templates
from app.utils.markdown import md_render as _md_render

from .common import _catchup_available

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-app"])


def _scoring_available(ai_on: bool, settings: UserSettings | None) -> bool:
    if not ai_on or not settings:
        return False
    return bool(settings.ai_scoring_enabled_default)


@router.get("/app/catch-me-up", response_class=HTMLResponse)
async def catchup_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.settings import AppSettings as _AS
    from app.models.user import UserCatchupConfig
    from app.services.ai_service import _DEFAULT_CATCHUP_PROMPT
    from app.services.feed import list_user_feeds

    ai_on = bool(await ai_enabled_globally(db))
    settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()

    if not _catchup_available(ai_on, settings):
        return templates.TemplateResponse(request, "app/catch_me_up.html", {
            "user": user,
            "catchup_available": False,
            "ai_scoring_available": False,
            "user_feeds": [],
            "saved_configs": [],
        })

    user_feeds_data = await list_user_feeds(user, db)
    user_labels = await list_labels(user, db)
    saved_configs = (await db.execute(
        select(UserCatchupConfig)
        .where(UserCatchupConfig.user_id == user.id)
        .order_by(UserCatchupConfig.name)
    )).scalars().all()

    # Period descriptions with user timezone
    from app.services.catchup_service import _period_to_start_dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    tz_str = settings.timezone if settings else "UTC"
    try:
        _tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        _tz = ZoneInfo("UTC")

    from app.utils.formats import current_viewer_format, format_date_parts, resolve_profile
    _fmt_profile = resolve_profile(current_viewer_format.get())

    def _period_desc(period: str) -> str:
        start = _period_to_start_dt(period, tz_str).astimezone(_tz)
        today_date = datetime.now(_tz).date()
        days = (today_date - start.date()).days + 1
        day_label = f"{days} day{'s' if days != 1 else ''}"
        return f"from {format_date_parts(start, _fmt_profile, with_year=False)} 00:00 · {day_label}"

    period_descs = {p: _period_desc(p) for p in ("today", "yesterday", "7days")}

    smtp_cfg = (await db.execute(
        select(
            _AS.smtp_host,
            _AS.smtp_user,
            _AS.smtp_from_email,
        ).where(_AS.id == 1)
    )).one_or_none()
    smtp_available = bool(smtp_cfg and smtp_cfg[0] and smtp_cfg[2])

    return templates.TemplateResponse(request, "app/catch_me_up.html", {
        "user": user,
        "catchup_available": True,
        "ai_scoring_available": _scoring_available(ai_on, settings),
        "user_feeds": user_feeds_data,
        "labels": user_labels,
        "saved_configs": saved_configs,
        "default_catchup_prompt": _DEFAULT_CATCHUP_PROMPT,
        "period_descs": period_descs,
        "smtp_available": smtp_available,
    })


@router.get("/htmx/catch-me-up/estimate", response_class=HTMLResponse)
async def htmx_catchup_estimate(
    request: Request,
    period: str = Query("7days"),
    filter_status: str = Query("all"),
    label_filter: str | None = Query(None),
    filter_score_min: float | None = Query(None),
    scope_include: str | None = Query(None),
    article_limit: int = Query(500),
    model_slot: str = Query("fast"),
    # Same checkbox semantics as the generate route: an unchecked box submits no
    # value at all, so a missing param means off, not the default.
    include_snippet: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the article count and the cost estimate for the current form state.

    Both lines come from one count so they can't disagree, and so a change to the
    form is one request rather than two.
    """
    article_limit = max(1, min(article_limit, 500))
    from app.services.catchup_service import count_catchup_articles

    settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()
    tz_str = settings.timezone if settings else "UTC"

    count = await count_catchup_articles(
        user_id=user.id, tz_str=tz_str, db=db,
        period=period, scope_include=scope_include,
        filter_status=filter_status, label_filter=label_filter,
        filter_score_min=filter_score_min / 100 if filter_score_min is not None else None,
    )
    if count > article_limit:
        count_html = f'<span>{count} articles <span class="text-gray-400">({article_limit} will be used)</span></span>'
    else:
        count_html = f'<span>{count} articles</span>'

    cost_html = await _cost_line(user, db, min(count, article_limit), model_slot, include_snippet)
    return HTMLResponse(f'<div>{count_html}</div><div>{cost_html}</div>')


async def _cost_line(
    user: User,
    db: AsyncSession,
    effective_count: int,
    model_slot: str,
    include_snippet: str | None,
) -> str:
    """Cost estimate for `effective_count` articles, or a hint / empty string when
    there is no price to show. The article count above it renders either way."""
    from app.services.ai_service import get_ai_client
    from app.services.catchup_service import estimate_catchup_tokens
    from app.services.stats_service import _calc_cost

    try:
        _client, provider, model = await get_ai_client(user.id, model_slot, db)
    except Exception:
        return '<span class="text-gray-400">Configure AI model in settings to see cost estimate</span>'

    input_tokens, output_tokens = estimate_catchup_tokens(effective_count, include_snippet == "true")
    cost, cost_estimated = _calc_cost(model, provider, input_tokens, output_tokens)
    if cost is None:
        return ""

    slot_label = "fast" if model_slot == "fast" else "quality"
    est_note = " · model not in price list, approximated" if cost_estimated else ""
    from app.utils.formats import format_number
    return (
        f'<span class="text-gray-500 text-sm">Estimated cost: ~${format_number(cost, 4)} '
        f'<span class="text-gray-400">({effective_count} articles × {slot_label} model{est_note})</span></span>'
    )


@router.post("/htmx/catch-me-up/generate", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_catchup)
async def htmx_catchup_generate(
    request: Request,
    period: str = Form("7days"),
    filter_status: str = Form("all"),
    label_filter: str | None = Form(None),
    filter_score_min: float | None = Form(None),
    scope_include: str | None = Form(None),
    article_limit: int = Form(500),
    model_slot: str = Form("fast"),
    custom_prompt: str | None = Form(None),
    include_snippet: str | None = Form(None),
    config_id: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    include_snippet_bool = include_snippet == 'true'
    article_limit = max(1, min(article_limit, 500))
    from app.models.user import CatchupLog
    from app.services.ai_service import catch_me_up, get_ai_client
    from app.services.catchup_service import (
        apply_catchup_limit, build_articles_meta, fetch_catchup_articles,
        populate_snippet_sources, validate_scope,
    )

    ai_on = bool(await ai_enabled_globally(db))
    settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()

    if not _catchup_available(ai_on, settings):
        return HTMLResponse('<div class="text-red-600 text-sm p-4">Catch me up is not available.</div>')

    # Validate scope ownership
    try:
        await validate_scope(user.id, scope_include, db)
    except ValueError as exc:
        return HTMLResponse(f'<div class="text-red-600 text-sm p-4">Invalid scope: {html_module.escape(str(exc)[:200])}</div>')

    tz_str = settings.timezone if settings else "UTC"
    scoring_available = _scoring_available(ai_on, settings)

    try:
        articles = await fetch_catchup_articles(
            user_id=user.id, tz_str=tz_str, db=db,
            period=period, scope_include=scope_include,
            filter_status=filter_status, label_filter=label_filter,
            filter_score_min=filter_score_min / 100 if filter_score_min is not None else None,
        )
    except Exception as exc:
        logger.exception("catchup: fetch failed for user %d", user.id)
        return HTMLResponse(f'<div class="text-red-600 text-sm p-4">Could not fetch articles: {html_module.escape(str(exc)[:200])}</div>')

    if not articles:
        return HTMLResponse('<div class="text-gray-500 text-sm p-4">No articles match the selected filters.</div>')

    sampled = apply_catchup_limit(articles, article_limit, scoring_available)
    if include_snippet_bool:
        await populate_snippet_sources(sampled, user.id, db)
    articles_meta = build_articles_meta(sampled, include_snippet_bool)

    try:
        client, provider, model = await get_ai_client(user.id, model_slot, db)
        prompt = custom_prompt.strip() if custom_prompt and custom_prompt.strip() else None
        text, input_tokens, output_tokens = await catch_me_up(
            articles_meta=articles_meta,
            period=period,
            client=client,
            provider=provider,
            model=model,
            custom_prompt=prompt,
        )
    except Exception as exc:
        logger.exception("catchup: AI generation failed for user %d", user.id)
        return HTMLResponse(f'<div class="text-red-600 text-sm p-4">Could not generate digest: {html_module.escape(str(exc)[:200])}</div>')

    # Log the run
    log = CatchupLog(
        user_id=user.id,
        config_id=config_id,
        article_count=len(sampled),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model,
        provider=provider,
        model_slot=model_slot,
    )
    db.add(log)
    await db.commit()

    rendered = _md_render(text)
    return HTMLResponse(
        f'<div class="prose prose-sm dark:prose-invert max-w-none">{rendered}</div>'
    )


# ── Catchup config CRUD ───────────────────────────────────────────────────────

async def _catchup_configs_list_html(request: Request, user_id: int, db: AsyncSession) -> HTMLResponse:
    from app.models.user import UserCatchupConfig
    from app.models.settings import AppSettings as _AS
    configs = (await db.execute(
        select(UserCatchupConfig)
        .where(UserCatchupConfig.user_id == user_id)
        .order_by(UserCatchupConfig.name)
    )).scalars().all()
    smtp_cfg = (await db.execute(
        select(_AS.smtp_host, _AS.smtp_from_email).where(_AS.id == 1)
    )).one_or_none()
    smtp_available = bool(smtp_cfg and smtp_cfg[0] and smtp_cfg[1])
    return templates.TemplateResponse(request, "app/partials/catchup_configs_list.html", {
        "saved_configs": configs,
        "smtp_available": smtp_available,
    })


@router.post("/htmx/catchup-configs", response_class=HTMLResponse)
async def htmx_catchup_config_create(
    request: Request,
    name: str = Form(...),
    scope_include: str | None = Form(None),
    period: str = Form("7days"),
    filter_status: str = Form("all"),
    label_filter: str | None = Form(None),
    filter_score_min: float | None = Form(None),
    article_limit: int = Form(500),
    model_slot: str = Form("fast"),
    custom_prompt: str | None = Form(None),
    include_snippet: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    include_snippet_bool = include_snippet == 'true'
    article_limit = max(1, min(article_limit, 500))
    from app.models.user import UserCatchupConfig
    from app.services.catchup_service import validate_scope

    try:
        await validate_scope(user.id, scope_include, db)
    except ValueError as exc:
        return HTMLResponse(f'<div class="text-red-600 text-sm">Invalid scope: {html_module.escape(str(exc)[:200])}</div>', status_code=422)

    clean_name = name.strip()[:100]
    if not clean_name:
        return HTMLResponse(
            '<p class="text-yellow-600 text-sm mt-1">Configuration name cannot be empty.</p>',
            status_code=200,
        )
    # Upsert by (name, period) — allows same name with different period
    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.user_id == user.id,
            UserCatchupConfig.name == clean_name,
            UserCatchupConfig.period == period,
        )
    )).scalar_one_or_none()

    score_min_stored = filter_score_min / 100 if filter_score_min is not None else None
    if config:
        config.scope_include = scope_include
        config.period = period
        config.filter_status = filter_status
        config.label_filter = label_filter
        config.filter_score_min = score_min_stored
        config.article_limit = article_limit
        config.model_slot = model_slot
        config.custom_prompt = custom_prompt
        config.include_snippet = include_snippet_bool
        config.updated_at = datetime.now(timezone.utc)
    else:
        config = UserCatchupConfig(
            user_id=user.id,
            name=clean_name,
            scope_include=scope_include,
            period=period,
            filter_status=filter_status,
            label_filter=label_filter,
            filter_score_min=score_min_stored,
            article_limit=article_limit,
            model_slot=model_slot,
            custom_prompt=custom_prompt,
            include_snippet=include_snippet_bool,
        )
        db.add(config)

    await db.commit()
    return await _catchup_configs_list_html(request, user.id, db)


@router.put("/htmx/catchup-configs/{config_id}", response_class=HTMLResponse)
async def htmx_catchup_config_update(
    config_id: int,
    request: Request,
    name: str = Form(...),
    scope_include: str | None = Form(None),
    period: str = Form("7days"),
    filter_status: str = Form("all"),
    label_filter: str | None = Form(None),
    filter_score_min: float | None = Form(None),
    article_limit: int = Form(500),
    model_slot: str = Form("fast"),
    custom_prompt: str | None = Form(None),
    include_snippet: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    include_snippet_bool = include_snippet == 'true'
    article_limit = max(1, min(article_limit, 500))
    from app.models.user import UserCatchupConfig
    from app.services.catchup_service import validate_scope

    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.id == config_id,
            UserCatchupConfig.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not config:
        return HTMLResponse("Not found", status_code=404)

    try:
        await validate_scope(user.id, scope_include, db)
    except ValueError as exc:
        return HTMLResponse(f'<div class="text-red-600 text-sm">Invalid scope: {html_module.escape(str(exc)[:200])}</div>', status_code=422)

    config.name = name.strip()[:100]
    config.scope_include = scope_include
    config.period = period
    config.filter_status = filter_status
    config.label_filter = label_filter
    config.filter_score_min = filter_score_min / 100 if filter_score_min is not None else None
    config.article_limit = article_limit
    config.model_slot = model_slot
    config.custom_prompt = custom_prompt
    config.include_snippet = include_snippet_bool
    config.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await _catchup_configs_list_html(request, user.id, db)


@router.put("/htmx/catchup-configs/{config_id}/rename", response_class=HTMLResponse)
async def htmx_catchup_config_rename(
    config_id: int,
    request: Request,
    name: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import UserCatchupConfig

    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.id == config_id,
            UserCatchupConfig.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not config:
        return HTMLResponse("Not found", status_code=404)

    clean_name = name.strip()[:100]
    if clean_name:
        config.name = clean_name
        config.updated_at = datetime.now(timezone.utc)
        await db.commit()
    return await _catchup_configs_list_html(request, user.id, db)


@router.delete("/htmx/catchup-configs/{config_id}", response_class=HTMLResponse)
async def htmx_catchup_config_delete(
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import UserCatchupConfig

    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.id == config_id,
            UserCatchupConfig.user_id == user.id,
        )
    )).scalar_one_or_none()
    if config:
        await db.delete(config)
        await db.commit()
    return await _catchup_configs_list_html(request, user.id, db)


# ── Scheduled briefings (per catchup config) ─────────────────────────────────

@router.get("/htmx/catchup-configs/{config_id}/briefing", response_class=HTMLResponse)
async def htmx_briefing_modal_get(
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import UserCatchupConfig
    from app.models.settings import AppSettings as _AS

    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.id == config_id,
            UserCatchupConfig.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not config:
        return HTMLResponse("Not found", status_code=404)

    smtp_cfg = (await db.execute(
        select(_AS.smtp_host, _AS.smtp_from_email).where(_AS.id == 1)
    )).one_or_none()
    smtp_available = bool(smtp_cfg and smtp_cfg[0] and smtp_cfg[1])

    settings = (await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )).scalar_one_or_none()
    tz_str = (settings.timezone if settings else None) or None

    return templates.TemplateResponse(request, "app/partials/briefing_modal.html", {
        "config": config,
        "smtp_available": smtp_available,
        "tz_str": tz_str,
        "is_admin": user.role == "admin",
    })


@router.put("/htmx/catchup-configs/{config_id}/briefing", response_class=HTMLResponse)
async def htmx_briefing_modal_save(
    config_id: int,
    request: Request,
    briefing_enabled: bool = Form(False),
    briefing_interval: str = Form("daily"),
    briefing_day: int | None = Form(None),
    briefing_time: str = Form("08:00"),
    briefing_recipients: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.user import UserCatchupConfig
    from app.services.briefing_service import compute_next_send_at

    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.id == config_id,
            UserCatchupConfig.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not config:
        return HTMLResponse("Not found", status_code=404)

    def _validation_error(msg: str) -> HTMLResponse:
        return HTMLResponse(
            f'<p class="text-red-600 text-sm">{msg}</p>',
            headers={"HX-Retarget": "#briefing-form-error", "HX-Reswap": "innerHTML"},
        )

    # Validate interval
    if briefing_interval not in ("daily", "weekly"):
        return _validation_error("Invalid interval.")

    # Validate day
    if briefing_interval == "weekly":
        if briefing_day is None or not (0 <= briefing_day <= 6):
            return _validation_error("Invalid day of week.")
    else:
        briefing_day = None

    # Validate time HH:MM
    try:
        h, m = int(briefing_time[:2]), int(briefing_time[3:5])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except (ValueError, IndexError):
        return _validation_error("Invalid time format (use HH:MM).")

    # Validate extra recipients
    extra_emails: list[str] = []
    if briefing_recipients:
        raw_emails = [e.strip() for e in briefing_recipients.split(",") if e.strip()]
        if len(raw_emails) > 5:
            return _validation_error("Maximum 5 additional recipients.")
        from app.utils.email_validate import is_valid_email
        for addr in raw_emails:
            if not is_valid_email(addr):
                return _validation_error(f"Invalid email address: {html_module.escape(addr)}")
        extra_emails = raw_emails

    settings = (await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )).scalar_one_or_none()
    tz_str = (settings.timezone if settings else None) or "UTC"

    config.briefing_enabled = briefing_enabled
    config.briefing_interval = briefing_interval
    config.briefing_day = briefing_day
    config.briefing_time = briefing_time
    config.briefing_recipients = json.dumps(extra_emails) if extra_emails else None

    if briefing_enabled:
        config.briefing_next_send_at = compute_next_send_at(
            briefing_interval, briefing_day, briefing_time, tz_str
        )
        config.briefing_retry_count = 0
        config.briefing_last_error = None
    else:
        config.briefing_next_send_at = None
        config.briefing_retry_count = 0

    await db.commit()
    response = await _catchup_configs_list_html(request, user.id, db)
    response.headers["HX-Trigger"] = "closeBriefingModal"
    return response


@router.post("/htmx/catchup-configs/{config_id}/briefing/test", response_class=HTMLResponse)
@limiter.limit("1/minute")
async def htmx_briefing_test_send(
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import smtplib
    from app.models.user import UserCatchupConfig, UserSettings
    from app.models.settings import AppSettings as _AS
    from app.services.briefing_service import send_briefing

    config = (await db.execute(
        select(UserCatchupConfig).where(
            UserCatchupConfig.id == config_id,
            UserCatchupConfig.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not config:
        return HTMLResponse("Not found", status_code=404)

    app_settings = (await db.execute(select(_AS).where(_AS.id == 1))).scalar_one_or_none()
    if not app_settings or not app_settings.ai_enabled:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">AI is disabled. Enable it in admin settings.</p>'
        )
    if not app_settings.smtp_host or not app_settings.smtp_from_email:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Email sending is not configured. Set up SMTP in Admin → Settings.</p>'
        )

    user_settings = (await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )).scalar_one_or_none()
    user.settings = user_settings

    try:
        await send_briefing(config, user, db, app_settings, test_mode=True)
    except smtplib.SMTPException as exc:
        return HTMLResponse(
            f'<p class="text-red-600 text-sm">SMTP error: {html_module.escape(str(exc)[:200])}</p>'
        )
    except Exception as exc:
        return HTMLResponse(
            f'<p class="text-red-600 text-sm">Error: {html_module.escape(str(exc)[:200])}</p>'
        )

    return HTMLResponse(
        '<p class="text-green-600 text-sm font-medium" id="briefing-test-ok">Test briefing sent successfully.</p>'
        '<script>setTimeout(()=>document.getElementById("briefing-test-ok")?.remove(),5000)</script>'
    )
