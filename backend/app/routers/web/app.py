"""Web routes for the main application UI."""
import asyncio
import html as html_module
from app.utils.markdown import md_render as _md_render
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select, func, update as sa_update, delete as sa_delete

logger = logging.getLogger(__name__)

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.rate_limit import limiter
from app.models.article import Article, ArticleAiChat, ArticleAiJob, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.user import User, UserSettings
from app.schemas.article import ArticleStateUpdate
from app.services.article import filter_accessible_article_ids, get_article, list_articles, mark_articles_read_batch, mark_scope_read, toggle_article_state, update_article_state
from app.services.feed import list_user_feeds
from app.services.label_service import list_labels
from app.services.readable_service import apply_readable_result

from app.templating import templates

router = APIRouter(tags=["web-app"])


async def _extract_readable_bg(
    article_id: int,
    url: str,
    auth_user: str | None,
    auth_pass_enc: str | None,
) -> None:
    """Background readable extraction fired when user opens an article."""
    from app.database import async_session_factory
    from app.services.readable_service import extract_readable
    from app.utils.crypto import decrypt

    auth_pass: str | None = None
    if auth_pass_enc:
        try:
            auth_pass = decrypt(auth_pass_enc)
        except Exception:
            logger.warning("readable bg: decrypt failed for article %d", article_id)

    loop = asyncio.get_running_loop()
    try:
        content, error, http_status = await loop.run_in_executor(
            None, extract_readable, url, auth_user, auth_pass
        )
    except Exception as exc:
        content, error, http_status = None, str(exc)[:200], None
        logger.warning("readable bg: extraction error for article %d: %s", article_id, exc)

    async with async_session_factory() as db:
        article = (await db.execute(
            select(Article).where(Article.id == article_id)
        )).scalar_one_or_none()
        if not article:
            return
        apply_readable_result(article, content, error, http_status)
        # Mirror the batch readable path: once readable finishes, complete any
        # label-deferred AI pipeline (scoring/filters/summary). run_pipeline_for_
        # article_all_users is label-scoped — it only runs for users who labeled
        # this article — so opening an unlabeled article never triggers scoring.
        from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
        if content:
            await run_pipeline_for_article_all_users(article, db)
        elif article.readable_status == "failed":
            # Terminal failure — score with the RSS content we already have.
            await run_pipeline_for_article_all_users(article, db)
        await db.commit()
        logger.info("readable bg: article %d → %s", article_id, article.readable_status)


# Grace period after starring before the summary is processed immediately, so a
# quick unstar (mis-click) cancels it instead of spending tokens. The 5-minute
# batch worker still backstops anything left pending.
_STAR_SUMMARY_DEBOUNCE_S = 5.0


async def _summary_after_star_bg(article_id: int, user_id: int) -> None:
    """Wait out the debounce, then process the pending summary job immediately
    (instead of waiting for the batch) — unless the star was removed meanwhile."""
    await asyncio.sleep(_STAR_SUMMARY_DEBOUNCE_S)
    from app.database import async_session_factory
    from app.services.ai_pipeline_service import _run_summary_now

    async with async_session_factory() as db:
        state = await db.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id == article_id,
            )
        )
        # Unstarred during the debounce, or summary already produced → skip.
        if state is None or not state.is_starred or state.ai_summary:
            return
        article = await db.scalar(select(Article).where(Article.id == article_id))
        if article is None:
            return
        await _run_summary_now(article, user_id, db)
        await db.commit()
        logger.info("star summary: article=%d user=%d processed", article_id, user_id)


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
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    ai_avail = bool(ai_on and settings and settings.ai_quality_provider and settings.ai_quality_model)
    chat_available = bool(ai_avail and getattr(settings, 'ai_chat_enabled', False))
    catchup_avail = _catchup_available(bool(ai_on), settings)
    return templates.TemplateResponse(request, "app/main.html", {
        "user": user,
        "bucket_small_max": bucket_small_max,
        "bucket_medium_max": bucket_medium_max,
        "reading_font_size": reading_font_size,
        "reading_font_family": reading_font_family,
        "label_display": label_display,
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
    uas_row = (await db.execute(
        select(
            func.count().filter(UserArticleState.is_starred == True).label("starred"),
            func.count().filter((UserArticleState.is_starred == True) & (UserArticleState.is_read == False)).label("unread_starred"),
            func.count().filter(UserArticleState.is_archived == True).label("archived"),
            func.count().filter((UserArticleState.is_archived == True) & (UserArticleState.is_read == False)).label("unread_archived"),
        )
        .where(UserArticleState.user_id == user.id)
    )).one()
    nav_starred = uas_row.starred or 0
    nav_unread_starred = uas_row.unread_starred or 0
    nav_archived = uas_row.archived or 0
    nav_unread_archived = uas_row.unread_archived or 0
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
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    ai_avail = bool(ai_on and settings and settings.ai_quality_provider and settings.ai_quality_model)
    chat_available = bool(ai_avail and getattr(settings, 'ai_chat_enabled', False))
    catchup_avail = _catchup_available(bool(ai_on), settings)

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
        "nav_labeled": nav_labeled,
        "nav_unread_labeled": nav_unread_labeled,
        "label_unread_counts": label_unread_counts,
        "folder_unread_counts": folder_unread_counts,
        "folder_total_counts": folder_total_counts,
        "pinned": pinned,
        "chat_available": chat_available,
        "catchup_available": catchup_avail,
    })


@router.post("/htmx/sidebar/pin", response_class=HTMLResponse)
async def htmx_sidebar_pin(
    request: Request,
    user: User = Depends(get_current_user),
):
    # Pin state is now managed client-side via localStorage; endpoint kept for compatibility.
    return HTMLResponse("", status_code=204)


@router.post("/htmx/articles/set-read-batch")
async def htmx_set_read_batch(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or [])[:500] if str(i).isdigit()]
    await mark_articles_read_batch(user, ids, db)
    return HTMLResponse("", status_code=200)


@router.post("/htmx/articles/mark-read", response_class=HTMLResponse)
async def htmx_mark_articles_read(
    before: str = Form(...),
    starred_only: str = Form(""),
    archived_only: str = Form(""),
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
        labeled_only=labeled_only == "1",
        label_id=int(label_id) if label_id else None,
    )
    lid = int(label_id) if label_id else None
    total = await _mark_read_total(user, db, starred_only == "1", archived_only == "1", labeled_only == "1", lid)
    resp = HTMLResponse(_BADGE_TOTAL.format(total), status_code=200)
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
    resp = HTMLResponse(_BADGE_TOTAL.format(total), status_code=200)
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
    resp = HTMLResponse(_BADGE_TOTAL.format(total), status_code=200)
    resp.headers["HX-Trigger"] = "sidebarRefresh"
    return resp


_BADGE_UNREAD = '<span class="mark-read-badge ml-auto flex-shrink-0 text-xs font-medium bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full">{}</span>'
_BADGE_TOTAL  = '<span class="mark-read-badge ml-auto flex-shrink-0 text-xs text-gray-400 px-1.5 py-0.5">{}</span>'


