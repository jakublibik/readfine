"""Public (unauthenticated) view of a shared article."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.article import Article, UserArticleState
from app.models.feed import Feed
from app.templating import templates

router = APIRouter(tags=["web-app"])


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
