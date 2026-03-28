"""Web routes for the main application UI."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func, select

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
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
):
    return templates.TemplateResponse(request, "app/main.html", {"user": user})


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
    })


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
        limit=50,
        offset=offset,
    )
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    return templates.TemplateResponse(request, "app/partials/article_list.html", {
        "articles": articles,
        "feed_id": feed_id,
        "folder_id": folder_id,
        "unread_only": unread_only,
        "starred_only": starred_only,
        "archived_only": archived_only,
        "mark_read_on_scroll": mark_read_on_scroll,
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
    """Return star button HTML + OOB article row update + sidebarRefresh trigger."""
    btn_html = templates.env.get_template("app/partials/star_button.html").render(
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