async def _mark_read_total(
    user: User, db: AsyncSession,
    starred_only: bool, archived_only: bool, labeled_only: bool,
    label_id: int | None,
) -> int:
    if starred_only:
        return (await db.execute(
            select(func.count()).select_from(UserArticleState)
            .where(UserArticleState.user_id == user.id, UserArticleState.is_starred == True)
        )).scalar() or 0
    if archived_only:
        return (await db.execute(
            select(func.count()).select_from(UserArticleState)
            .where(UserArticleState.user_id == user.id, UserArticleState.is_archived == True)
        )).scalar() or 0
    if label_id is not None:
        return (await db.execute(
            select(func.count(ArticleLabel.article_id))
            .where(ArticleLabel.user_id == user.id, ArticleLabel.label_id == label_id)
        )).scalar() or 0
    if labeled_only:
        return (await db.execute(
            select(func.count()).select_from(
                select(ArticleLabel.article_id).where(ArticleLabel.user_id == user.id).distinct().subquery()
            )
        )).scalar() or 0
    # All articles
    return (await db.execute(
        select(func.count(Article.id))
        .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
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
    async with async_session_factory() as fetch_session:
        feed_obj = await fetch_session.get(Feed, feed_id)
        if feed_obj:
            if feed_obj.feed_type == "scrape":
                from app.fetcher.scrape import fetch_scrape_feed
                try:
                    await fetch_scrape_feed(feed_obj, fetch_session)
                except Exception as e:
                    feed_obj.last_error = str(e)[:500]
            else:
                from app.fetcher.rss import fetch_feed
                await fetch_feed(feed_obj, fetch_session)

    await db.refresh(feed)
    error_msg = feed.last_error or None

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

    badge = _BADGE_UNREAD.format(unread) if unread > 0 else _BADGE_TOTAL.format(total)
    toast_msg = error_msg[:150] if error_msg else "Feed refreshed"
    toast_type = "error" if error_msg else "ok"
    headers = {"HX-Trigger": json.dumps({"showToast": {"msg": toast_msg, "type": toast_type}})}
    return HTMLResponse(badge, headers=headers)


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


def _badge_html(unread: int, total: int) -> str:
    if unread > 0:
        return f'<span class="mark-read-badge ml-auto flex-shrink-0 text-xs font-medium bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full">{unread}</span>'
    return f'<span class="mark-read-badge ml-auto flex-shrink-0 text-xs text-gray-400 px-1.5 py-0.5">{total}</span>'


async def _label_badge_oob(user_id: int, label_id: int | None, labeled_only: bool, db: AsyncSession) -> str:
    """Return OOB HTML snippets to update label badge(s) in the sidebar."""
    if not label_id and not labeled_only:
        return ""
    oob = ""
    if label_id:
        lu = (await db.scalar(
            select(func.count(ArticleLabel.article_id))
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user_id))
            .where(
                ArticleLabel.user_id == user_id,
                ArticleLabel.label_id == label_id,
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
        )) or 0
        lt = (await db.scalar(
            select(func.count(ArticleLabel.article_id))
            .where(ArticleLabel.user_id == user_id, ArticleLabel.label_id == label_id)
        )) or 0
        oob += f'<span id="label-badge-{label_id}" hx-swap-oob="innerHTML">{_badge_html(lu, lt)}</span>'
    # Aggregate "Labels" badge
    all_unread = (await db.scalar(
        select(func.count(Article.id.distinct()))
        .select_from(Article)
        .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user_id))
        .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id))
        .where((UserArticleState.is_read == None) | (UserArticleState.is_read == False))
    )) or 0
    all_total = (await db.scalar(
        select(func.count()).select_from(
            select(ArticleLabel.article_id).where(ArticleLabel.user_id == user_id).distinct().subquery()
        )
    )) or 0
    oob += f'<span id="label-badge-all" hx-swap-oob="innerHTML">{_badge_html(all_unread, all_total)}</span>'
    return oob


def _build_more_qs(filter_params: dict, articles, q: str | None, next_offset: int) -> str:
    """Query string for the infinite-scroll "load more" sentinel.

    Search (FTS) keeps offset pagination (ts_rank ordering can't be keyset-paged,
    and search isn't unread-filtered). Everything else uses a keyset cursor on
    (sort_ts, id) so marking articles read mid-scroll can't shift the window and
    skip rows — see ix_articles_sort_ts.
    """
    params = dict(filter_params)
    if q and q.strip():
        params["offset"] = next_offset
    elif articles:
        params["cursor_ts"] = articles[-1].sort_ts.isoformat()
        params["cursor_id"] = articles[-1].id
    return urlencode(params)


@router.get("/htmx/articles", response_class=HTMLResponse)
async def htmx_article_list(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    scope_include: str | None = Query(None),
    label_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    labeled_only: bool = Query(False),
    q: str | None = Query(None),
    sort: str | None = Query(None),
    read_status: str | None = Query(None),
    label_filter: str | None = Query(None),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()

    # The search modal can submit with an empty query term as a pure filter view
    # (scope / labels / status), so "search mode" is any of those, not just q.
    is_search = bool(q and q.strip()) or bool(scope_include) or bool(label_filter) or bool(read_status)

    sort_order = settings.default_sort_order if settings else "newest"
    # Search has its own sort selector (relevance default); other views use the
    # user's configured list sort. Without a query term relevance is meaningless,
    # so list_articles' non-FTS branch treats "relevance" as newest.
    if is_search:
        sort_order = sort or "relevance"
    articles_per_page = settings.articles_per_page if settings else 50
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    label_display = settings.label_display if settings else "indicator"
    ua = request.headers.get("user-agent", "")
    is_mobile = any(x in ua.lower() for x in ("mobile", "android", "iphone", "ipad"))
    density = (settings.list_density_mobile if is_mobile else settings.list_density_web) if settings else "comfortable"

    # Resolve effective unread filter
    if is_search or starred_only or archived_only:
        # Search uses its own status selector (read_status below); other
        # state-based views always show everything.
        effective_unread_only = False
    elif unread_only:
        # Explicit "Unread" nav item — always filter
        effective_unread_only = True
    else:
        unread_filter = settings.unread_filter if settings else "adaptive"
        if unread_filter == "unread_only":
            effective_unread_only = True
        elif unread_filter == "show_all":
            effective_unread_only = False
        else:  # adaptive
            probe = await list_articles(
                user=user, db=db,
                feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
                label_id=label_id,
                labeled_only=labeled_only,
                unread_only=True, limit=1,
            )
            effective_unread_only = len(probe) > 0

    articles = await list_articles(
        user=user,
        db=db,
        feed_id=feed_id,
        folder_id=folder_id,
        scope_include=scope_include,
        label_id=label_id,
        label_filter=label_filter,
        unread_only=effective_unread_only,
        read_status=read_status,
        starred_only=starred_only,
        archived_only=archived_only,
        labeled_only=labeled_only,
        q=q or None,
        sort_order=sort_order,
        limit=articles_per_page,
        offset=offset,
    )

    has_more = len(articles) >= articles_per_page

    # Title bar count for mobile hideable mode
    title_bar_count: int | None = None
    title_bar_count_type: str | None = None
    if label_id is not None:
        title_bar_count = (await db.execute(
            select(func.count(ArticleLabel.article_id))
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user.id))
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id == label_id,
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
        )).scalar() or 0
        title_bar_count_type = "unread"
    elif labeled_only:
        title_bar_count = (await db.execute(
            select(func.count(Article.id.distinct()))
            .select_from(Article)
            .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
            .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id))
            .where((UserArticleState.is_read == None) | (UserArticleState.is_read == False))
        )).scalar() or 0
        title_bar_count_type = "unread"
    elif starred_only:
        title_bar_count = (await db.execute(
            select(func.count(UserArticleState.article_id))
            .where(
                UserArticleState.user_id == user.id,
                UserArticleState.is_starred == True,
            )
        )).scalar() or 0
        title_bar_count_type = "starred"

    filter_params: dict = {}
    if feed_id is not None:
        filter_params["feed_id"] = feed_id
    if folder_id is not None:
        filter_params["folder_id"] = folder_id
    if scope_include:
        filter_params["scope_include"] = scope_include
    if label_id is not None:
        filter_params["label_id"] = label_id
    if effective_unread_only:
        filter_params["unread_only"] = "true"
    if starred_only:
        filter_params["starred_only"] = "true"
    if archived_only:
        filter_params["archived_only"] = "true"
    if labeled_only:
        filter_params["labeled_only"] = "true"
    if q and q.strip():
        filter_params["q"] = q.strip()
    if is_search:
        # Carry the search/filter knobs into pagination, even with an empty query.
        filter_params["sort"] = sort_order
        if read_status:
            filter_params["read_status"] = read_status
        if label_filter:
            filter_params["label_filter"] = label_filter

    extra_headers: dict[str, str] = {}
    if feed_id is not None:
        feed_obj = await db.get(Feed, feed_id)
        if feed_obj and feed_obj.status in ("error", "disabled") and feed_obj.last_error:
            extra_headers["HX-Trigger"] = json.dumps(
                {"showToast": {"msg": feed_obj.last_error[:150], "type": "warning"}}
            )

    extra_ctx: dict = {}
    if settings and getattr(settings, 'ai_chat_enabled', False):
        extra_ctx["chat_article_ids"] = await _get_chat_article_ids(
            user.id, [a.id for a in articles], db
        )

    if not articles and offset == 0:
        feed_count = await db.scalar(
            select(func.count(UserFeed.id)).where(UserFeed.user_id == user.id)
        )
        extra_ctx["has_feeds"] = bool(feed_count)
    else:
        extra_ctx["has_feeds"] = True

    list_html = templates.env.get_template("app/partials/article_list.html").render(
        request=request,
        articles=articles,
        feed_id=feed_id,
        folder_id=folder_id,
        unread_only=effective_unread_only,
        starred_only=starred_only,
        archived_only=archived_only,
        search_query=q.strip() if q and q.strip() else None,
        filter_active=is_search,
        mark_read_on_scroll=mark_read_on_scroll,
        density=density,
        label_display=label_display,
        show_ai_score=settings.ai_score_show_in_list if settings else False,
        has_more=has_more,
        more_qs=_build_more_qs(filter_params, articles, q, len(articles)),
        title_bar_count=title_bar_count,
        title_bar_count_type=title_bar_count_type,
        **extra_ctx,
    )
    oob = await _label_badge_oob(user.id, label_id, labeled_only, db)
    return HTMLResponse(list_html + oob, headers=extra_headers)


