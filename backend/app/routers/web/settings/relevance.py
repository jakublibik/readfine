"""Web routes for relevance settings: the interest profile and basic scoring.

The profile lives here rather than on the AI page because both scorers read it,
and the lexical one runs without an API key or an admin who has AI switched on.
This page is therefore never hidden, unlike the AI item in the settings nav.

Generating a profile and scheduling regenerations still belong to the AI routes
(`/settings/ai/generate-preference`, `/settings/ai/revert-preference`): they call
a model. The buttons sit here because this is where the field they fill in is,
and they say so when there is no key behind them.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.ai_profile_service import (
    AUTO_INTERVALS,
    preference_auto_status,
    quality_slot_blocker,
)
from app.services.ai_service import PROVIDER_LABELS, get_preference_strong_count
from app.services.relevance_corpus_service import get_stats
from app.templating import templates

from .common import _get_or_create_settings

router = APIRouter(prefix="/settings", tags=["settings"])

PROFILE_MAX_CHARS = 5000
# Empty is a valid answer: it means "do not score". Anything else has to be long
# enough to match an article against, and two characters is not. The bar is low
# on purpose, since "AI safety" is a real profile someone might stop at, and its
# job is to catch a slip rather than to judge how someone reads.
PROFILE_MIN_CHARS = 10


async def _page_context(user: User, db: AsyncSession) -> dict:
    s = await _get_or_create_settings(user, db)
    auto_status, auto_detail = await preference_auto_status(s, db)
    # Whether the generate button is offered at all. Not "does a key exist
    # somewhere": generation runs on the main model slot, so a slot left on
    # "-- provider --", or pointed at a provider with no key, cannot generate no
    # matter how many other keys are saved.
    blocker = await quality_slot_blocker(s, db)
    return {
        "s": s,
        "active": "relevance",
        "gen_blocked": blocker[0] if blocker else None,
        "gen_blocked_detail": blocker[1] if blocker else {},
        "pref_strong_count": await get_preference_strong_count(user.id, db),
        "pref_auto_status": auto_status,
        "pref_auto_detail": auto_detail,
        "pref_auto_intervals": AUTO_INTERVALS,
        "provider_labels": PROVIDER_LABELS,
        # Nothing is scored until the nightly job has counted a corpus once, and
        # an empty score column with no explanation looks like a bug rather than
        # like the first day of an install.
        "corpus_ready": await get_stats(db) is not None,
        "profile_max_chars": PROFILE_MAX_CHARS,
    }


@router.get("/relevance", response_class=HTMLResponse)
async def settings_relevance(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return templates.TemplateResponse(
        request, "settings/relevance.html", await _page_context(user, db))


@router.post("/relevance", response_class=HTMLResponse)
async def settings_relevance_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    s = await _get_or_create_settings(user, db)

    pref_text = (form.get("ai_preference_text") or "").strip() or None
    length_error = None
    if pref_text and len(pref_text) > PROFILE_MAX_CHARS:
        length_error = (f"Interest profile is too long ({len(pref_text)} characters). "
                        f"Maximum is {PROFILE_MAX_CHARS:,} characters.".replace(",", " "))
    elif pref_text and len(pref_text) < PROFILE_MIN_CHARS:
        length_error = (f"Interest profile is too short ({len(pref_text)} characters). "
                        f"Write at least {PROFILE_MIN_CHARS} characters worth of topics, "
                        f"or leave it empty to score nothing.")
    if length_error:
        ctx = await _page_context(user, db)
        ctx["error"] = length_error
        ctx["pref_text_submitted"] = pref_text
        return templates.TemplateResponse(request, "settings/relevance.html", ctx)

    s.basic_scoring_enabled = form.get("basic_scoring_enabled") == "on"
    s.ai_score_show_in_list = form.get("ai_score_show_in_list") == "on"

    # Order matters, the schedule and the text arrive in the same submit: a real
    # text change stamps the timestamp (which is also what makes the lexical
    # backfill due), switching the schedule on only stamps it when the text did
    # not change.
    if pref_text != s.ai_preference_text:
        s.ai_preference_text = pref_text
        s.ai_preference_updated_at = datetime.now(timezone.utc)
        s.ai_preference_source = "manual"

    # Absent means "not up for changing", not "off". The schedule is only on the
    # page while a model can actually be called, so applying it unconditionally
    # would silently clear the interval of anyone who saves this page after
    # switching the main model away or after the admin turned AI off.
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

    await db.commit()

    ctx = await _page_context(user, db)
    ctx["saved"] = True
    return templates.TemplateResponse(request, "settings/relevance.html", ctx)
