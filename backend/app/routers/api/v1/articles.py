from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate
from app.services.article import get_article, list_articles, update_article_state

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("", response_model=list[ArticleListItem])
async def get_articles(
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
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
        q=q or None,
        limit=limit,
        offset=offset,
    )


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