@router.get("/htmx/articles/more", response_class=HTMLResponse)
async def htmx_article_list_more(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    scope_include: str | None = Query(None),
    label_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    labeled_only: bool = Query(False),
    q: str | None = Query(None),
    sort: str | None = Query(None),
    read_status: str | None = Query(None),
    label_filter: str | None = Query(None),
    offset: int = Query(0, ge=0),
    cursor_ts: datetime | None = Query(None),
    cursor_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()

    is_search = bool(q and q.strip()) or bool(scope_include) or bool(label_filter) or bool(read_status)
    sort_order = settings.default_sort_order if settings else "newest"
    if is_search:
        sort_order = sort or "relevance"
    articles_per_page = settings.articles_per_page if settings else 50
    ua = request.headers.get("user-agent", "")
    is_mobile = any(x in ua.lower() for x in ("mobile", "android", "iphone", "ipad"))
    density = (settings.list_density_mobile if is_mobile else settings.list_density_web) if settings else "comfortable"
    label_display = settings.label_display if settings else "indicator"

    articles = await list_articles(
        user=user,
        db=db,
        feed_id=feed_id,
        folder_id=folder_id,
        scope_include=scope_include,
        label_id=label_id,
        label_filter=label_filter,
        unread_only=unread_only,
        read_status=read_status,
        starred_only=starred_only,
        archived_only=archived_only,
        labeled_only=labeled_only,
        q=q or None,
        sort_order=sort_order,
        limit=articles_per_page,
        offset=offset,
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
    )

    has_more = len(articles) >= articles_per_page
    filter_params: dict = {}
    if feed_id is not None:
        filter_params["feed_id"] = feed_id
    if folder_id is not None:
        filter_params["folder_id"] = folder_id
    if scope_include:
        filter_params["scope_include"] = scope_include
    if label_id is not None:
        filter_params["label_id"] = label_id
    if unread_only:
        filter_params["unread_only"] = "true"
    if starred_only:
        filter_params["starred_only"] = "true"
    if archived_only:
        filter_params["archived_only"] = "true"
    if labeled_only:
        filter_params["labeled_only"] = "true"
    if q and q.strip():
        filter_params["q"] = q.strip()
    if is_search:
        filter_params["sort"] = sort_order
        if read_status:
            filter_params["read_status"] = read_status
        if label_filter:
            filter_params["label_filter"] = label_filter

    extra_ctx = {}
    if settings and getattr(settings, 'ai_chat_enabled', False):
        extra_ctx["chat_article_ids"] = await _get_chat_article_ids(
            user.id, [a.id for a in articles], db
        )

    return templates.TemplateResponse(request, "app/partials/article_list_append.html", {
        "articles": articles,
        "density": density,
        "label_display": label_display,
        "show_ai_score": settings.ai_score_show_in_list if settings else False,
        "has_more": has_more,
        "more_qs": _build_more_qs(filter_params, articles, q, offset + len(articles)),
        **extra_ctx,
    })


@router.get("/htmx/articles/{article_id}", response_class=HTMLResponse)
async def htmx_article_detail(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Auto-trigger readable extraction if feed has it enabled and article wasn't extracted yet
    trigger_row = (await db.execute(
        select(
            Article.readable_status,
            Article.url,
            Feed.fetch_auth_user,
            Feed.fetch_auth_pass_encrypted,
            UserFeed.extract_readable,
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .where(Article.id == article_id)
    )).first()

    if (
        trigger_row is not None
        and trigger_row.extract_readable
        and trigger_row.readable_status == "skipped"
        and trigger_row.url
    ):
        await db.execute(
            sa_update(Article).where(Article.id == article_id).values(readable_status="pending")
        )
        await db.commit()
        asyncio.create_task(_extract_readable_bg(
            article_id,
            trigger_row.url,
            trigger_row.fetch_auth_user,
            trigger_row.fetch_auth_pass_encrypted,
        ))

    article = await get_article(user, article_id, db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-4'>Article not found.</p>", status_code=404)
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    ai_avail = bool(ai_on and settings and settings.ai_quality_provider and settings.ai_quality_model)
    summary_pending = False
    if ai_avail and not article.ai_summary:
        summary_pending = bool(await db.scalar(
            select(ArticleAiJob.id).where(
                ArticleAiJob.article_id == article_id,
                ArticleAiJob.user_id == user.id,
                ArticleAiJob.operation == "summary",
                ArticleAiJob.status == "pending",
            )
        ))
    chat_available = bool(ai_avail and settings and getattr(settings, 'ai_chat_enabled', False))
    chat_messages: list[dict] = []
    if chat_available:
        existing_chat = await db.scalar(
            select(ArticleAiChat).where(
                ArticleAiChat.article_id == article_id,
                ArticleAiChat.user_id == user.id,
            )
        )
        if existing_chat and existing_chat.messages:
            chat_messages = list(existing_chat.messages)
    return templates.TemplateResponse(request, "app/partials/article_detail.html", {
        "article": article,
        "mark_read_on_scroll": mark_read_on_scroll,
        "ai_available": ai_avail,
        "summary_pending": summary_pending,
        "chat_available": chat_available,
        "chat_messages": chat_messages,
    })


@router.get("/htmx/articles/{article_id}/readable-poll", response_class=HTMLResponse)
async def htmx_readable_poll(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Polling endpoint: returns article content fragment. HTMX polling stops once status leaves 'pending'."""
    article = await get_article(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)
    return _content_with_readtime_oob(request, article)


async def _get_row_context(user, request: Request, db) -> dict:
    """Return density and label_display for article row rendering."""
    s = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()
    ua = request.headers.get("user-agent", "")
    is_mobile = any(x in ua.lower() for x in ("mobile", "android", "iphone", "ipad"))
    density = ((s.list_density_mobile if is_mobile else s.list_density_web) if s else "comfortable")
    label_display = s.label_display if s else "indicator"
    show_ai_score = s.ai_score_show_in_list if s else False
    return {"density": density, "label_display": label_display, "show_ai_score": show_ai_score}


def _content_with_readtime_oob(request: Request, article) -> HTMLResponse:
    """Return article_content.html + OOB span to update the reading-time metadata."""
    content_html = templates.env.get_template("app/partials/article_content.html").render(
        request=request, article=article, chat_available=False
    )
    read_time = f"· {article.estimated_read_min} min read" if article.estimated_read_min else ""
    oob = (
        f'<span id="article-meta-readtime-{article.id}" class="shrink-0"'
        f' hx-swap-oob="true">{read_time}</span>'
    )
    return HTMLResponse(content_html + oob)


def _read_response(request: Request, article, density: str, label_display: str, **_) -> HTMLResponse:
    """Return read button HTML + JS class toggle via HX-Trigger (no OOB row swap to avoid flicker)."""
    import json
    btn_html = templates.env.get_template("app/partials/read_button.html").render(
        article=article, request=request
    )
    response = HTMLResponse(btn_html)
    response.headers["HX-Trigger"] = json.dumps({
        "sidebarRefresh": True,
        "articleReadChanged": {"id": article.id, "isRead": article.is_read},
    })
    return response


def _star_response(request: Request, article) -> HTMLResponse:
    """Return star icon HTML + JS class toggle via HX-Trigger (no OOB row swap to avoid flicker)."""
    import json
    btn_html = templates.env.get_template("app/partials/star_icon.html").render(
        article=article, request=request
    )
    response = HTMLResponse(btn_html)
    response.headers["HX-Trigger"] = json.dumps({
        "sidebarRefresh": True,
        "articleStarChanged": {"id": article.id, "isStarred": article.is_starred},
    })
    return response


def _archive_response(request: Request, article) -> HTMLResponse:
    """Return archive button HTML + JS class toggle via HX-Trigger (no OOB row swap to avoid flicker)."""
    import json
    btn_html = templates.env.get_template("app/partials/archive_button.html").render(
        article=article, request=request
    )
    response = HTMLResponse(btn_html)
    response.headers["HX-Trigger"] = json.dumps({
        "sidebarRefresh": True,
        "articleArchiveChanged": {"id": article.id, "isArchived": article.is_archived},
    })
    return response


@router.post("/htmx/articles/{article_id}/read", response_class=HTMLResponse)
async def htmx_toggle_read(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(user, article_id, "is_read", db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    ctx = await _get_row_context(user, request, db)
    return _read_response(request, article, **ctx)


@router.post("/htmx/articles/{article_id}/set-read", response_class=HTMLResponse)
async def htmx_set_read(
    article_id: int,
    request: Request,
    state: bool = Query(True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await update_article_state(user, article_id, ArticleStateUpdate(is_read=state), db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    ctx = await _get_row_context(user, request, db)
    return _read_response(request, article, **ctx)


@router.post("/htmx/articles/{article_id}/dwell")
async def htmx_article_dwell(
    article_id: int,
    seconds: int = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    seconds = max(0, min(seconds, 1800))  # cap at 30 minutes per session
    if seconds <= 3:
        return HTMLResponse("", status_code=204)
    if not await filter_accessible_article_ids(user.id, [article_id], db):
        return HTMLResponse("", status_code=404)
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    # Upsert: an article read in the detail panel has no state row yet (mark-read
    # fires later on scroll-off), so a plain UPDATE would silently drop the dwell.
    stmt = (
        pg_insert(UserArticleState)
        .values(user_id=user.id, article_id=article_id, dwell_seconds=seconds)
        .on_conflict_do_update(
            index_elements=["user_id", "article_id"],
            set_={"dwell_seconds": UserArticleState.dwell_seconds + seconds},
        )
    )
    await db.execute(stmt)
    await db.commit()
    return HTMLResponse("", status_code=204)


@router.post("/htmx/articles/{article_id}/link-opened")
async def htmx_article_link_opened(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.article_id == article_id,
            UserArticleState.user_id == user.id,
        )
    )
    if state is not None and not state.link_opened:
        state.link_opened = True
        await db.commit()
    return HTMLResponse("", status_code=204)


@router.post("/htmx/articles/{article_id}/star", response_class=HTMLResponse)
async def htmx_toggle_star(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(user, article_id, "is_starred", db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)

    if article.is_starred:
        settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
        if settings and settings.ai_summary_enabled_default:
            article_obj = await db.scalar(select(Article).where(Article.id == article_id))
            if article_obj is not None:
                from app.services.ai_summary_service import enqueue_summary_job
                enqueued = await enqueue_summary_job(article_obj, user.id, db)
                await db.commit()
                if enqueued:
                    asyncio.create_task(_summary_after_star_bg(article_id, user.id))
    else:
        # Unstarred — cancel a not-yet-run summary job so a mis-click doesn't
        # produce (and bill) a summary via the debounce task or the batch worker.
        await db.execute(
            sa_delete(ArticleAiJob).where(
                ArticleAiJob.article_id == article_id,
                ArticleAiJob.user_id == user.id,
                ArticleAiJob.operation == "summary",
                ArticleAiJob.status == "pending",
            )
        )
        await db.commit()

    return _star_response(request, article)


@router.post("/htmx/articles/{article_id}/archive", response_class=HTMLResponse)
async def htmx_toggle_archive(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(user, article_id, "is_archived", db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    return _archive_response(request, article)


@router.get("/htmx/articles/{article_id}/labels", response_class=HTMLResponse)
async def htmx_article_labels(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    all_labels = await list_labels(user, db)
    assigned: set[int] = set((await db.execute(
        select(ArticleLabel.label_id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user.id)
    )).scalars())
    return templates.TemplateResponse(request, "app/partials/label_picker.html", {
        "article_id": article_id,
        "all_labels": all_labels,
        "assigned": assigned,
        "show_oob": False,
        "assigned_labels": [],
    })


@router.post("/htmx/articles/{article_id}/labels/{label_id}/toggle", response_class=HTMLResponse)
async def htmx_toggle_article_label(
    article_id: int,
    label_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.label import Label
    from app.services.label_service import assign_label, remove_label

    existing = (await db.execute(
        select(ArticleLabel).where(
            ArticleLabel.article_id == article_id,
            ArticleLabel.label_id == label_id,
            ArticleLabel.user_id == user.id,
        )
    )).scalar_one_or_none()

    if existing:
        ok = await remove_label(user, article_id, label_id, db)
    else:
        ok = await assign_label(user, article_id, label_id, db)

    if not ok:
        return HTMLResponse("", status_code=404)

    all_labels = await list_labels(user, db)
    assigned_labels_rows = (await db.execute(
        select(Label.id, Label.name, Label.color)
        .join(ArticleLabel, ArticleLabel.label_id == Label.id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user.id)
        .order_by(Label.position, Label.name)
    )).all()
    assigned_labels = [{"id": r[0], "name": r[1], "color": r[2]} for r in assigned_labels_rows]
    assigned: set[int] = {l["id"] for l in assigned_labels}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    label_display = settings.label_display if settings else "indicator"

    picker_html = templates.env.get_template("app/partials/label_picker.html").render(
        request=request,
        article_id=article_id,
        all_labels=all_labels,
        assigned=assigned,
        show_oob=True,
        assigned_labels=assigned_labels,
        label_display=label_display,
    )
    response = HTMLResponse(picker_html)
    response.headers["HX-Trigger"] = "sidebarRefresh"
    return response


@router.post("/htmx/articles/{article_id}/share", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_share_token)
async def htmx_toggle_share(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle share token for an article. Generates on first call, revokes on second."""
    # Load article access + state
    stmt = (
        select(Article, UserArticleState)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id),
        )
        .where(
            Article.id == article_id,
            (UserFeed.id != None)
            | (UserArticleState.is_starred == True)
            | (UserArticleState.is_archived == True),
        )
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)

    article, state = row
    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)

    if state.share_token:
        state.share_token = None
        share_url = None
    else:
        state.share_token = secrets.token_urlsafe(24)
        share_url = str(request.base_url) + f"share/{state.share_token}"

    await db.commit()
    await db.refresh(state)

    return templates.TemplateResponse(request, "app/partials/share_button.html", {
        "article": type("A", (), {"id": article_id, "share_token": state.share_token})(),
        "share_url": share_url,
    })


@router.post("/htmx/articles/{article_id}/extract-readable", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_extract_readable)
async def htmx_extract_readable(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extract readable content on demand for a single article."""
    import asyncio
    from app.services.readable_service import extract_readable
    from app.utils.crypto import decrypt

    stmt = (
        select(Article, Feed.fetch_auth_user, Feed.fetch_auth_pass_encrypted)
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id),
        )
        .where(
            Article.id == article_id,
            (UserFeed.id != None)
            | (UserArticleState.is_starred == True)
            | (UserArticleState.is_archived == True),
        )
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)

    article, auth_user, auth_pass_enc = row
    if not article.url:
        return HTMLResponse("<p class='text-amber-500 p-2 text-xs'>Article has no URL.</p>")

    if article.readable_status == "success":
        return HTMLResponse("")  # already done, nothing to do

    auth_pass: str | None = None
    if auth_pass_enc:
        try:
            auth_pass = decrypt(auth_pass_enc)
        except Exception:
            # Matches the background path: a decrypt failure signals ENCRYPTION_KEY
            # drift / corruption, so log it rather than silently fetch without auth.
            logger.warning("readable: decrypt failed for article %d", article.id)

    loop = asyncio.get_running_loop()
    content, error, http_status = await loop.run_in_executor(
        None, extract_readable, article.url, auth_user, auth_pass
    )

    apply_readable_result(article, content, error, http_status)
    await db.commit()
    await db.refresh(article)

    return _content_with_readtime_oob(request, article)


def _ai_available(settings: UserSettings | None) -> bool:
    if settings is None:
        return False
    # TODO: respect settings.ai_enabled as per-user killswitch once multi-user demand is confirmed
    return bool(settings.ai_quality_provider and settings.ai_quality_model)


async def _get_article_access(user: User, article_id: int, db: AsyncSession):
    """Return Article ORM object if user has access, else None."""
    stmt = (
        select(Article)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id),
        )
        .where(
            Article.id == article_id,
            (UserFeed.id != None)
            | (UserArticleState.is_starred == True)
            | (UserArticleState.is_archived == True),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _ai_summary_block(article_id: int, summary: str) -> str:
    return (
        f'<div id="ai-summary-{article_id}" '
        f'class="border-l-2 border-blue-400 dark:border-blue-500 pl-4 text-gray-700 dark:text-gray-300">'
        f'<div class="text-xs font-semibold text-blue-500 dark:text-blue-400 mb-1">AI summary</div>'
        f'<div class="prose dark:prose-invert max-w-none ai-text">{_md_render(summary)}</div>'
        f'</div>'
    )


def _ai_context_block(article_id: int, context: str) -> str:
    return (
        f'<div id="ai-context-{article_id}" '
        f'class="border-l-2 border-amber-400 dark:border-amber-500 pl-4 text-gray-700 dark:text-gray-300">'
        f'<div class="text-xs font-semibold text-amber-500 dark:text-amber-400 mb-1">AI context</div>'
        f'<div class="prose dark:prose-invert max-w-none ai-text">{_md_render(context)}</div>'
        f'</div>'
    )


_CHAT_MAX_MESSAGES = 10  # 5 user + 5 assistant turns


def _chat_messages_html(container_id: str, messages: list[dict]) -> str:
    parts = [f'<div id="{container_id}" class="flex-1 overflow-y-auto space-y-3 mb-3 min-h-0">']
    for msg in messages:
        if msg["role"] == "user":
            parts.append(
                f'<div class="flex justify-end">'
                f'<div class="max-w-[85%] bg-blue-50 dark:bg-blue-900/30 '
                f'border border-blue-100 dark:border-blue-800 rounded-lg px-3 py-2 text-sm '
                f'text-gray-800 dark:text-gray-200">'
                f'{html_module.escape(msg["content"])}</div></div>'
            )
        else:
            parts.append(
                f'<div class="flex justify-start">'
                f'<div class="max-w-[85%] bg-gray-50 dark:bg-gray-800 '
                f'border border-gray-100 dark:border-gray-700 rounded-lg px-3 py-2 '
                f'prose prose-sm dark:prose-invert max-w-none ai-text">'
                f'{_md_render(msg["content"])}</div></div>'
            )
    parts.append('</div>')
    return ''.join(parts)


def _chat_input_html(
    *,
    input_id: str,
    include_id: str,
    area_id: str,
    post_url: str,
    hx_include_extra: str = "",
    include_article: bool = True,
    placeholder: str = "Ask a question…",
    input_extra_attr: str = "",
    attach_btn_id: str = "",
    attach_visible: bool = True,
    attach_tooltip: str = "Attach article",
    attach_title_id: str = "",
    attach_title_text: str = "",
    submit_id: str = "",
    error: str = "",
) -> str:
    article_chk = 'checked' if include_article else ''
    hx_include = f"#{input_id},#{include_id}{hx_include_extra}"
    submit_id_attr = f'id="{submit_id}" ' if submit_id else ''
    input_extra = f' {input_extra_attr}' if input_extra_attr else ''
    attach_btn_id_attr = f'id="{attach_btn_id}" ' if attach_btn_id else ''
    attach_title_id_attr = f'id="{attach_title_id}" ' if attach_title_id else ''
    attach_hidden_cls = '' if attach_visible else 'hidden '
    attach_color = 'text-blue-500' if include_article else 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300'
    error_html = f'<p class="text-xs text-red-500 py-1">{html_module.escape(error)}</p>' if error else ''
    return (
        f'{error_html}'
        f'<div class="flex-shrink-0 pt-2 border-t border-gray-100 dark:border-gray-700">'
        f'<textarea id="{input_id}" name="message" rows="3" '
        f'placeholder="{html_module.escape(placeholder)}" '
        f'class="w-full text-sm border border-gray-200 dark:border-gray-600 '
        f'dark:bg-gray-800 dark:text-gray-200 rounded p-2 resize-none mb-1 sm:mb-2"'
        f'{input_extra}></textarea>'
        f'<div class="flex items-center pl-0.5">'
        f'<div class="flex items-center gap-1 min-w-0 flex-1">'
        f'<button type="button" {attach_btn_id_attr}'
        f'class="{attach_hidden_cls}w-6 h-6 flex items-center justify-center rounded {attach_color} '
        f'bg-transparent border-0 cursor-pointer flex-shrink-0" '
        f'title="{html_module.escape(attach_tooltip)}">'
        f'<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f'<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
        f'd="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656'
        f'l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"/>'
        f'</svg></button>'
        f'<span {attach_title_id_attr}'
        f'class="{attach_hidden_cls}text-xs text-gray-400 dark:text-gray-500 truncate">'
        f'{html_module.escape(attach_title_text)}</span>'
        f'<input type="checkbox" name="include_article" id="{include_id}" class="sr-only" {article_chk}>'
        f'</div>'
        f'<button {submit_id_attr}class="hidden" '
        f'hx-post="{post_url}" '
        f'hx-include="{hx_include}" '
        f'hx-target="#{area_id}" hx-swap="outerHTML"></button>'
        f'</div>'
        f'</div>'
    )


def _render_chat_area(article_id: int, messages: list[dict],
                      include_article: bool = True,
                      error: str = "",
                      article_title: str = "") -> str:
    short = (article_title[:25] + '…') if len(article_title) > 25 else article_title
    return (
        f'<div id="chat-area-{article_id}" '
        f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
        + _chat_messages_html(f"chat-messages-{article_id}", messages)
        + _chat_input_html(
            input_id=f"chat-input-{article_id}",
            include_id=f"chat-article-{article_id}",
            area_id=f"chat-area-{article_id}",
            post_url=f"/htmx/articles/{article_id}/ai-chat",
            include_article=include_article,
            placeholder="Ask a question about this article…",
            input_extra_attr=f'data-chat-input-id="{article_id}"',
            attach_btn_id=f"chat-attach-btn-{article_id}",
            attach_visible=True,
            attach_tooltip="Attach article",
            attach_title_id=f"chat-attach-title-{article_id}",
            attach_title_text=short,
            error=error,
        )
        + '</div>'
    )


def _ai_spinner(target_id: str, poll_url: str) -> str:
    return (
        f'<div id="{target_id}" '
        f'hx-get="{poll_url}" hx-trigger="every 30s" hx-swap="outerHTML" '
        f'class="flex items-center gap-2 text-sm text-gray-500 py-2">'
        f'<svg class="animate-spin h-4 w-4 text-blue-500" fill="none" viewBox="0 0 24 24">'
        f'<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>'
        f'<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z"/>'
        f'</svg>'
        f'Generating…'
        f'</div>'
    )


@router.post("/htmx/articles/{article_id}/ai-summary", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_summary)
async def htmx_ai_summary_trigger(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """On-demand: run summary synchronously and return result block."""
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    if not ai_on:
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-gray-400 py-1">AI is disabled.</div>'
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-gray-400 py-1">Quality AI model not configured.</div>'
        )

    article = await _get_article_access(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)

    from app.services.ai_summary_service import _normalize_content, _MIN_CONTENT_CHARS, run_summary_on_demand
    content_text = _normalize_content(article.title, article.readable_content or article.content, settings.ai_content_limit)
    if len(content_text) < _MIN_CONTENT_CHARS:
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-gray-400 py-1">'
            f'Article is too short for a summary (minimum {_MIN_CONTENT_CHARS} characters).'
            f'</div>'
        )

    import html as _html
    summary, error = await run_summary_on_demand(article, user.id, db)
    if summary is None:
        msg = _html.escape(error) if error else "Summary unavailable."
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-red-500 py-1">Summary failed: {msg}</div>'
        )
    return HTMLResponse(_ai_summary_block(article_id, summary))


@router.get("/htmx/articles/{article_id}/ai-summary/poll", response_class=HTMLResponse)
async def htmx_ai_summary_poll(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll summary job status and return final block or keep spinner."""
    job = await db.scalar(
        select(ArticleAiJob).where(
            ArticleAiJob.article_id == article_id,
            ArticleAiJob.user_id == user.id,
            ArticleAiJob.operation == "summary",
        )
    )

    if job is None or job.status == "pending":
        return HTMLResponse(_ai_spinner(f"ai-summary-{article_id}", f"/htmx/articles/{article_id}/ai-summary/poll"))

    if job.status == "failed":
        msg = html_module.escape((job.error_message or "Unknown error")[:120])
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-red-500 py-1">Summary failed: {msg}</div>'
        )

    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article_id,
        )
    )
    if state and state.ai_summary:
        return HTMLResponse(_ai_summary_block(article_id, state.ai_summary))

    return HTMLResponse(f'<div id="ai-summary-{article_id}"></div>')


