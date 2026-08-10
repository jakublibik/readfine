from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.article import (
    ArticleListItem,
    ArticleResponse,
    ArticleStateUpdate,
    SaveUrlRequest,
)
from app.services.article import get_article, list_articles, update_article_state
from app.services.saved_article_service import save_article_by_url

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("", response_model=list[ArticleListItem])
async def get_articles(
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    saved_only: bool = Query(False),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_articles(
        user=user,
        db=db,
        feed_id=feed_id,
        folder_id=folder_id,
        unread_only=unread_only,
        starred_only=starred_only,
        archived_only=archived_only,
        saved_only=saved_only,
        q=q or None,
        limit=limit,
        offset=offset,
    )


@router.post("/save-url", response_model=ArticleResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(app_settings_config.rate_limit_save_url)
async def save_url(
    request: Request,
    response: Response,
    payload: SaveUrlRequest,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    """Save an article by its address, the way the Saved box in the web app does.

    Answers **201** for a newly imported article and **200** for one already in the
    database, which is the whole difference between the two: a link you already have
    is attached to your Saved list rather than duplicated, and that is a success, not
    a conflict. **400** is a URL that cannot be fetched at all (bad scheme, no host,
    unresolvable, or a private/loopback address).

    The article comes back immediately, with ``readable_status`` at ``pending`` and
    the address standing in for a title: the page is fetched in the background, so
    poll ``GET /articles/{id}`` if the client needs the text or the real headline.
    Everything that can only fail during that fetch (a 404, a timeout, a paywall)
    leaves the article saved with ``readable_status`` at ``failed`` and the reason in
    ``readable_error``.
    """
    try:
        article, already_known = await save_article_by_url(payload.url, user, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if already_known:
        response.status_code = status.HTTP_200_OK
    # Re-read rather than building a response from the ORM row: ArticleResponse
    # carries per-user fields (state, labels, feed title) the Article does not have.
    saved = await get_article(user, article.id, db)
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Article was saved but could not be read back",
        )
    return saved


@router.get("/{article_id}", response_model=ArticleResponse)
async def get_article_detail(
    article_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    article = await get_article(user, article_id, db)
    if not article:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
    return article


@router.patch("/{article_id}", response_model=ArticleResponse)
async def patch_article_state(
    article_id: int,
    payload: ArticleStateUpdate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    article = await update_article_state(user, article_id, payload, db)
    if not article:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found")
    return article
