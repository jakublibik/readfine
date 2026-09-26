"""The welcome screen a new account sees once, before its first look at the reader.

One question, which topics the reader follows. The answer is stored as the basic
relevance term list, the same as if it had been typed on Settings → Relevance, so
articles are scored from the first feed on. Skipping is a full answer too: the
article list offers the same setup later (relevance_terms_service.show_relevance_intro).

The screen is the place for later first-run choices as well, such as starting
empty or with sample content. The bar's dismiss endpoint lives here too.
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
    return templates.TemplateResponse(request, "app/welcome.html", {
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
        save_terms(s, raw, source="onboarding")
        # A score nobody can see does not show that the answer did anything.
        s.ai_score_show_in_list = True
    if not s.onboarded_at:
        s.onboarded_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(headers={"HX-Redirect": "/app"})


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