@router.post("/htmx/articles/{article_id}/ai-context", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_context)
async def htmx_ai_context_trigger(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """On-demand: call AI directly and return context block (synchronous, may take several seconds)."""
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    if not ai_on:
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-gray-400 py-1">AI is disabled.</div>'
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-gray-400 py-1">Quality AI model not configured.</div>'
        )

    article = await _get_article_access(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)

    from app.services.ai_summary_service import _normalize_content, _MIN_CONTENT_CHARS
    content_text = _normalize_content(article.title, article.readable_content or article.content, settings.ai_content_limit)
    if len(content_text) < _MIN_CONTENT_CHARS:
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-gray-400 py-1">'
            f'Article is too short for context generation (minimum {_MIN_CONTENT_CHARS} characters).'
            f'</div>'
        )

    form = await request.form()
    focus = (form.get("focus") or "").strip() or None

    from app.services.ai_service import get_ai_client, get_article_context
    client, provider, model = await get_ai_client(user.id, "quality", db)
    if client is None:
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-gray-400 py-1">Quality AI model not configured.</div>'
        )

    try:
        result, in_tok, out_tok = await get_article_context(
            content_text, client, provider, model,
            base_prompt=settings.ai_context_prompt,
            focus=focus,
        )
    except Exception as exc:
        msg = html_module.escape(str(exc)[:120])
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-red-500 py-1">Context failed: {msg}</div>'
        )

    now = datetime.now(timezone.utc)
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article_id,
        )
    )
    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)
    state.ai_context = result

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    await db.execute(
        pg_insert(ArticleAiJob).values(
            article_id=article_id,
            user_id=user.id,
            operation="context",
            status="success",
            input_tokens=in_tok,
            output_tokens=out_tok,
            processed_at=now,
        ).on_conflict_do_update(
            index_elements=["article_id", "user_id", "operation"],
            set_={"status": "success", "input_tokens": in_tok, "output_tokens": out_tok, "processed_at": now},
        )
    )
    await db.commit()

    return HTMLResponse(_ai_context_block(article_id, result))


