"""Web routes for the main application UI."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.article import get_article, list_articles, update_article_state
from app.services.feed import list_user_feeds
from app.schemas.article import ArticleStateUpdate

router = APIRouter(tags=["web-app"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/app", response_class=HTMLResponse)
async def main_app(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        "app/main.html",
        {"request": request, "user": user},
    )


# ── HTMX fragments ────────────────────────────────────────────────────────────

@router.get("/htmx/sidebar", response_class=HTMLResponse)
async def htmx_sidebar(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds = await list_user_feeds(user, db)
    return templates.TemplateResponse(
        "app/partials/sidebar.html",
        {"request": request, "user": user, "user_feeds": user_feeds},
    )


@router.get("/htmx/articles", response_class=HTMLResponse)
async def htmx_article_list(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
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
        unread_only=unread_only,
        starred_only=starred_only,
        archived_only=archived_only,
        limit=50,
        offset=offset,
    )
    return templates.TemplateResponse(
        "app/partials/article_list.html",
        {
            "request": request,
            "articles": articles,
            "feed_id": feed_id,
            "folder_id": folder_id,
            "unread_only": unread_only,
            "starred_only": starred_only,
            "archived_only": archived_only,
        },
    )


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
    return templates.TemplateResponse(
        "app/partials/article_detail.html",
        {"request": request, "article": article},
    )


@router.post("/htmx/articles/{article_id}/read", response_class=HTMLResponse)
async def htmx_toggle_read(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current = await get_article(user, article_id, db)
    if not current:
        return HTMLResponse("", status_code=404)
    article = await update_article_state(
        user, article_id, ArticleStateUpdate(is_read=not current.is_read), db
    )
    return templates.TemplateResponse(
        "app/partials/read_button.html",
        {"request": request, "article": article},
    )


@router.post("/htmx/articles/{article_id}/star", response_class=HTMLResponse)
async def htmx_toggle_star(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current = await get_article(user, article_id, db)
    if not current:
        return HTMLResponse("", status_code=404)
    article = await update_article_state(
        user, article_id, ArticleStateUpdate(is_starred=not current.is_starred), db
    )
    return templates.TemplateResponse(
        "app/partials/star_button.html",
        {"request": request, "article": article},
    )


@router.post("/htmx/articles/{article_id}/archive", response_class=HTMLResponse)
async def htmx_toggle_archive(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    current = await get_article(user, article_id, db)
    if not current:
        return HTMLResponse("", status_code=404)
    article = await update_article_state(
        user, article_id, ArticleStateUpdate(is_archived=not current.is_archived), db
    )
    return templates.TemplateResponse(
        "app/partials/archive_button.html",
        {"request": request, "article": article},
    )
