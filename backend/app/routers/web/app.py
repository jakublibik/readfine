"""Web routes for the main application UI."""
import asyncio
import html as html_module
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select, func, update as sa_update

logger = logging.getLogger(__name__)

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.rate_limit import limiter
from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.user import User, UserSettings
from app.schemas.article import ArticleStateUpdate
from app.services.article import get_article, list_articles, mark_articles_read_batch, mark_scope_read, toggle_article_state, update_article_state
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
        await db.commit()
        logger.info("readable bg: article %d → %s", article_id, article.readable_status)


@router.get("/app", response_class=HTMLResponse)
async def main_app(
    request: Request,
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
    return templates.TemplateResponse(request, "app/main.html", {
        "user": user,
        "bucket_small_max": bucket_small_max,
        "bucket_medium_max": bucket_medium_max,
        "reading_font_size": reading_font_size,
        "reading_font_family": reading_font_family,
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
        .where(UserFeed.user_id == user.id)
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
        select(func.count()).select_from(
            select(ArticleLabel.article_id).where(ArticleLabel.user_id == user.id).distinct().subquery()
        )
    )).scalar() or 0
    nav_unread_labeled = (await db.execute(
        select(func.count(Article.id.distinct()))
        .select_from(Article)
        .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
        .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id))
        .where((UserArticleState.is_read == None) | (UserArticleState.is_read == False))
    )).scalar() or 0

    # Feed total + unread counts (batch, computed from DB — not cached unread_count)
    if feed_ids:
        feed_total_counts = dict((await db.execute(
            select(Article.feed_id, func.count(Article.id))
            .where(Article.feed_id.in_(feed_ids))
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
            .where(ArticleLabel.user_id == user.id, ArticleLabel.label_id.in_(label_ids))
            .group_by(ArticleLabel.label_id)
        )).all())
        label_unread_counts = dict((await db.execute(
            select(ArticleLabel.label_id, func.count(ArticleLabel.article_id))
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user.id))
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id.in_(label_ids),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
            .group_by(ArticleLabel.label_id)
        )).all())
    else:
        label_counts = {}
        label_unread_counts = {}

    pinned = request.query_params.get("pinned", "true").lower() != "false"

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
    return HTMLResponse("", status_code=200)


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
    return HTMLResponse("", status_code=200)


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
    return HTMLResponse("", status_code=200)


