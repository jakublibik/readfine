"""Web routes for relevance settings: the basic term list and how scores show.

Basic relevance runs without an API key or an admin who has AI switched on, so
this page is never hidden, unlike the AI item in the settings nav. It holds the
term list the lexical scorer matches, its switch, and the score-in-list option,
which applies to both scorers. The AI interest profile is a different text for a
different reader (the model) and lives with AI scoring on the AI page.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.relevance_corpus_service import get_stats
from app.services.relevance_service import parse_terms, skipped_terms
from app.templating import templates

from .common import _get_or_create_settings

router = APIRouter(prefix="/settings", tags=["settings"])

TERMS_MAX_CHARS = 5000


async def _page_context(user: User, db: AsyncSession) -> dict:
    s = await _get_or_create_settings(user, db)
    return {
        "s": s,
        "active": "relevance",
        "term_count": len(parse_terms(s.relevance_terms)),
        # Nothing is scored until the corpus has been counted once, and an empty
        # score column with no explanation looks like a bug rather than like the
        # first minutes of an install.
        "corpus_ready": await get_stats(db) is not None,
        "terms_max_chars": TERMS_MAX_CHARS,
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

    # Line endings are the browser's, not the reader's: a textarea submits \r\n.
    raw = (form.get("relevance_terms") or "").replace("\r\n", "\n")
    if len(raw) > TERMS_MAX_CHARS:
        ctx = await _page_context(user, db)
        ctx["error"] = (f"The term list is too long ({len(raw)} characters). "
                        f"Maximum is {TERMS_MAX_CHARS:,} characters.".replace(",", " "))
        ctx["terms_submitted"] = raw
        return templates.TemplateResponse(request, "settings/relevance.html", ctx)

    # Stored the way the reader wrote it, separators and all: the scorer reads
    # it apart on every load (parse_terms), and rewriting someone's list under
    # their hands would be the surprising thing to do. What the scorer is going
    # to skip is said instead.
    text = raw.strip() or None

    s.basic_scoring_enabled = form.get("basic_scoring_enabled") == "on"
    s.ai_score_show_in_list = form.get("ai_score_show_in_list") == "on"
    if text != s.relevance_terms:
        s.relevance_terms = text
        # Also what makes the 7-day backfill due.
        s.relevance_terms_updated_at = datetime.now(timezone.utc)
        s.relevance_terms_source = "manual"
    await db.commit()

    ctx = await _page_context(user, db)
    ctx["saved"] = True
    ctx["skipped"] = skipped_terms(raw)
    return templates.TemplateResponse(request, "settings/relevance.html", ctx)