async def _get_chat_article_ids(user_id: int, article_ids: list[int], db: AsyncSession) -> set[int]:
    if not article_ids:
        return set()
    rows = await db.execute(
        select(ArticleAiChat.article_id).where(
            ArticleAiChat.user_id == user_id,
            ArticleAiChat.article_id.in_(article_ids),
            func.jsonb_array_length(ArticleAiChat.messages) > 0,
        )
    )
    return {r[0] for r in rows.all()}


def _render_general_chat_area(messages: list[dict], error: str = "") -> str:
    history_json = html_module.escape(json.dumps(messages, ensure_ascii=False))
    extra_inputs = (
        f'<input type="hidden" id="general-chat-history" name="history" value="{history_json}">'
        f'<input type="hidden" id="general-chat-article-id" name="article_id" value="">'
    )
    return (
        f'<div id="general-chat-area" '
        f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
        + extra_inputs
        + _chat_messages_html("general-chat-messages", messages)
        + _chat_input_html(
            input_id="general-chat-input",
            include_id="general-chat-include-article",
            area_id="general-chat-area",
            post_url="/htmx/ai-chat",
            hx_include_extra=",#general-chat-history,#general-chat-article-id",
            include_article=False,
            placeholder="Ask a question…",
            input_extra_attr="data-general-chat-input",
            attach_btn_id="general-chat-attach-btn",
            attach_visible=False,
            attach_tooltip="Attach article",
            attach_title_id="general-chat-attach-title",
            attach_title_text="",
            submit_id="general-chat-submit",
            error=error,
        )
        + '</div>'
    )


