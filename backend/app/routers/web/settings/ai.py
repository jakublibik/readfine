"""Web routes for AI settings: keys, preferences, verify, bulk summary, profile gen."""
import html as html_module
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_ai_enabled
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.article import Article
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.services.ai_service import (
    PROVIDER_DOCS_URLS,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    delete_api_key,
    generate_preference_text,
    get_ai_client,
    get_preference_strong_count,
    list_api_keys,
    save_api_key,
    scoring_model_rejection,
    verify_ai_slot,
)
from app.services.ai_profile_service import AUTO_INTERVALS, preference_auto_status
from app.services.stats_service import get_ai_cost_stats
from app.templating import templates
from app.utils.url_validator import async_validate_ai_endpoint_url

from .common import _get_or_create_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# Matches the column width of user_settings.ai_custom_base_url.
_MAX_BASE_URL_LEN = 500


async def _ai_page_context(user: User, db: AsyncSession) -> dict:
    from app.services.ai_service import _DEFAULT_SUMMARY_PROMPT, _DEFAULT_CONTEXT_PROMPT
    s = await _get_or_create_settings(user, db)
    keys = await list_api_keys(user.id, db)
    cost_stats = await get_ai_cost_stats(user.id, db, days=30)
    strong_count = await get_preference_strong_count(user.id, db)
    # Same call the scheduler job makes, so the status line cannot promise a
    # run the job would skip.
    auto_status, auto_detail = await preference_auto_status(s, db)
    # Title for the error panel's article link. Stays None once retention purge
    # clears the FK, which is why the panel treats the link as optional.
    error_article_title = None
    if s.last_ai_error and s.last_ai_error_article_id:
        error_article_title = await db.scalar(
            select(Article.title).where(Article.id == s.last_ai_error_article_id)
        )
    return {
        "s": s,
        "error_article_title": error_article_title,
        "keys": keys,
        "cost_stats": cost_stats,
        "active_days": 30,
        "providers": SUPPORTED_PROVIDERS,
        "provider_docs": PROVIDER_DOCS_URLS,
        "provider_labels": PROVIDER_LABELS,
        "pref_strong_count": strong_count,
        "pref_auto_status": auto_status,
        "pref_auto_detail": auto_detail,
        "pref_auto_intervals": AUTO_INTERVALS,
        "default_summary_prompt": _DEFAULT_SUMMARY_PROMPT,
        "default_context_prompt": _DEFAULT_CONTEXT_PROMPT,
    }


