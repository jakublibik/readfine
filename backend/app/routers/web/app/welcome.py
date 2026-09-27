"""The welcome screens a new account sees once, before its first look at the reader.

Step 1 asks which topics the reader follows. The answer is stored as the basic
relevance term list, the same as if it had been typed on Settings → Relevance, so
articles are scored from the first feed on. Skipping is a full answer too: the
article list offers the same setup later (relevance_terms_service.show_relevance_intro).

Step 2 asks where the articles should come from: a few starter feeds
(app/content/starter_feeds.yml), an OPML import, or feeds of the reader's own. The
account counts as onboarded only after this choice, so a reader who leaves halfway
comes back to it; /app sends one who already answered step 1 straight to step 2.
The bar's dismiss endpoint lives here too.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.routers.web.settings.common import _get_or_create_settings
from app.services.relevance_service import parse_terms
from app.services.relevance_terms_service import TERMS_MAX_CHARS, save_terms
from app.services.saved_search_service import create_top_picks
from app.services.starter_feeds_service import (
    load_starter_categories,
    preselected,
    subscribe_starter,
)
from app.templating import templates

router = APIRouter(tags=["web-app"])


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_or_create_settings(user, db)
    if s.onboarded_at:
        return RedirectResponse("/app", status_code=303)
    # Always step 1, filled in when coming back from step 2 (its Back link or the
    # browser's); /app is what sends a returning reader on to step 2.
    return templates.TemplateResponse(request, "app/welcome.html", {
        "relevance_terms": s.relevance_terms or "",
        "terms_max_chars": TERMS_MAX_CHARS,
    })


@router.post("/welcome")
async def welcome_save(
    request: Request,
    relevance_terms: str = Form(""),
    skip: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_or_create_settings(user, db)
    raw = relevance_terms.replace("\r\n", "\n")
    if not skip:
        # The text area has maxlength, so only a hand-made request gets here.
        if len(raw) > TERMS_MAX_CHARS:
            return HTMLResponse(
                f"That list is too long. Maximum is {TERMS_MAX_CHARS:,} characters.".replace(",", " ")
            )
        # Continue with nothing usable in the box would do what Skip does, and
        # quietly: say so instead, and leave the choice to skip to the reader.
        if not raw.strip():
            return HTMLResponse("Type a few topics, or choose Skip for now.")
        if not parse_terms(raw):
            return HTMLResponse("None of these can be matched. Use words of two letters or more.")
        # A view of what scores best shows what the terms are for. First pass only:
        # a repeated POST must not bring back a Top picks the reader deleted.
        first_pass = not s.onboarded_at and not s.relevance_terms
        save_terms(s, raw, source="onboarding")
        # A score nobody can see does not show that the answer did anything.
        s.ai_score_show_in_list = True
        if first_pass:
            await create_top_picks(db, user.id)
    await db.commit()
    return Response(headers={"HX-Redirect": "/app" if s.onboarded_at else "/welcome/feeds"})


@router.get("/welcome/feeds", response_class=HTMLResponse)
async def welcome_feeds(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_or_create_settings(user, db)
    if s.onboarded_at:
        return RedirectResponse("/app", status_code=303)
    categories = load_starter_categories()
    return templates.TemplateResponse(request, "app/welcome_feeds.html", {
        "categories": categories,
        "checked": preselected(categories, s.relevance_terms),
    })


# Where the two ways other than the starter feeds continue.
_WELCOME_EXITS = {"import": "/settings/opml", "manual": "/settings/feeds"}


@router.post("/welcome/feeds")
async def welcome_feeds_save(
    request: Request,
    choice: str = Form(""),
    category: list[str] = Form([]),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if (await _get_or_create_settings(user, db)).onboarded_at:
        # A second tab, or the browser's Back: the welcome is over, Settings → Feeds
        # is where feeds are added from now on.
        return Response(headers={"HX-Redirect": "/app"})
    if choice == "starter":
        if not category:
            return HTMLResponse("Pick at least one topic, or choose another way below.")
        result = await subscribe_starter(user, category, db)
        if not (result.added or result.skipped):
            if result.over_limit:
                return HTMLResponse("Your account has reached its feed limit.")
            return HTMLResponse(
                "These feeds could not be reached right now. Try again in a moment, "
                "or choose another way below."
            )
        target = "/app"
    elif choice in _WELCOME_EXITS:
        target = _WELCOME_EXITS[choice]
    else:
        return HTMLResponse("Choose one of the ways below.")
    # Read after subscribing: a failed feed there rolls the session back.
    s = await _get_or_create_settings(user, db)
    if not s.onboarded_at:
        s.onboarded_at = datetime.now(timezone.utc)
        await db.commit()
    return Response(headers={"HX-Redirect": target})


@router.post("/htmx/relevance-intro/dismiss", response_class=HTMLResponse)
async def relevance_intro_dismiss(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Close the article list's "set up relevance" bar for good, on every device."""
    s = await _get_or_create_settings(user, db)
    if not s.relevance_intro_dismissed_at:
        s.relevance_intro_dismissed_at = datetime.now(timezone.utc)
        await db.commit()
    return HTMLResponse("")
