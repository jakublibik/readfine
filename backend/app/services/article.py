"""Article service: listing, detail, state toggles, unread count management."""
import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SNIPPET_LEN = 200


def _make_snippet(summary: str | None, content: str | None) -> str | None:
    """Return a plain-text snippet: summary if usable, otherwise content prefix."""
    for source in (summary, content):
        if not source:
            continue
        text = _HTML_TAG_RE.sub(" ", source)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if len(text) > 20:
            return text[:_SNIPPET_LEN].rsplit(" ", 1)[0] if len(text) > _SNIPPET_LEN else text
    return None


def _format_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).date()
    return dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%b %d, %H:%M")


async def list_articles(
    user: User,
    db: AsyncSession,
    feed_id: int | None = None,
    folder_id: int | None = None,
    label_id: int | None = None,
    unread_only: bool = False,
    starred_only: bool = False,
    archived_only: bool = False,
    labeled_only: bool = False,
    sort_order: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> list[ArticleListItem]:
    """Return articles visible to the user with their read/star state."""
    # Starred/archived views: UserFeed is optional (articles remain visible after unsubscribe)
    is_state_view = starred_only or archived_only
    if is_state_view:
        stmt = (
            select(
                Article,
                UserArticleState,
                Feed.title.label("feed_title"),
                UserFeed.custom_title.label("custom_title"),
            )
            .join(
                UserArticleState,
                (UserArticleState.article_id == Article.id)
                & (UserArticleState.user_id == user.id),
            )
            .outerjoin(Feed, Feed.id == Article.feed_id)
            .outerjoin(
                UserFeed,
                (UserFeed.feed_id == Article.feed_id)
                & (UserFeed.user_id == user.id),
            )
        )
    else:
        # Normal view: user must be subscribed to the feed
        stmt = (
            select(
                Article,
                UserArticleState,
                Feed.title.label("feed_title"),
                UserFeed.custom_title.label("custom_title"),
            )
            .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
            .join(Feed, Feed.id == Article.feed_id)
            .outerjoin(
                UserArticleState,
                (UserArticleState.article_id == Article.id)
                & (UserArticleState.user_id == user.id),
            )
        )

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)

    if folder_id is not None:
        stmt = stmt.where(UserFeed.folder_id == folder_id)

    if label_id is not None:
        stmt = stmt.join(
            ArticleLabel,
            (ArticleLabel.article_id == Article.id)
            & (ArticleLabel.user_id == user.id)
            & (ArticleLabel.label_id == label_id),
        )
    elif labeled_only:
        stmt = stmt.join(
            ArticleLabel,
            (ArticleLabel.article_id == Article.id)
            & (ArticleLabel.user_id == user.id),
        ).distinct()

    if unread_only:
        stmt = stmt.where(
            (UserArticleState.is_read == False) | (UserArticleState.is_read == None)  # noqa: E711
        )

    if starred_only:
        stmt = stmt.where(UserArticleState.is_starred == True)  # noqa: E712

    if archived_only:
        stmt = stmt.where(UserArticleState.is_archived == True)  # noqa: E712

    order = Article.published_at.asc().nulls_last() if sort_order == "oldest" else Article.published_at.desc().nulls_last()
    stmt = stmt.order_by(order).limit(limit).offset(offset)

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
            snippet=_make_snippet(article.summary, article.content),
            published_at=article.published_at,
            formatted_date=_format_date(article.published_at or article.created_at),
            estimated_read_min=article.estimated_read_min,
            image_url=article.image_url,
            is_read=state.is_read if state else False,
            is_starred=state.is_starred if state else False,
            is_archived=state.is_archived if state else False,
        ))
    return items


async def get_article(user: User, article_id: int, db: AsyncSession) -> ArticleResponse | None:
    """Return article detail with user state. Returns None if not accessible.

    Access is granted if the user subscribes to the feed, OR has a starred/archived
    state for the article (remains accessible after unsubscribing).
    """
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user.id),
        )
        .where(
            Article.id == article_id,
            (UserFeed.id != None)  # noqa: E711 — subscribed
            | (UserArticleState.is_starred == True)  # noqa: E712 — starred orphan
            | (UserArticleState.is_archived == True),  # noqa: E712 — archived orphan
        )
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
        readable_content=article.readable_content,
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


async def toggle_article_state(
    user: User,
    article_id: int,
    field: str,
    db: AsyncSession,
) -> ArticleResponse | None:
    """Toggle a single boolean field (is_read/is_starred/is_archived) in one DB round-trip."""
    assert field in {"is_read", "is_starred", "is_archived"}
    current_value = {field: True}  # payload with inverted value – determined after load
    # Load current state first via the same single-query path as update_article_state
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user.id),
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
        return None

    article, state, feed_title, custom_title = row

    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)

    new_value = not getattr(state, field, False)
    setattr(state, field, new_value)

    if field == "is_read":
        state.read_at = datetime.now(timezone.utc) if new_value else None

    await db.commit()
    await db.refresh(state)

    return ArticleResponse(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        content=article.content,
        content_source=article.content_source,
        readable_content=article.readable_content,
        readable_status=article.readable_status,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read,
        is_starred=state.is_starred,
        is_archived=state.is_archived,
        read_at=state.read_at,
    )


async def update_article_state(
    user: User,
    article_id: int,
    payload: ArticleStateUpdate,
    db: AsyncSession,
) -> ArticleResponse | None:
    """Toggle is_read / is_starred / is_archived. Creates UserArticleState if needed.

    Single round-trip: loads Article + Feed title + UserFeed + state in one query,
    applies changes, commits, and builds the response from already-loaded data.
    """
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user.id),
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
        return None

    article, state, feed_title, custom_title = row

    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)

    if payload.is_read is not None:
        state.is_read = payload.is_read
        state.read_at = datetime.now(timezone.utc) if payload.is_read else None

    if payload.is_starred is not None:
        state.is_starred = payload.is_starred

    if payload.is_archived is not None:
        state.is_archived = payload.is_archived

    await db.commit()
    await db.refresh(state)

    return ArticleResponse(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        content=article.content,
        content_source=article.content_source,
        readable_content=article.readable_content,
        readable_status=article.readable_status,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read,
        is_starred=state.is_starred,
        is_archived=state.is_archived,
        read_at=state.read_at,
    )
