"""App shell: the main page, the sidebar and the actions it owns (mark scope read,
manual feed refresh, search modal)."""
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.user import User, UserSettings
from app.services.article import mark_scope_read
from app.services.feed import list_user_feeds
from app.services.label_service import list_labels
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
    now = datetime.now(timezone.utc)
    if not user.last_active_at or user.last_active_at < now - timedelta(hours=1):
        user.last_active_at = now
        await db.commit()
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    bucket_small_max = settings.bucket_small_max if settings else 640
    bucket_medium_max = settings.bucket_medium_max if settings else 1100
    reading_font_size = settings.reading_font_size if settings else "md"
    reading_font_family = settings.reading_font_family if settings else "sans"
    label_display = settings.label_display if settings else "indicator"
    ai = await _ai_availability(settings, db)
    chat_available = ai.chat
    catchup_avail = ai.catchup
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
    })


# ── HTMX fragments ────────────────────────────────────────────────────────────

@router.get("/htmx/sidebar", response_class=HTMLResponse)
async def htmx_sidebar(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds = await list_user_feeds(user, db)
    user_labels = await list_labels(user, db)

    feed_ids = [uf.feed_id for uf in user_feeds]

    # Nav counts
    nav_total = (await db.execute(
        select(func.count()).select_from(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(UserFeed.user_id == user.id, Article.trimmed_at.is_(None))
    )).scalar() or 0
    # Every counter here joins Article for trimmed_at IS NULL, the same filter
    # list_articles applies, so a badge can never stand above a list that opens onto
    # fewer rows. Retention cannot trim an article while it is starred, archived or
    # saved (purge_service._fully_protected_exists), but the reverse reaches it: a
    # stub trimmed overnight can still be starred from a list rendered before the
    # trim, and the resulting drift never heals on its own.
    uas_row = (await db.execute(
        select(
            func.count().filter(UserArticleState.is_starred == True).label("starred"),
            func.count().filter((UserArticleState.is_starred == True) & (UserArticleState.is_read == False)).label("unread_starred"),
            func.count().filter(UserArticleState.is_archived == True).label("archived"),
            func.count().filter((UserArticleState.is_archived == True) & (UserArticleState.is_read == False)).label("unread_archived"),
            func.count().filter(UserArticleState.saved_at.is_not(None)).label("saved"),
            func.count().filter((UserArticleState.saved_at.is_not(None)) & (UserArticleState.is_read == False)).label("unread_saved"),
        )
        .select_from(UserArticleState)
        .join(Article, Article.id == UserArticleState.article_id)
        .where(UserArticleState.user_id == user.id, Article.trimmed_at.is_(None))
    )).one()
    nav_starred = uas_row.starred or 0
    nav_unread_starred = uas_row.unread_starred or 0
    nav_archived = uas_row.archived or 0
    nav_unread_archived = uas_row.unread_archived or 0
    nav_saved = uas_row.saved or 0
    nav_unread_saved = uas_row.unread_saved or 0
    nav_labeled = (await db.execute(
        select(func.count(func.distinct(ArticleLabel.article_id)))
        .select_from(ArticleLabel)
        .join(Article, Article.id == ArticleLabel.article_id)
        .where(ArticleLabel.user_id == user.id, Article.trimmed_at.is_(None))
    )).scalar() or 0
    nav_unread_labeled = (await db.execute(
        select(func.count(Article.id.distinct()))
        .select_from(Article)
        .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
        .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id))
        .where(
            Article.trimmed_at.is_(None),
            (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
        )
    )).scalar() or 0

    # Feed total + unread counts (batch, computed from DB — not cached unread_count)
    if feed_ids:
        feed_total_counts = dict((await db.execute(
            select(Article.feed_id, func.count(Article.id))
            .where(Article.feed_id.in_(feed_ids), Article.trimmed_at.is_(None))
            .group_by(Article.feed_id)
        )).all())
        feed_unread_counts = dict((await db.execute(
            select(Article.feed_id, func.count(Article.id))
            .outerjoin(
                UserArticleState,
                (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id),
            )
            .where(
                Article.feed_id.in_(feed_ids),
                Article.trimmed_at.is_(None),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
            .group_by(Article.feed_id)
        )).all())
    else:
        feed_total_counts = {}
        feed_unread_counts = {}

    nav_unread = sum(feed_unread_counts.values())

    # Aggregate counts per folder (None = no folder)
    folder_unread_counts: dict[int | None, int] = {}
    folder_total_counts: dict[int | None, int] = {}
    for uf in user_feeds:
        key = uf.folder_id
        folder_unread_counts[key] = folder_unread_counts.get(key, 0) + feed_unread_counts.get(uf.feed_id, 0)
        folder_total_counts[key] = folder_total_counts.get(key, 0) + feed_total_counts.get(uf.feed_id, 0)

    # Label article counts (batch)
    label_ids = [lb.id for lb in user_labels]
    if label_ids:
        label_counts = dict((await db.execute(
            select(ArticleLabel.label_id, func.count(ArticleLabel.article_id))
            .join(Article, Article.id == ArticleLabel.article_id)
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id.in_(label_ids),
                Article.trimmed_at.is_(None),
            )
            .group_by(ArticleLabel.label_id)
        )).all())
        label_unread_counts = dict((await db.execute(
            select(ArticleLabel.label_id, func.count(ArticleLabel.article_id))
            .join(Article, Article.id == ArticleLabel.article_id)
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user.id))
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id.in_(label_ids),
                Article.trimmed_at.is_(None),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
            .group_by(ArticleLabel.label_id)
        )).all())
    else:
        label_counts = {}
        label_unread_counts = {}

    pinned = request.query_params.get("pinned", "true").lower() != "false"

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    ai = await _ai_availability(settings, db)
    chat_available = ai.chat
    catchup_avail = ai.catchup

    return templates.TemplateResponse(request, "app/partials/sidebar.html", {
        "user": user,
        "user_feeds": user_feeds,
        "user_labels": user_labels,
        "feed_total_counts": feed_total_counts,
        "feed_unread_counts": feed_unread_counts,
        "label_counts": label_counts,
        "nav_total": nav_total,
        "nav_unread": nav_unread,
        "nav_starred": nav_starred,
        "nav_unread_starred": nav_unread_starred,
        "nav_archived": nav_archived,
        "nav_unread_archived": nav_unread_archived,
        "nav_saved": nav_saved,
        "nav_unread_saved": nav_unread_saved,
        "nav_labeled": nav_labeled,
        "nav_unread_labeled": nav_unread_labeled,
        "label_unread_counts": label_unread_counts,
        "folder_unread_counts": folder_unread_counts,
        "folder_total_counts": folder_total_counts,
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
    await mark_scope_read(
        user, db, before=before_dt,
        starred_only=starred_only == "1",
        archived_only=archived_only == "1",
        saved_only=saved_only == "1",
        labeled_only=labeled_only == "1",
        label_id=int(label_id) if label_id else None,
    )
    lid = int(label_id) if label_id else None
    total = await _mark_read_total(
        user, db, starred_only == "1", archived_only == "1", saved_only == "1",
        labeled_only == "1", lid,
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
    # Scope the count to the user's own subscription — otherwise it leaks the
    # article count of any feed_id (mark_scope_read itself is already scoped).
    total = (await db.execute(
        select(func.count(Article.id))
        .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .where(Article.feed_id == feed_id)
    )).scalar() or 0
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
    folder_cond = UserFeed.folder_id.is_(None) if folder_id == 0 else (UserFeed.folder_id == folder_id)
    total = (await db.execute(
        select(func.count(Article.id))
        .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .where(folder_cond)
    )).scalar() or 0
    resp = HTMLResponse(_badge_total_html(total), status_code=200)
    resp.headers["HX-Trigger"] = "sidebarRefresh"
    return resp


def _feed_error_oob(feed_id: int, status: str | None, last_error: str | None) -> str:
    """Out-of-band fragment that re-renders the sidebar error indicator for a feed
    from its current status (empty when healthy, red bar when error/disabled)."""
    macros = templates.env.get_template("app/partials/feed_error.html").module
    return str(macros.feed_error(feed_id, status, last_error, oob=True))


async def _mark_read_total(
    user: User, db: AsyncSession,
    starred_only: bool, archived_only: bool, saved_only: bool, labeled_only: bool,
    label_id: int | None,
) -> int:
    """How many articles the ✓ on a sidebar row covers, for the badge next to it.

    Every branch filters ``Article.trimmed_at IS NULL``, the same as the counters in
    htmx_sidebar and the same as list_articles: a retention stub is hidden in the
    list and in the badge, so counting it here would leave the two numbers on one
    row disagreeing.
    """
    async def _states(*conditions) -> int:
        return (await db.execute(
            select(func.count())
            .select_from(UserArticleState)
            .join(Article, Article.id == UserArticleState.article_id)
            .where(
                UserArticleState.user_id == user.id,
                Article.trimmed_at.is_(None),
                *conditions,
            )
        )).scalar() or 0

    if starred_only:
        return await _states(UserArticleState.is_starred == True)
    if archived_only:
        return await _states(UserArticleState.is_archived == True)
    if saved_only:
        return await _states(UserArticleState.saved_at.is_not(None))
    if label_id is not None:
        return (await db.execute(
            select(func.count(ArticleLabel.article_id))
            .select_from(ArticleLabel)
            .join(Article, Article.id == ArticleLabel.article_id)
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id == label_id,
                Article.trimmed_at.is_(None),
            )
        )).scalar() or 0
    if labeled_only:
        return (await db.execute(
            select(func.count(func.distinct(ArticleLabel.article_id)))
            .select_from(ArticleLabel)
            .join(Article, Article.id == ArticleLabel.article_id)
            .where(ArticleLabel.user_id == user.id, Article.trimmed_at.is_(None))
        )).scalar() or 0
    # All articles
    return (await db.execute(
        select(func.count(Article.id))
        .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .where(Article.trimmed_at.is_(None))
    )).scalar() or 0


@router.post("/htmx/feeds/{feed_id}/refresh", response_class=HTMLResponse)
async def htmx_refresh_feed(
    feed_id: int,
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

    unread = await db.scalar(
        select(func.count(Article.id))
        .outerjoin(UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id))
        .where(Article.feed_id == feed_id,
               (UserArticleState.is_read == None) | (UserArticleState.is_read == False))
    ) or 0
    total = await db.scalar(
        select(func.count(Article.id)).where(Article.feed_id == feed_id)
    ) or 0

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
    scope: str | None = Query(None),
    sort: str | None = Query(None),
    status: str | None = Query(None),
    labels: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds = await list_user_feeds(user, db)
    user_labels = await list_labels(user, db)

    return templates.TemplateResponse(request, "app/partials/search_modal.html", {
        "user_feeds": user_feeds,
        "labels": user_labels,
        "scope_value": scope or None,
        "sort_value": sort or None,
        "status_value": status or None,
        "label_value": labels or None,
    })
