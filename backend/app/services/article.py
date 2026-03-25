"""Article service: listing, detail, state toggles, unread count management."""
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate


async def list_articles(
    user: User,
    db: AsyncSession,
    feed_id: int | None = None,
    folder_id: int | None = None,
    unread_only: bool = False,
    starred_only: bool = False,
    archived_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[ArticleListItem]:
    """Return articles visible to the user with their read/star state."""
    # Base: articles in feeds the user subscribes to
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        )
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .join(Feed, Feed.id == Article.feed_id)
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user.id),
        )
        .where(UserFeed.user_id == user.id)
    )

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)

    if folder_id is not None:
        stmt = stmt.where(UserFeed.folder_id == folder_id)

    if unread_only:
        stmt = stmt.where(
            (UserArticleState.is_read == False) | (UserArticleState.is_read == None)  # noqa: E711
        )

    if starred_only:
        stmt = stmt.where(UserArticleState.is_starred == True)  # noqa: E712

    if archived_only:
        stmt = stmt.where(UserArticleState.is_archived == True)  # noqa: E712

    stmt = stmt.order_by(Article.published_at.desc().nulls_last()).limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()

    items = []
    for article, state, feed_title, custom_title in rows:
        items.append(ArticleListItem(
            id=article.id,
            feed_id=article.feed_id,
            feed_title=custom_title or feed_title,
            title=article.title,
            author=article.author,
            summary=article.summary,
            published_at=article.published_at,
            estimated_read_min=article.estimated_read_min,
            image_url=article.image_url,
            is_read=state.is_read if state else False,
            is_starred=state.is_starred if state else False,
            is_archived=state.is_archived if state else False,
        ))
    return items


async def get_article(user: User, article_id: int, db: AsyncSession) -> ArticleResponse | None:
    """Return article detail with user state. Returns None if not accessible."""
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        )
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .join(Feed, Feed.id == Article.feed_id)
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user.id),
        )
        .where(Article.id == article_id, UserFeed.user_id == user.id)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None

    article, state, feed_title, custom_title = row
    return ArticleResponse(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        content=article.content,
        content_source=article.content_source,
        readable_status=article.readable_status,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read if state else False,
        is_starred=state.is_starred if state else False,
        is_archived=state.is_archived if state else False,
        read_at=state.read_at if state else None,
    )


async def update_article_state(
    user: User,
    article_id: int,
    payload: ArticleStateUpdate,
    db: AsyncSession,
) -> ArticleResponse | None:
    """Toggle is_read / is_starred / is_archived. Creates UserArticleState if needed."""
    # Verify the article is accessible to this user
    access = await db.execute(
        select(UserFeed.id)
        .join(Article, Article.feed_id == UserFeed.feed_id)
        .where(Article.id == article_id, UserFeed.user_id == user.id)
    )
    if not access.scalar_one_or_none():
        return None

    state = await db.get(UserArticleState, (user.id, article_id))
    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)

    if payload.is_read is not None:
        prev_read = state.is_read
        state.is_read = payload.is_read
        state.read_at = datetime.now(timezone.utc) if payload.is_read else None
        # Update denormalized unread_count on affected UserFeed
        if prev_read != payload.is_read:
            await _adjust_unread_count(user.id, article_id, delta=-1 if payload.is_read else 1, db=db)

    if payload.is_starred is not None:
        state.is_starred = payload.is_starred

    if payload.is_archived is not None:
        state.is_archived = payload.is_archived

    await db.commit()
    return await get_article(user, article_id, db)


async def _adjust_unread_count(user_id: int, article_id: int, delta: int, db: AsyncSession) -> None:
    """Increment or decrement unread_count on the UserFeed that owns this article."""
    article = await db.get(Article, article_id)
    if not article or not article.feed_id:
        return
    await db.execute(
        update(UserFeed)
        .where(UserFeed.user_id == user_id, UserFeed.feed_id == article.feed_id)
        .values(unread_count=UserFeed.unread_count + delta)
    )