@router.post("/htmx/ai-chat", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_chat)
async def htmx_general_ai_chat(
    request: Request,
    message: str = Form(...),
    include_article: str = Form(""),
    history: str = Form("[]"),
    article_id: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    if not ai_on:
        return HTMLResponse(
            f'<div id="general-chat-area" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">AI is disabled.</p></div>'
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return HTMLResponse(
            f'<div id="general-chat-area" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">Quality AI model not configured.</p></div>'
        )
    if not getattr(settings, 'ai_chat_enabled', False):
        return HTMLResponse("", status_code=403)

    msg_text = message.strip()[:2000]
    if not msg_text:
        return HTMLResponse("", status_code=400)

    try:
        current_messages: list[dict] = json.loads(history)
        if not isinstance(current_messages, list):
            current_messages = []
    except (json.JSONDecodeError, ValueError):
        current_messages = []

    current_messages.append({"role": "user", "content": msg_text})
    if len(current_messages) > _CHAT_MAX_MESSAGES:
        current_messages = current_messages[-_CHAT_MAX_MESSAGES:]

    tier = "quality"
    use_article = (include_article == "on")

    article_ctx = None
    art_id: int | None = None
    article = None
    if use_article and article_id.strip().isdigit():
        art_id = int(article_id)
        article = await _get_article_access(user, art_id, db)
        if article:
            from app.services.ai_summary_service import _normalize_content
            article_ctx = _normalize_content(
                article.title,
                article.readable_content or article.content,
                settings.ai_content_limit,
            )

    from app.services.ai_service import get_ai_client, chat_with_article
    client, provider, model = await get_ai_client(user.id, tier, db)
    if client is None:
        return HTMLResponse(
            _render_general_chat_area(
                current_messages[:-1],
                error="Quality AI model not configured.",
            )
        )

    try:
        response_text, in_tok, out_tok = await chat_with_article(current_messages, article_ctx, client, provider, model)
    except Exception as exc:
        exc_str = str(exc)
        status = getattr(exc, "status_code", None)
        if status == 529 or "529" in exc_str or "overloaded" in exc_str.lower():
            err_msg = "AI provider is overloaded — please try again in a moment."
        elif status == 429 or "429" in exc_str or "rate_limit" in exc_str.lower():
            err_msg = "Rate limit reached — please wait a moment and try again."
        elif status and status >= 500:
            err_msg = "AI provider returned a server error — please try again."
        else:
            err_msg = "Chat failed — please try again."
        return HTMLResponse(
            _render_general_chat_area(current_messages[:-1], error=err_msg)
        )

    current_messages.append({"role": "assistant", "content": response_text})
    if len(current_messages) > _CHAT_MAX_MESSAGES:
        current_messages = current_messages[-_CHAT_MAX_MESSAGES:]

    if use_article and art_id and article:
        chat_record = await db.scalar(
            select(ArticleAiChat).where(
                ArticleAiChat.user_id == user.id,
                ArticleAiChat.article_id == art_id,
            )
        )
        if chat_record is None:
            chat_record = ArticleAiChat(user_id=user.id, article_id=art_id, messages=[])
            db.add(chat_record)
        saved = list(chat_record.messages or [])
        saved.append({"role": "user", "content": msg_text})
        saved.append({"role": "assistant", "content": response_text})
        if len(saved) > _CHAT_MAX_MESSAGES:
            saved = saved[-_CHAT_MAX_MESSAGES:]
        chat_record.messages = saved
        chat_record.total_input_tokens = (chat_record.total_input_tokens or 0) + in_tok
        chat_record.total_output_tokens = (chat_record.total_output_tokens or 0) + out_tok
        chat_record.updated_at = datetime.now(timezone.utc)
    else:
        from app.models.article import GeneralChatLog
        db.add(GeneralChatLog(user_id=user.id, input_tokens=in_tok, output_tokens=out_tok))
    await db.commit()

    return HTMLResponse(_render_general_chat_area(current_messages))


@router.delete("/htmx/ai-chat", response_class=HTMLResponse)
async def htmx_general_ai_chat_clear(
    user: User = Depends(get_current_user),
):
    return HTMLResponse(_render_general_chat_area([]))


@router.post("/htmx/articles/{article_id}/ai-chat", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_chat)
async def htmx_ai_chat(
    article_id: int,
    request: Request,
    message: str = Form(...),
    include_article: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.settings import AppSettings as _AS
    ai_on = await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1))
    if not ai_on:
        return HTMLResponse(
            f'<div id="chat-area-{article_id}" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">AI is disabled.</p></div>'
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return HTMLResponse(
            f'<div id="chat-area-{article_id}" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">Quality AI model not configured.</p></div>'
        )
    if not getattr(settings, 'ai_chat_enabled', False):
        return HTMLResponse("", status_code=403)

    msg_text = message.strip()
    if not msg_text:
        return HTMLResponse("", status_code=400)

    article = await _get_article_access(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)

    chat = await db.scalar(
        select(ArticleAiChat).where(
            ArticleAiChat.user_id == user.id,
            ArticleAiChat.article_id == article_id,
        )
    )
    if chat is None:
        chat = ArticleAiChat(user_id=user.id, article_id=article_id, messages=[])
        db.add(chat)

    current_messages: list[dict] = list(chat.messages or [])
    current_messages.append({"role": "user", "content": msg_text})

    tier = "quality"
    use_article = (include_article == "on")

    article_ctx = None
    if use_article:
        from app.services.ai_summary_service import _normalize_content
        article_ctx = _normalize_content(article.title, article.readable_content or article.content, settings.ai_content_limit)

    from app.services.ai_service import get_ai_client, chat_with_article
    client, provider, model = await get_ai_client(user.id, tier, db)
    title = article.title or ""
    if client is None:
        return HTMLResponse(_render_chat_area(
            article_id, current_messages[:-1], use_article,
            error="Quality AI model not configured.",
            article_title=title,
        ))

    try:
        response_text, in_tok, out_tok = await chat_with_article(current_messages, article_ctx, client, provider, model)
    except Exception as exc:
        exc_str = str(exc)
        status = getattr(exc, "status_code", None)
        if status == 529 or "529" in exc_str or "overloaded" in exc_str.lower():
            err_msg = "AI provider is overloaded — please try again in a moment."
        elif status == 429 or "429" in exc_str or "rate_limit" in exc_str.lower():
            err_msg = "Rate limit reached — please wait a moment and try again."
        elif status and status >= 500:
            err_msg = "AI provider returned a server error — please try again."
        else:
            err_msg = "Chat failed — please try again."
        return HTMLResponse(_render_chat_area(
            article_id, current_messages[:-1], use_article,
            error=err_msg, article_title=title,
        ))

    current_messages.append({"role": "assistant", "content": response_text})
    if len(current_messages) > _CHAT_MAX_MESSAGES:
        current_messages = current_messages[-_CHAT_MAX_MESSAGES:]
    chat.messages = current_messages  # reassign — SQLAlchemy JSONB change tracking
    chat.total_input_tokens = (chat.total_input_tokens or 0) + in_tok
    chat.total_output_tokens = (chat.total_output_tokens or 0) + out_tok
    chat.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return HTMLResponse(_render_chat_area(article_id, current_messages, use_article,
                                          article_title=title))


@router.delete("/htmx/articles/{article_id}/ai-chat", response_class=HTMLResponse)
async def htmx_ai_chat_clear(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    chat = await db.scalar(
        select(ArticleAiChat).where(
            ArticleAiChat.user_id == user.id,
            ArticleAiChat.article_id == article_id,
        )
    )
    if chat is not None:
        chat.messages = []
        chat.updated_at = datetime.now(timezone.utc)
        await db.commit()
    return HTMLResponse(_render_chat_area(article_id, []))


# ── Catch me up ──────────────────────────────────────────────────────────────

def _catchup_available(ai_on: bool, settings: UserSettings | None) -> bool:
    if not ai_on or not settings:
        return False
    return bool(settings.ai_fast_provider or settings.ai_quality_provider)


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

    ai_on = bool(await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1)))
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

    def _period_desc(period: str) -> str:
        start = _period_to_start_dt(period, tz_str).astimezone(_tz)
        today_date = datetime.now(_tz).date()
        days = (today_date - start.date()).days + 1
        day_label = f"{days} day{'s' if days != 1 else ''}"
        return f"from {start.strftime('%d.%m')} 00:00 · {day_label}"

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
        "saved_configs": saved_configs,
        "default_catchup_prompt": _DEFAULT_CATCHUP_PROMPT,
        "period_descs": period_descs,
        "smtp_available": smtp_available,
    })