@router.get("/ai", response_class=HTMLResponse)
async def settings_ai(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    ctx = await _ai_page_context(user, db)
    return templates.TemplateResponse(request, "settings/ai.html", ctx)


@router.post("/ai/dismiss-error", response_class=HTMLResponse)
async def settings_ai_dismiss_error(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear the persisted last-AI-error record (manual dismiss). The badge and
    panel both read this field, so both disappear. If the cause persists, the
    next background AI call re-sets it and the badge returns."""
    await db.execute(
        update(UserSettings)
        .where(UserSettings.user_id == user.id)
        .values(last_ai_error=None, last_ai_error_at=None, last_ai_error_article_id=None)
    )
    await db.commit()
    # Empty body removes the panel (hx-swap=outerHTML); HX-Trigger clears any
    # nav badges still in the DOM on this page.
    return HTMLResponse("", headers={"HX-Trigger": "ai-error-dismissed"})


@router.post("/ai/keys", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def settings_ai_keys_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    form = await request.form()
    provider = (form.get("provider") or "").strip()
    api_key = (form.get("api_key") or "").strip()

    if provider not in SUPPORTED_PROVIDERS:
        ctx = await _ai_page_context(user, db)
        ctx["keys_error"] = "Unknown provider."
        return templates.TemplateResponse(request, "settings/ai.html", ctx)

    if api_key:
        await save_api_key(user.id, provider, api_key, db)
        ctx = await _ai_page_context(user, db)
        ctx["keys_saved"] = provider
    else:
        await delete_api_key(user.id, provider, db)
        ctx = await _ai_page_context(user, db)
        ctx["keys_deleted"] = provider

    return templates.TemplateResponse(request, "settings/ai.html", ctx)


async def _prefs_error(
    request: Request, user: User, db: AsyncSession, message: str, form,
) -> HTMLResponse:
    """Re-render the AI settings page with an error and the values as submitted.

    The page otherwise draws the slots from the stored record, which a rejected
    save has not touched — so the form would answer "fill in the endpoint" while
    showing the provider the user just moved away from, and the endpoint field
    would be hidden because nothing on file says custom. Everything the Models
    section holds is handed back, not just the field the message is about.
    """
    ctx = await _ai_page_context(user, db)
    ctx["prefs_error"] = message
    for slot in ("fast", "quality"):
        ctx[f"{slot}_provider_submitted"] = (form.get(f"ai_{slot}_provider") or "").strip() or None
        ctx[f"{slot}_model_submitted"] = (form.get(f"ai_{slot}_model") or "").strip() or None
    if "ai_custom_base_url" in form:
        ctx["custom_base_url_submitted"] = (form.get("ai_custom_base_url") or "").strip() or None
    return templates.TemplateResponse(request, "settings/ai.html", ctx)


# Rate limited because this route reaches out to the network: saving a new
# scoring model probes it (see scoring_model_rejection), and with the custom
# provider the address being probed is whatever the form carries. Without a limit
# a signed-in user could have the server issue POSTs to any public address as
# fast as it can submit the form. Every other route here that fetches something
# on the user's behalf is capped the same way; matched to the key form's 10/min,
# which is generous for a settings page nobody submits in a loop by hand.
@router.post("/ai/preferences", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def settings_ai_preferences_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    form = await request.form()
    s = await _get_or_create_settings(user, db)

    fast_provider = (form.get("ai_fast_provider") or "").strip() or None
    quality_provider = (form.get("ai_quality_provider") or "").strip() or None
    for provider_val in (fast_provider, quality_provider):
        if provider_val is not None and provider_val not in SUPPORTED_PROVIDERS:
            return await _prefs_error(
                request, user, db,
                f"Unknown provider '{provider_val}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}.",
                form,
            )

    # The endpoint is settled before anything is sent to it. The scoring probe
    # below is a real request to whatever address this form carries, so checking
    # the URL afterwards would leave that one request outside the SSRF check the
    # whole feature is gated on.
    # Absent from the submit means "not up for changing" rather than "clear it",
    # as elsewhere in this handler: the field is hidden while neither slot is on
    # custom, and a save made then must not throw the endpoint away.
    custom_base_url = (
        ((form.get("ai_custom_base_url") or "").strip() or None)
        if "ai_custom_base_url" in form
        else s.ai_custom_base_url
    )
    uses_custom = "custom" in (fast_provider, quality_provider)
    if uses_custom:
        # Checked before the column has to hold it: ai_custom_base_url is
        # varchar(500), and anything longer reaches Postgres as a truncation
        # error, i.e. a 500 page instead of a sentence about the field.
        if custom_base_url and len(custom_base_url) > _MAX_BASE_URL_LEN:
            return await _prefs_error(
                request, user, db,
                f"That endpoint URL is too long ({len(custom_base_url)} characters). "
                f"Maximum is {_MAX_BASE_URL_LEN}.",
                form,
            )
        if not custom_base_url:
            return await _prefs_error(
                request, user, db,
                "Enter the endpoint URL for the custom provider "
                "(for example http://localhost:11434/v1).",
                form,
            )
        try:
            await async_validate_ai_endpoint_url(custom_base_url)
        except ValueError as exc:
            return await _prefs_error(request, user, db, str(exc), form)

    fast_model = (form.get("ai_fast_model") or "").strip() or None
    # The fast slot is the one scoring runs on, and a model that always reasons has
    # nothing left of its ten tokens by the time it should answer — it would fail on
    # every article and only say so in the error banner. Ask the model itself before
    # storing it, and only when the choice actually changed, so an unrelated save
    # costs no provider call. Anything inconclusive comes back None and saves.
    # The endpoint comes from the form, not from the record: on the save that
    # first sets up a custom endpoint nothing is stored yet, and probing the
    # empty stored value would turn every such first save into a failure.
    if (fast_provider, fast_model) != (s.ai_fast_provider, s.ai_fast_model) or (
        fast_provider == "custom" and custom_base_url != s.ai_custom_base_url
    ):
        rejection = await scoring_model_rejection(
            user.id, fast_provider, fast_model, db, base_url=custom_base_url
        )
        if rejection:
            return await _prefs_error(request, user, db, rejection, form)

    s.ai_custom_base_url = custom_base_url
    s.ai_fast_provider = fast_provider
    s.ai_fast_model = fast_model
    s.ai_quality_provider = quality_provider
    s.ai_quality_model = (form.get("ai_quality_model") or "").strip() or None
    s.ai_scoring_enabled_default = form.get("ai_scoring_enabled_default") == "on"
    s.ai_summary_enabled_default = form.get("ai_summary_enabled_default") == "on"
    s.ai_chat_enabled = form.get("ai_chat_enabled") == "on"

    # Everything below belongs to scoring and is disabled in the form while
    # scoring is off. A disabled control submits nothing, so applying these
    # unconditionally would wipe the profile (and the schedule, and the score
    # toggle) the moment someone saves with scoring turned off.
    if "ai_preference_text" in form:
        pref_text = (form.get("ai_preference_text") or "").strip() or None
        if pref_text and len(pref_text) > 5000:
            ctx = await _ai_page_context(user, db)
            ctx["prefs_error"] = f"Interest profile is too long ({len(pref_text)} characters). Maximum is 5 000 characters."
            ctx["pref_text_submitted"] = pref_text
            return templates.TemplateResponse(request, "settings/ai.html", ctx)
        # Order matters: the schedule and the text arrive in the same submit. A
        # real text change stamps the timestamp (and resets the auto clock with
        # it); switching the schedule on only stamps it when the text did not.
        if pref_text != s.ai_preference_text:
            s.ai_preference_text = pref_text
            s.ai_preference_updated_at = datetime.now(timezone.utc)
            s.ai_preference_source = "manual"

    if "ai_preference_auto_days" in form:
        try:
            auto_days = int(form.get("ai_preference_auto_days") or 0)
        except (TypeError, ValueError):
            auto_days = 0
        if auto_days not in AUTO_INTERVALS:
            auto_days = 0
        if auto_days and not s.ai_preference_auto_days:
            s.ai_preference_fail_count = 0
            s.ai_preference_last_error = None
            s.ai_preference_last_error_at = None
            # Turning the schedule on must not rewrite an existing profile the
            # next morning: start the clock now and let the first run come one
            # full interval later. An empty profile keeps NULL and generates
            # right away.
            if s.ai_preference_text and s.ai_preference_updated_at is None:
                s.ai_preference_updated_at = datetime.now(timezone.utc)
        s.ai_preference_auto_days = auto_days

    if s.ai_scoring_enabled_default:
        s.ai_score_show_in_list = form.get("ai_score_show_in_list") == "on"
    _raw_limit = re.sub(r"\s", "", form.get("ai_content_limit") or "")
    _content_limit_reset = False
    try:
        _parsed_limit = int(_raw_limit) if _raw_limit else 20_000
        if not (1_000 <= _parsed_limit <= 100_000):
            raise ValueError
        s.ai_content_limit = _parsed_limit
    except (ValueError, TypeError):
        s.ai_content_limit = 20_000
        _content_limit_reset = True
    s.ai_summary_prompt = (form.get("ai_summary_prompt") or "").strip() or None
    s.ai_context_prompt = (form.get("ai_context_prompt") or "").strip() or None

    await db.commit()

    ctx = await _ai_page_context(user, db)
    ctx["prefs_saved"] = True
    if _content_limit_reset:
        ctx["content_limit_reset"] = True
    ctx["summary_banner_html"] = ""
    return templates.TemplateResponse(request, "settings/ai.html", ctx)


@router.post("/ai/verify/{slot}", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def settings_ai_verify(
    slot: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    if slot not in ("fast", "quality"):
        return HTMLResponse("Invalid slot", status_code=400)

    form = await request.form()
    provider_override = (form.get(f"ai_{slot}_provider") or "").strip() or None
    model_override = (form.get(f"ai_{slot}_model") or "").strip() or None
    # Sent along by the Models form, so Verify checks the endpoint on screen
    # rather than the one last saved.
    base_url_override = (form.get("ai_custom_base_url") or "").strip() or None
    result = await verify_ai_slot(
        user.id, slot, db, provider_override, model_override, base_url_override
    )
    # The slot's own check passes a model that reasons its way through a generous
    # budget, which is right for every use of it except scoring. Since the form
    # refuses to store such a model in the fast slot, Verify has to agree with it
    # rather than report a connection the save will then turn down.
    if result["ok"] and slot == "fast":
        s = await _get_or_create_settings(user, db)
        rejection = await scoring_model_rejection(
            user.id,
            provider_override or s.ai_fast_provider,
            model_override or result["model"],
            db,
            base_url=base_url_override or s.ai_custom_base_url,
        )
        if rejection:
            result = {"ok": False, "model": result["model"], "error": rejection}
    if result["ok"]:
        html = (
            f'<span class="text-green-600 text-sm">✓ Connected — {result["model"]}</span>'
        )
    else:
        html = (
            f'<span class="text-red-600 text-sm">✗ {html_module.escape(result["error"])}</span>'
        )
    return HTMLResponse(html)


@router.post("/ai/bulk-summary", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def settings_ai_bulk_summary(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    """Enqueue summary jobs for all starred articles without a summary."""
    from app.models.article import Article as _Article, UserArticleState as _UAS
    from app.services.ai_summary_service import enqueue_summary_job

    article_ids = (await db.scalars(
        select(_UAS.article_id).where(
            _UAS.user_id == user.id,
            _UAS.is_starred == True,
            _UAS.ai_summary == None,
        )
    )).all()

    count = 0
    for aid in article_ids:
        article = await db.scalar(select(_Article).where(_Article.id == aid))
        if article:
            created = await enqueue_summary_job(article, user.id, db)
            if created:
                count += 1

    await db.commit()
    return HTMLResponse(
        f'<div id="ai-summary-banner" class="mt-3 p-3 bg-green-50 border border-green-200 rounded text-sm text-green-800">'
        f'Summary jobs queued for <strong>{count}</strong> article{"s" if count != 1 else ""}. '
        f'They will be processed in the background within a few minutes.'
        f'</div>'
    )


@router.post("/ai/generate-preference", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_preference)
async def settings_ai_generate_preference(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    client, provider, model = await get_ai_client(user.id, "quality", db)
    if client is None:
        return HTMLResponse(
            '<span class="text-red-600 text-sm">Main model not configured.</span>'
        )
    try:
        text_result, in_tok, out_tok = await generate_preference_text(user.id, db, client, provider, model)
    except Exception as exc:
        logger.warning("generate_preference_text failed for user=%s: %s", user.id, exc)
        return HTMLResponse(
            f'<span class="text-red-600 text-sm">Error: {html_module.escape(str(exc)[:150])}</span>'
        )

    # Log token usage
    from app.models.article import AiUsageLog  # noqa: PLC0415
    db.add(AiUsageLog(
        user_id=user.id,
        operation="preference_generation",
        model_slot="quality",
        model=model,
        provider=provider,
        input_tokens=in_tok,
        output_tokens=out_tok,
    ))
    await db.commit()

    strong_count = await get_preference_strong_count(user.id, db)
    escaped = text_result.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    warning_inner = (
        f'<p class="text-xs text-amber-600 mt-1 mb-1">'
        f'Only {strong_count} article{"s" if strong_count != 1 else ""} with strong reading signals so far — '
        f'profile was supplemented with feed names. Keep reading and starring to improve accuracy.'
        f'</p>'
    ) if strong_count < 20 else ""
    return HTMLResponse(
        f'<span class="text-green-600 text-sm">Generated — review and save below.</span>'
        f'<textarea name="ai_preference_text" id="ai_preference_text" rows="4"'
        f' class="w-full border border-gray-300 rounded px-3 py-2 text-sm font-mono"'
        f' hx-swap-oob="true">{escaped}</textarea>'
        f'<div id="pref-cold-start-warning" hx-swap-oob="true">{warning_inner}</div>'
    )


@router.post("/ai/revert-preference", response_class=HTMLResponse)
async def settings_ai_revert_preference(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    """Swap the current profile with the version an automatic generation replaced.

    Swapping rather than restoring means the button works both ways, so a revert
    made by mistake is one more click away from being undone.
    """
    s = await _get_or_create_settings(user, db)
    if not s.ai_preference_prev_text:
        return HTMLResponse('<span class="text-gray-500 text-sm">No previous version stored.</span>')

    s.ai_preference_text, s.ai_preference_prev_text = s.ai_preference_prev_text, s.ai_preference_text
    s.ai_preference_updated_at = datetime.now(timezone.utc)
    s.ai_preference_source = "manual"
    await db.commit()

    escaped = html_module.escape(s.ai_preference_text or "")
    return HTMLResponse(
        f'<span class="text-green-600 text-sm">Previous version restored.</span>'
        f'<textarea name="ai_preference_text" id="ai_preference_text" rows="7"'
        f' class="w-full border border-gray-300 rounded px-3 py-2 text-sm font-mono"'
        f' hx-swap-oob="true">{escaped}</textarea>'
        f'<span id="pref-char-count" hx-swap-oob="true">{len(s.ai_preference_text or "")}</span>'
    )