_BADGE_UNREAD = '<span class="mark-read-badge ml-auto flex-shrink-0 text-xs font-medium bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full">{}</span>'
_BADGE_TOTAL  = '<span class="mark-read-badge ml-auto flex-shrink-0 text-xs text-gray-400 px-1.5 py-0.5">{}</span>'


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds = await list_user_feeds(user, db)

    seen_folder_ids: set[int] = set()
    folders: list[tuple[int, str]] = []
    for uf in user_feeds:
        if uf.folder_id and uf.folder_id not in seen_folder_ids:
            seen_folder_ids.add(uf.folder_id)
            folders.append((uf.folder_id, uf.folder.name))
    folders.sort(key=lambda x: x[1].lower())

    feeds = [(uf.feed_id, uf.custom_title or uf.feed.title) for uf in user_feeds]
    feeds.sort(key=lambda x: x[1].lower())

    return templates.TemplateResponse(request, "app/partials/search_modal.html", {
        "folders": folders,
        "feeds": feeds,
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


@router.get("/htmx/articles", response_class=HTMLResponse)
async def htmx_article_list(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    label_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    labeled_only: bool = Query(False),
    q: str | None = Query(None),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()

    sort_order = settings.default_sort_order if settings else "newest"
    articles_per_page = settings.articles_per_page if settings else 50
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    label_display = settings.label_display if settings else "indicator"
    ua = request.headers.get("user-agent", "")
    is_mobile = any(x in ua.lower() for x in ("mobile", "android", "iphone", "ipad"))
    density = (settings.list_density_mobile if is_mobile else settings.list_density_web) if settings else "comfortable"

    # Resolve effective unread filter
    if q or starred_only or archived_only:
        # Search and state-based views always show everything
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
                feed_id=feed_id, folder_id=folder_id, label_id=label_id,
                labeled_only=labeled_only,
                unread_only=True, limit=1,
            )
            effective_unread_only = len(probe) > 0

    articles = await list_articles(
        user=user,
        db=db,
        feed_id=feed_id,
        folder_id=folder_id,
        label_id=label_id,
        unread_only=effective_unread_only,
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

    extra_headers: dict[str, str] = {}
    if feed_id is not None:
        feed_obj = await db.get(Feed, feed_id)
        if feed_obj and feed_obj.status in ("error", "disabled") and feed_obj.last_error:
            extra_headers["HX-Trigger"] = json.dumps(
                {"showToast": {"msg": feed_obj.last_error[:150], "type": "warning"}}
            )

    list_html = templates.env.get_template("app/partials/article_list.html").render(
        request=request,
        articles=articles,
        feed_id=feed_id,
        folder_id=folder_id,
        unread_only=effective_unread_only,
        starred_only=starred_only,
        archived_only=archived_only,
        search_query=q.strip() if q and q.strip() else None,
        mark_read_on_scroll=mark_read_on_scroll,
        density=density,
        label_display=label_display,
        show_ai_score=settings.ai_score_show_in_list if settings else False,
        has_more=has_more,
        filter_qs=urlencode(filter_params),
        next_offset=len(articles),
        title_bar_count=title_bar_count,
        title_bar_count_type=title_bar_count_type,
    )
    oob = await _label_badge_oob(user.id, label_id, labeled_only, db)
    return HTMLResponse(list_html + oob, headers=extra_headers)


@router.get("/htmx/articles/more", response_class=HTMLResponse)
async def htmx_article_list_more(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    label_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    labeled_only: bool = Query(False),
    q: str | None = Query(None),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()

    sort_order = settings.default_sort_order if settings else "newest"
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
        label_id=label_id,
        unread_only=unread_only,
        starred_only=starred_only,
        archived_only=archived_only,
        labeled_only=labeled_only,
        q=q or None,
        sort_order=sort_order,
        limit=articles_per_page,
        offset=offset,
    )

    has_more = len(articles) >= articles_per_page
    filter_params: dict = {}
    if feed_id is not None:
        filter_params["feed_id"] = feed_id
    if folder_id is not None:
        filter_params["folder_id"] = folder_id
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

    return templates.TemplateResponse(request, "app/partials/article_list_append.html", {
        "articles": articles,
        "density": density,
        "label_display": label_display,
        "show_ai_score": settings.ai_score_show_in_list if settings else False,
        "has_more": has_more,
        "filter_qs": urlencode(filter_params),
        "next_offset": offset + len(articles),
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
    return templates.TemplateResponse(request, "app/partials/article_detail.html", {
        "article": article,
        "mark_read_on_scroll": mark_read_on_scroll,
        "ai_available": ai_avail,
        "summary_pending": summary_pending,
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
        request=request, article=article
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
    if seconds <= 3:
        return HTMLResponse("", status_code=204)
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.article_id == article_id,
            UserArticleState.user_id == user.id,
        )
    )
    if state is not None:
        state.dwell_seconds = state.dwell_seconds + seconds
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
                await enqueue_summary_job(article_obj, user.id, db)
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
            pass

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
        f'<p class="ai-text">{html_module.escape(summary)}</p>'
        f'</div>'
    )


def _ai_context_block(article_id: int, context: str) -> str:
    return (
        f'<div id="ai-context-{article_id}" '
        f'class="border-l-2 border-amber-400 dark:border-amber-500 pl-4 text-gray-700 dark:text-gray-300">'
        f'<div class="text-xs font-semibold text-amber-500 dark:text-amber-400 mb-1">AI context</div>'
        f'<p class="ai-text">{html_module.escape(context)}</p>'
        f'</div>'
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
    content_text = _normalize_content(article.title, article.readable_content or article.content)
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
        msg = (job.error_message or "Unknown error")[:120]
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
    content_text = _normalize_content(article.title, article.readable_content or article.content)
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
        result = await get_article_context(
            content_text, client, provider, model,
            base_prompt=settings.ai_context_prompt,
            focus=focus,
        )
    except Exception as exc:
        msg = str(exc)[:120]
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-red-500 py-1">Context failed: {msg}</div>'
        )

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
    await db.commit()

    return HTMLResponse(_ai_context_block(article_id, result))


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