@router.get("/htmx/catch-me-up/count", response_class=HTMLResponse)
async def htmx_catchup_count(
    request: Request,
    period: str = Query("7days"),
    filter_status: str = Query("all"),
    filter_labeled: bool = Query(False),
    filter_score_min: float | None = Query(None),
    scope_include: str | None = Query(None),
    article_limit: int = Query(500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article_limit = max(1, min(article_limit, 500))
    from app.services.catchup_service import fetch_catchup_articles

    settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()
    tz_str = settings.timezone if settings else "UTC"

    articles = await fetch_catchup_articles(
        user_id=user.id, tz_str=tz_str, db=db,
        period=period, scope_include=scope_include,
        filter_status=filter_status, filter_labeled=filter_labeled,
        filter_score_min=filter_score_min / 100 if filter_score_min is not None else None,
    )
    count = len(articles)
    if count > article_limit:
        return HTMLResponse(f'<span>{count} articles <span class="text-gray-400">({article_limit} will be used)</span></span>')
    return HTMLResponse(f'<span>{count} articles</span>')


@router.get("/htmx/catch-me-up/cost", response_class=HTMLResponse)
async def htmx_catchup_cost(
    request: Request,
    article_limit: int = Query(500),
    model_slot: str = Query("fast"),
    include_snippet: bool = Query(True),
    period: str = Query("7days"),
    filter_status: str = Query("all"),
    filter_labeled: bool = Query(False),
    filter_score_min: float | None = Query(None),
    scope_include: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article_limit = max(1, min(article_limit, 500))
    from app.services.catchup_service import estimate_catchup_tokens, fetch_catchup_articles
    from app.services.ai_service import get_ai_client
    from app.services.stats_service import _calc_cost

    try:
        client, provider, model = await get_ai_client(user.id, model_slot, db)
    except Exception:
        return HTMLResponse('<span class="text-gray-400">Configure AI model in settings to see cost estimate</span>')

    settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()
    tz_str = settings.timezone if settings else "UTC"
    articles = await fetch_catchup_articles(
        user_id=user.id, tz_str=tz_str, db=db,
        period=period, scope_include=scope_include,
        filter_status=filter_status, filter_labeled=filter_labeled,
        filter_score_min=filter_score_min / 100 if filter_score_min is not None else None,
    )
    effective_count = min(len(articles), article_limit)

    input_tokens, output_tokens = estimate_catchup_tokens(effective_count, include_snippet)
    cost = _calc_cost(model, input_tokens, output_tokens)
    if cost is None:
        return HTMLResponse("")

    slot_label = "fast" if model_slot == "fast" else "quality"
    return HTMLResponse(
        f'<span class="text-gray-500 text-sm">Estimated cost: ~${cost:.4f} '
        f'<span class="text-gray-400">({effective_count} articles × {slot_label} model)</span></span>'
    )


@router.post("/htmx/catch-me-up/generate", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_catchup)
async def htmx_catchup_generate(
    request: Request,
    period: str = Form("7days"),
    filter_status: str = Form("all"),
    filter_labeled: bool = Form(False),
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
    from app.models.settings import AppSettings as _AS
    from app.models.user import CatchupLog
    from app.services.ai_service import catch_me_up, get_ai_client
    from app.services.catchup_service import (
        apply_catchup_limit, build_articles_meta, fetch_catchup_articles,
        populate_snippet_sources, validate_scope,
    )

    ai_on = bool(await db.scalar(select(_AS.ai_enabled).where(_AS.id == 1)))
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
            filter_status=filter_status, filter_labeled=filter_labeled,
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
    filter_labeled: bool = Form(False),
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
        config.filter_labeled = filter_labeled
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
            filter_labeled=filter_labeled,
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
    filter_labeled: bool = Form(False),
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
    config.filter_labeled = filter_labeled
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


# ── User feedback / bug report ───────────────────────────────────────────────
_FEEDBACK_TYPES = {"bug", "feedback", "other"}
_FEEDBACK_SUBJECT_MAX = 200
_FEEDBACK_MESSAGE_MAX = 5000


async def _feedback_settings(db: AsyncSession):
    """Return (AppSettings|None, smtp_available, enabled) for the feedback feature."""
    from app.models.settings import AppSettings as _AS

    s = (await db.execute(select(_AS).where(_AS.id == 1))).scalar_one_or_none()
    smtp_available = bool(s and s.smtp_host and s.smtp_from_email)
    enabled = bool(s and s.feedback_enabled)
    return s, smtp_available, enabled


@router.get("/htmx/feedback", response_class=HTMLResponse)
async def htmx_feedback_modal_get(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, smtp_available, enabled = await _feedback_settings(db)
    if not (enabled and smtp_available):
        return HTMLResponse("Not available", status_code=403)
    return templates.TemplateResponse(request, "app/partials/feedback_modal.html", {})


@router.post("/htmx/feedback", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_feedback)
async def htmx_feedback_submit(
    request: Request,
    feedback_type: str = Form("feedback"),
    subject: str = Form(""),
    message: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone
    from app.utils.smtp import send_email

    ftype = (feedback_type or "").strip().lower()
    if ftype not in _FEEDBACK_TYPES:
        ftype = "feedback"
    # Collapse whitespace/newlines: subject becomes a single header line, so a
    # multi-line value would otherwise be rejected at send time (header folding).
    subject = " ".join((subject or "").split())
    message = (message or "").strip()

    def _form(error: str, status_code: int) -> HTMLResponse:
        """Re-render the form with the submitted values preserved + an error."""
        return templates.TemplateResponse(
            request, "app/partials/feedback_modal.html",
            {"error": error, "values": {"feedback_type": ftype, "subject": subject, "message": message}},
            status_code=status_code,
        )

    s, smtp_available, enabled = await _feedback_settings(db)
    if not (enabled and smtp_available):
        return _form("Feedback is not available.", status_code=403)

    if not subject or not message:
        return _form("Please fill in both a subject and a message.", status_code=400)
    if len(subject) > _FEEDBACK_SUBJECT_MAX or len(message) > _FEEDBACK_MESSAGE_MAX:
        return _form("Your subject or message is too long.", status_code=400)

    admin_emails = (await db.execute(
        select(User.email).where(User.role == "admin", User.email_verified == True)  # noqa: E712
    )).scalars().all()
    if not admin_emails:
        return _form("No administrator is available to receive feedback.", status_code=503)

    mail_subject = f"[Readfine {ftype}] {subject}"
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f"Type: {ftype}\n"
        f"From: {user.email} (user id {user.id})\n"
        f"Sent: {sent_at}\n"
        f"\n{message}\n"
    )

    try:
        # One SMTP transaction to all admins: avoids per-admin latency and the
        # partial-send case where some admins get the message and others don't.
        send_email(s, to=admin_emails, subject=mail_subject, body=body, reply_to=user.email)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to send feedback email: %s", e)
        return _form("Sorry, we couldn't send your message. Please try again later.", status_code=502)

    return templates.TemplateResponse(request, "app/partials/feedback_sent.html", {})


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
    import json as _json
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
        import re as _re
        _email_re = _re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
        for addr in raw_emails:
            if not _email_re.match(addr):
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
    config.briefing_recipients = _json.dumps(extra_emails) if extra_emails else None

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


@router.get("/share/{token}", response_class=HTMLResponse)
async def public_share_view(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Public article view — no authentication required."""
    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(UserArticleState, UserArticleState.article_id == Article.id)
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .where(UserArticleState.share_token == token)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return templates.TemplateResponse(request, "app/share_not_found.html", {}, status_code=404)

    article, feed_title = row
    return templates.TemplateResponse(request, "app/share.html", {
        "article": article,
        "feed_title": feed_title,
    })
