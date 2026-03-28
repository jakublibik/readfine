"""Web routes for the main application UI."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.auth.dependencies import get_current_user
from app.database import get_db
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
    return templates.TemplateResponse(request, "app/partials/sidebar.html", {
        "user": user,
        "user_feeds": user_feeds,
        "user_labels": user_labels,
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
    return templates.TemplateResponse(request, "app/partials/star_button.html", {"article": article})


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
    return templates.TemplateResponse(request, "app/partials/archive_button.html", {"article": article})
