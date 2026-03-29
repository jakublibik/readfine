"""Web routes for the main application UI."""
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select, func

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.main import limiter
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.user import User, UserSettings
from app.schemas.article import ArticleStateUpdate
from app.services.article import get_article, list_articles, toggle_article_state, update_article_state
from app.services.feed import list_user_feeds
from app.services.label_service import list_labels

router = APIRouter(tags=["web-app"])
templates = Jinja2Templates(directory="app/templates")


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
    pinned = settings.left_panel_pinned if settings else True
    return templates.TemplateResponse(request, "app/main.html", {"user": user, "pinned": pinned})


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
    nav_starred = (await db.execute(
        select(func.count()).select_from(UserArticleState)
        .where(UserArticleState.user_id == user.id, UserArticleState.is_starred == True)  # noqa: E712
    )).scalar() or 0
    nav_unread_starred = (await db.execute(
        select(func.count()).select_from(UserArticleState)
        .where(UserArticleState.user_id == user.id, UserArticleState.is_starred == True, UserArticleState.is_read == False)  # noqa: E712
    )).scalar() or 0
    nav_archived = (await db.execute(
        select(func.count()).select_from(UserArticleState)
        .where(UserArticleState.user_id == user.id, UserArticleState.is_archived == True)  # noqa: E712
    )).scalar() or 0
    nav_unread_archived = (await db.execute(
        select(func.count()).select_from(UserArticleState)
        .where(UserArticleState.user_id == user.id, UserArticleState.is_archived == True, UserArticleState.is_read == False)  # noqa: E712
    )).scalar() or 0
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
        .where((UserArticleState.is_read == None) | (UserArticleState.is_read == False))  # noqa: E711
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
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),  # noqa: E711
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
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),  # noqa: E711
            )
            .group_by(ArticleLabel.label_id)
        )).all())
    else:
        label_counts = {}
        label_unread_counts = {}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    pinned = settings.left_panel_pinned if settings else True

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
    db: AsyncSession = Depends(get_db),
):
    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user.id)
        db.add(settings)
    settings.left_panel_pinned = not settings.left_panel_pinned
    await db.commit()
    pinned = settings.left_panel_pinned
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = json.dumps({
        "sidebarRefresh": True,
        "sidebarPinChanged": {"pinned": pinned},
    })
    return response


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
    ua = request.headers.get("user-agent", "")
    is_mobile = any(x in ua.lower() for x in ("mobile", "android", "iphone", "ipad"))
    density = (settings.list_density_mobile if is_mobile else settings.list_density_web) if settings else "comfortable"

    # Resolve effective unread filter
    if starred_only or archived_only:
        # State-based views always show everything
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
        sort_order=sort_order,
        limit=articles_per_page,
        offset=offset,
    )

    return templates.TemplateResponse(request, "app/partials/article_list.html", {
        "articles": articles,
        "feed_id": feed_id,
        "folder_id": folder_id,
        "unread_only": effective_unread_only,
        "starred_only": starred_only,
        "archived_only": archived_only,
        "mark_read_on_scroll": mark_read_on_scroll,
        "density": density,
    })


@router.get("/htmx/articles/{article_id}", response_class=HTMLResponse)
async def htmx_article_detail(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await get_article(user, article_id, db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-4'>Article not found.</p>", status_code=404)
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    return templates.TemplateResponse(request, "app/partials/article_detail.html", {"article": article, "mark_read_on_scroll": mark_read_on_scroll})


def _read_response(request: Request, article) -> HTMLResponse:
    """Return read button HTML + OOB article row update + sidebarRefresh trigger."""
    btn_html = templates.env.get_template("app/partials/read_button.html").render(
        article=article, request=request
    )
    row_html = templates.env.get_template("app/partials/article_row.html").render(
        article=article, request=request, oob=True
    )
    response = HTMLResponse(btn_html + row_html)
    response.headers["HX-Trigger"] = "sidebarRefresh"
    return response


def _star_response(request: Request, article) -> HTMLResponse:
    """Return star icon HTML + OOB article row update + sidebarRefresh trigger."""
    btn_html = templates.env.get_template("app/partials/star_icon.html").render(
        article=article, request=request
    )
    row_html = templates.env.get_template("app/partials/article_row.html").render(
        article=article, request=request, oob=True
    )
    response = HTMLResponse(btn_html + row_html)
    response.headers["HX-Trigger"] = "sidebarRefresh"
    return response


def _archive_response(request: Request, article) -> HTMLResponse:
    """Return archive button HTML + OOB article row update + sidebarRefresh trigger."""
    btn_html = templates.env.get_template("app/partials/archive_button.html").render(
        article=article, request=request
    )
    row_html = templates.env.get_template("app/partials/article_row.html").render(
        article=article, request=request, oob=True
    )
    response = HTMLResponse(btn_html + row_html)
    response.headers["HX-Trigger"] = "sidebarRefresh"
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
    return _read_response(request, article)


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
    return _read_response(request, article)


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
            (UserFeed.id != None)  # noqa: E711
            | (UserArticleState.is_starred == True)  # noqa: E712
            | (UserArticleState.is_archived == True),  # noqa: E712
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
            (UserFeed.id != None)  # noqa: E711
            | (UserArticleState.is_starred == True)  # noqa: E712
            | (UserArticleState.is_archived == True),  # noqa: E712
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
    content, error = await loop.run_in_executor(
        None, extract_readable, article.url, auth_user, auth_pass
    )

    if content:
        article.readable_content = content
        article.readable_status = "success"
        article.readable_error = None
    else:
        article.readable_status = "failed"
        article.readable_error = error
        article.readable_retries = (article.readable_retries or 0) + 1

    await db.commit()
    await db.refresh(article)

    return templates.TemplateResponse(request, "app/partials/article_content.html", {"article": article})


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
