"""App shell: the main page, the sidebar and the actions it owns (mark scope read,
manual feed refresh, search modal)."""
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.settings import AppSettings
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.routers.web.settings.filters import score_sources
from app.services.article import SINCE_DAYS_OPTIONS, mark_scope_read
from app.services.counts_service import (
    mark_read_total, sidebar_counts, story_dedup_of, view_badge, view_count,
)
from app.services.feed import list_user_feeds
from app.services.folder_service import FOLDER_ORDER_DEFAULT, get_folder_order
from app.services.label_service import list_labels
from app.services.saved_search_service import (
    get_saved_search, has_missing_references, list_saved_searches,
)
from app.services.scope_tokens import token_id
from app.services.search_params import modal_values, normalize_search_params
from app.services.story_service import DEDUP_COLLAPSE
from app.services.user import FEEDS_RESUMED_SESSION_KEY, touch_last_active
from app.templating import templates

from .common import _ai_availability, _badge_html, _badge_total_html

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-app"])


@router.get("/app", response_class=HTMLResponse)
async def main_app(
    request: Request,
    open_article_id: int | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await touch_last_active(user, db, request.session)
    feeds_resumed = bool(request.session.pop(FEEDS_RESUMED_SESSION_KEY, False))
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    if settings and settings.onboarded_at is None:
        # Topics already answered: pick up the welcome where it was left.
        step = "/welcome/feeds" if settings.relevance_terms else "/welcome"
        return RedirectResponse(step, status_code=303)
    bucket_small_max = settings.bucket_small_max if settings else 640
    bucket_medium_max = settings.bucket_medium_max if settings else 1100
    reading_font_size = settings.reading_font_size if settings else "md"
    reading_font_family = settings.reading_font_family if settings else "sans"
    label_display = settings.label_display if settings else "indicator"
    ai = await _ai_availability(settings, db)
    chat_available = ai.chat
    catchup_avail = ai.catchup
    # Mobile quicklink: offer Labels only to users who actually label articles,
    # everyone else gets All articles (checked per page load, so it follows along).
    has_labeled = bool(await db.scalar(select(exists().where(ArticleLabel.user_id == user.id))))
    return templates.TemplateResponse(request, "app/main.html", {
        "user": user,
        "bucket_small_max": bucket_small_max,
        "bucket_medium_max": bucket_medium_max,
        "reading_font_size": reading_font_size,
        "reading_font_family": reading_font_family,
        "label_display": label_display,
        "open_original_when_empty": bool(settings and settings.open_original_when_empty),
        "chat_available": chat_available,
        "catchup_available": catchup_avail,
        "open_article_id": open_article_id,
        "has_labeled": has_labeled,
        "feeds_resumed": feeds_resumed,
    })


# ── HTMX fragments ────────────────────────────────────────────────────────────

@router.get("/htmx/sidebar", response_class=HTMLResponse)
async def htmx_sidebar(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    user_feeds = await list_user_feeds(
        user, db, folder_order=settings.folder_order if settings else FOLDER_ORDER_DEFAULT
    )
    user_labels = await list_labels(user, db)

    # A reader with story dedup off gets a list that folds nothing, so their badges
    # count articles again.
    counts = await sidebar_counts(
        db, user.id,
        feed_ids=[uf.feed_id for uf in user_feeds],
        label_ids=[lb.id for lb in user_labels],
        story_dedup=settings.story_dedup if settings else DEDUP_COLLAPSE,
    )

    pinned = request.query_params.get("pinned", "true").lower() != "false"

    ai = await _ai_availability(settings, db)
    chat_available = ai.chat
    catchup_avail = ai.catchup

    return templates.TemplateResponse(request, "app/partials/sidebar.html", {
        "user": user,
        "user_feeds": user_feeds,
        "user_labels": user_labels,
        "saved_searches": await list_saved_searches(db, user.id),
        **asdict(counts),
        "pinned": pinned,
        "chat_available": chat_available,
        "catchup_available": catchup_avail,
        "mark_read_auto_advance": bool(settings and settings.mark_read_auto_advance),
    })


@router.post("/htmx/articles/mark-read", response_class=HTMLResponse)
async def htmx_mark_articles_read(
    before: str = Form(...),
    starred_only: str = Form(""),
    archived_only: str = Form(""),
    saved_only: str = Form(""),
    labeled_only: str = Form(""),
    label_id: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        return HTMLResponse("", status_code=400)
    try:
        lid = token_id(label_id) if label_id else None
    except ValueError:
        # Not a missing label but a garbled one (or past the id column): falling back
        # to None would widen the scope to every subscribed article.
        return HTMLResponse("", status_code=400)
    await mark_scope_read(
        user, db, before=before_dt,
        starred_only=starred_only == "1",
        archived_only=archived_only == "1",
        saved_only=saved_only == "1",
        labeled_only=labeled_only == "1",
        label_id=lid,
    )
    total = await mark_read_total(
        user, db,
        starred_only=starred_only == "1",
        archived_only=archived_only == "1",
        saved_only=saved_only == "1",
        labeled_only=labeled_only == "1",
        label_id=lid,
    )
    resp = HTMLResponse(_badge_total_html(total), status_code=200)
    resp.headers["HX-Trigger"] = "sidebarRefresh"
    return resp


@router.post("/htmx/feeds/{feed_id}/mark-read", response_class=HTMLResponse)
async def htmx_mark_feed_read(
    feed_id: int,
    before: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        return HTMLResponse("", status_code=400)
    await mark_scope_read(user, db, before=before_dt, feed_id=feed_id)
    # The feed's own list, which requires a subscription: a feed_id the reader
    # doesn't follow counts nothing (mark_scope_read itself is already scoped).
    total = await view_count(
        user, db, story_dedup=await story_dedup_of(db, user.id), feed_id=feed_id,
    )
    resp = HTMLResponse(_badge_total_html(total), status_code=200)
    resp.headers["HX-Trigger"] = "sidebarRefresh"
    return resp


@router.post("/htmx/folders/{folder_id}/mark-read", response_class=HTMLResponse)
async def htmx_mark_folder_read(
    folder_id: int,
    before: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        return HTMLResponse("", status_code=400)
    await mark_scope_read(user, db, before=before_dt, folder_id=folder_id)
    total = await view_count(
        user, db, story_dedup=await story_dedup_of(db, user.id), folder_id=folder_id,
    )
    resp = HTMLResponse(_badge_total_html(total), status_code=200)
    resp.headers["HX-Trigger"] = "sidebarRefresh"
    return resp


def _feed_error_oob(feed_id: int, status: str | None, last_error: str | None) -> str:
    """Out-of-band fragment that re-renders the sidebar error indicator for a feed
    from its current status (empty when healthy, red bar when error/disabled)."""
    macros = templates.env.get_template("app/partials/feed_error.html").module
    return str(macros.feed_error(feed_id, status, last_error, oob=True))


@router.post("/htmx/feeds/{feed_id}/refresh", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_feed_refresh)
async def htmx_refresh_feed(
    feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not (await db.execute(
        select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed_id)
    )).scalar_one_or_none():
        return HTMLResponse("", status_code=403)

    feed = await db.get(Feed, feed_id)
    if not feed:
        return HTMLResponse("", status_code=404)

    from app.database import async_session_factory
    from app.fetcher.rss import cooldown_until
    from app.utils.url_validator import format_retry_in
    cooldown_msg = None
    async with async_session_factory() as fetch_session:
        feed_obj = await fetch_session.get(Feed, feed_id)
        if feed_obj:
            now = datetime.now(timezone.utc)
            cd = cooldown_until(feed_obj, now)
            if cd is not None:
                # Known rate-limit window — don't hammer into another 429.
                cooldown_msg = f"Rate-limited — try again in {format_retry_in(cd, now)}."
            elif feed_obj.feed_type == "scrape":
                from app.fetcher.scrape import fetch_scrape_feed
                try:
                    await fetch_scrape_feed(feed_obj, fetch_session)
                except Exception:
                    # fetch_scrape_feed handles a failed scrape itself, message and
                    # counters and all, so only a failure of its *error* path reaches
                    # here — which makes this ours, not the feed's. It used to be
                    # written to feed_obj.last_error, which did nothing twice over: the
                    # session is closed without a commit, and the instance is expired by
                    # the rollback inside fetch_scrape_feed. Log it instead of quietly
                    # dropping it; the row keeps whatever the fetcher already stored.
                    logger.exception("Manual scrape refresh of feed %d failed", feed_id)
            else:
                from app.fetcher.rss import fetch_feed
                await fetch_feed(feed_obj, fetch_session)

    await db.refresh(feed)
    error_msg = cooldown_msg or feed.last_error or None
    # A live 429 during the fetch just armed a cooldown but stored only the raw
    # httpx error — replace that with the timed message (only on failure, so a
    # successful fetch that merely exhausted the budget stays a success).
    if cooldown_msg is None and feed.last_error:
        now2 = datetime.now(timezone.utc)
        cd2 = cooldown_until(feed, now2)
        if cd2 is not None:
            error_msg = f"Rate-limited — try again in {format_retry_in(cd2, now2)}."

    unread, total = await view_badge(
        user, db, story_dedup=await story_dedup_of(db, user.id), feed_id=feed_id,
    )

    badge = _badge_html(unread, total)
    # Refresh the sidebar error indicator out-of-band: it lives outside the swapped
    # #feed-badge target, so a fetch that cleared Feed.status would otherwise leave
    # the red bar stale until a full sidebar reload.
    error_oob = _feed_error_oob(feed_id, feed.status, feed.last_error)
    toast_msg = error_msg[:150] if error_msg else "Feed refreshed"
    toast_type = "error" if error_msg else "ok"
    trigger = {
        "showToast": {"msg": toast_msg, "type": toast_type},
        # Let the client reload the article list if this feed is being viewed.
        "feedRefreshed": {"feed_id": feed_id},
    }
    headers = {"HX-Trigger": json.dumps(trigger)}
    return HTMLResponse(badge + error_oob, headers=headers)


@router.get("/htmx/search-modal", response_class=HTMLResponse)
async def htmx_search_modal(
    request: Request,
    q: str | None = Query(None),
    scope: str | None = Query(None),
    sort: str | None = Query(None),
    status: str | None = Query(None),
    labels: str | None = Query(None),
    score_source: str | None = Query(None),
    score_op: str | None = Query(None),
    score_val: str | None = Query(None),
    since_days: str | None = Query(None),
    state: str | None = Query(None),
    saved_id: int | None = Query(None),
    edited: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The search form. With ``saved_id`` it edits that saved search: filled in with
    its stored parameters, or with the ones in the request when ``edited`` (the
    reader changed the search since opening it and is coming back to save that)."""
    saved = None
    if saved_id is not None:
        saved = await get_saved_search(db, user.id, saved_id)
        if saved is None:
            raise HTTPException(status_code=404)
        if not edited:
            v = modal_values(saved.params)
            q, scope, sort, status, labels = v["q"], v["scope"], v["sort"], v["status"], v["labels"]
            score_source, score_op, score_val = v["score_source"], v["score_op"], v["score_val"]
            since_days, state = v["since_days"], v["state"]

    user_feeds = await list_user_feeds(user, db, folder_order=await get_folder_order(db, user.id))
    user_labels = await list_labels(user, db)
    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    user_s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    # The same sources the filter editor offers: only the scorers this reader runs.
    sources = score_sources(app_s, user_s)
    # "any" is the Score row's own "no condition": the source picks whether to
    # filter at all, and only a condition brings the operator and number with it.
    score_cond = score_op in ("gte", "lt")

    return templates.TemplateResponse(request, "app/partials/search_modal.html", {
        "user_feeds": user_feeds,
        "labels": user_labels,
        "scope_value": scope or None,
        "sort_value": sort or None,
        "status_value": status or None,
        "label_value": labels or None,
        "score_sources": sources,
        "score_source_value": (score_source if score_source in sources else "relevance")
                              if score_cond else "any",
        "score_op_value": score_op if score_cond else None,
        "score_val_value": score_val or "",
        "since_options": SINCE_DAYS_OPTIONS,
        "since_value": int(since_days) if since_days and since_days.isdigit() else None,
        "state_value": state or None,
        "q_value": q or "",
        "saved_search": saved,
        # Checked on the form as shown, which is the stored search or the reader's
        # changes to it.
        "missing_references": bool(saved) and await has_missing_references(
            db, user.id, normalize_search_params({"scope_include": scope, "label_filter": labels}),
        ),
    })
