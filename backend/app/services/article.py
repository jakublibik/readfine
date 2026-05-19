"""Article service: listing, detail, state toggles, unread count management."""
import logging
import re
from datetime import date, datetime, timezone

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SNIPPET_LEN = 200


async def _recalculate_unread_counts(
    user_id: int, article_ids: list[int], db: AsyncSession
) -> None:
    """Recalculate unread_count for all UserFeeds affected by the given article IDs.

    Uses a correlated subquery so the update is a single SQL statement regardless
    of how many feeds are affected.
    """
    if not article_ids:
        return
    unread_subq = (
        select(func.count())
        .select_from(Article)
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user_id),
        )
        .where(
            Article.feed_id == UserFeed.feed_id,
            (UserArticleState.is_read == False) | UserArticleState.is_read.is_(None),
        )
        .correlate(UserFeed)
        .scalar_subquery()
    )
    affected_feed_ids_subq = select(Article.feed_id.distinct()).where(
        Article.id.in_(article_ids)
    )
    await db.execute(
        update(UserFeed)
        .where(UserFeed.user_id == user_id, UserFeed.feed_id.in_(affected_feed_ids_subq))
        .values(unread_count=unread_subq)
    )


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


_FTS_VECTOR = (
    "to_tsvector('simple',"
    " coalesce(articles.title,'') || ' ' ||"
    " coalesce(articles.summary,'') || ' ' ||"
    " coalesce(articles.content,'') || ' ' ||"
    " coalesce(articles.readable_content,''))"
)


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
    q: str | None = None,
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
        if folder_id == 0:
            stmt = stmt.where(UserFeed.folder_id == None)
        else:
            stmt = stmt.where(UserFeed.folder_id == folder_id)

    if label_id is not None:
        stmt = stmt.join(
            ArticleLabel,
            (ArticleLabel.article_id == Article.id)
            & (ArticleLabel.user_id == user.id)
            & (ArticleLabel.label_id == label_id),
        )
    elif labeled_only:
        stmt = stmt.where(
            select(ArticleLabel.article_id)
            .where(
                (ArticleLabel.article_id == Article.id)
                & (ArticleLabel.user_id == user.id)
            )
            .exists()
        )

    if unread_only:
        stmt = stmt.where(
            (UserArticleState.is_read == False) | (UserArticleState.is_read == None)
        )

    if starred_only:
        stmt = stmt.where(UserArticleState.is_starred == True)

    if archived_only:
        stmt = stmt.where(UserArticleState.is_archived == True)

    if q:
        fts_vec = literal_column(_FTS_VECTOR)
        tsquery = func.websearch_to_tsquery('simple', q)
        try:
            # Round-trip to PostgreSQL to catch malformed inputs before the full query
            await db.execute(select(tsquery))
        except Exception:
            logger.warning("websearch_to_tsquery failed for %r, falling back to plainto_tsquery", q)
            tsquery = func.plainto_tsquery('simple', q)
        stmt = stmt.where(fts_vec.op('@@')(tsquery))
        stmt = stmt.order_by(
            func.ts_rank(fts_vec, tsquery).desc(),
            func.coalesce(Article.published_at, Article.fetched_at).desc(),
            Article.id.desc(),
        )
    else:
        coalesced = func.coalesce(Article.published_at, Article.fetched_at)
        order = coalesced.asc() if sort_order == "oldest" else coalesced.desc()
        stmt = stmt.order_by(order)
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()

    # Batch-fetch labels for all articles
    labels_by_article: dict[int, list[dict]] = {}
    if rows:
        from app.models.label import Label
        article_ids = [row[0].id for row in rows]
        labels_rows = (await db.execute(
            select(ArticleLabel.article_id, Label.id, Label.name, Label.color)
            .join(Label, Label.id == ArticleLabel.label_id)
            .where(ArticleLabel.article_id.in_(article_ids), ArticleLabel.user_id == user.id)
            .order_by(ArticleLabel.article_id, Label.position, Label.name)
        )).all()
        for aid, lid, lname, lcolor in labels_rows:
            labels_by_article.setdefault(aid, []).append({"id": lid, "name": lname, "color": lcolor})

    items = []
    for article, state, feed_title, custom_title in rows:
        items.append(ArticleListItem(
            id=article.id,
            feed_id=article.feed_id,
            feed_title=custom_title or feed_title,
            url=article.url,
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
            ai_score=state.ai_score if state else None,
            labels=labels_by_article.get(article.id, []),
        ))
    return items


async def _fetch_labels(article_id: int, user_id: int, db: AsyncSession) -> list[dict]:
    from app.models.label import Label
    rows = (await db.execute(
        select(ArticleLabel.label_id, Label.name, Label.color)
        .join(Label, Label.id == ArticleLabel.label_id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user_id)
        .order_by(Label.position, Label.name)
    )).all()
    return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]


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
            UserFeed.id.is_not(None)
            | UserArticleState.is_starred.is_(True)
            | UserArticleState.is_archived.is_(True),
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
        readable_error=article.readable_error,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read if state else False,
        is_starred=state.is_starred if state else False,
        is_archived=state.is_archived if state else False,
        read_at=state.read_at if state else None,
        share_token=state.share_token if state else None,
        ai_summary=state.ai_summary if state else None,
        ai_context=state.ai_context if state else None,
        labels=[
            {"id": r.id, "name": r.name, "color": r.color}
            for r in (await db.execute(
                select(Label.id, Label.name, Label.color)
                .join(ArticleLabel, ArticleLabel.label_id == Label.id)
                .where(
                    ArticleLabel.article_id == article_id,
                    ArticleLabel.user_id == user.id,
                )
                .order_by(Label.position, Label.name)
            )).all()
        ],
    )


async def mark_scope_read(
    user: User,
    db: AsyncSession,
    before: datetime,
    feed_id: int | None = None,
    folder_id: int | None = None,
    label_id: int | None = None,
    starred_only: bool = False,
    archived_only: bool = False,
    labeled_only: bool = False,
) -> None:
    """Bulk mark as read all articles in scope with fetched_at <= before.

    Starred/archived scopes only UPDATE (state row is guaranteed to exist).
    All other scopes upsert to handle articles with and without existing state rows.
    """
    from app.models.label import ArticleLabel

    now = datetime.now(timezone.utc)

    if starred_only or archived_only:
        # Articles in these views already have a state row by definition – plain UPDATE suffices.
        filter_cond = (
            UserArticleState.is_starred == True
            if starred_only
            else UserArticleState.is_archived == True
        )
        article_ids = (await db.execute(
            select(UserArticleState.article_id)
            .join(Article, Article.id == UserArticleState.article_id)
            .where(UserArticleState.user_id == user.id, Article.fetched_at <= before, filter_cond)
        )).scalars().all()
        if not article_ids:
            return
        await db.execute(
            update(UserArticleState)
            .where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id.in_(article_ids),
                UserArticleState.is_read == False,
            )
            .values(is_read=True, read_at=now)
        )
        await _recalculate_unread_counts(user.id, list(article_ids), db)
        await db.commit()
        return

    # All other scopes: user must be subscribed; upsert to handle missing state rows.
    subq = (
        select(Article.id)
        .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .where(Article.fetched_at <= before)
    )
    if feed_id is not None:
        subq = subq.where(Article.feed_id == feed_id)
    elif folder_id is not None:
        if folder_id == 0:
            subq = subq.where(UserFeed.folder_id.is_(None))
        else:
            subq = subq.where(UserFeed.folder_id == folder_id)
    elif label_id is not None:
        subq = subq.join(
            ArticleLabel,
            (ArticleLabel.article_id == Article.id)
            & (ArticleLabel.user_id == user.id)
            & (ArticleLabel.label_id == label_id),
        )
    elif labeled_only:
        subq = subq.where(
            select(ArticleLabel.article_id)
            .where((ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
            .exists()
        )
    # else: no extra filter → all subscribed articles

    article_ids = (await db.execute(subq)).scalars().all()
    if not article_ids:
        return

    stmt = pg_insert(UserArticleState).values([
        {
            "user_id": user.id,
            "article_id": aid,
            "is_read": True,
            "is_starred": False,
            "is_archived": False,
            "is_hidden": False,
            "read_at": now,
        }
        for aid in article_ids
    ]).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        set_={"is_read": True, "read_at": now},
        where=(UserArticleState.__table__.c.is_read == False),
    )
    await db.execute(stmt)
    await _recalculate_unread_counts(user.id, list(article_ids), db)
    await db.commit()


async def mark_articles_read_batch(user: User, article_ids: list[int], db: AsyncSession) -> None:
    """Mark specific articles as read in one upsert. Used by scroll-based batch mark-read."""
    if not article_ids:
        return
    now = datetime.now(timezone.utc)
    stmt = pg_insert(UserArticleState).values([
        {"user_id": user.id, "article_id": aid, "is_read": True,
         "is_starred": False, "is_archived": False, "is_hidden": False, "read_at": now}
        for aid in article_ids
    ]).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        set_={"is_read": True, "read_at": now},
        where=(UserArticleState.__table__.c.is_read.is_not(True)),
    )
    await db.execute(stmt)
    await _recalculate_unread_counts(user.id, article_ids, db)
    await db.commit()


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
            UserFeed.extract_readable,
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
            (UserFeed.id != None)
            | (UserArticleState.is_starred == True)
            | (UserArticleState.is_archived == True),
        )
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None

    article, state, feed_title, custom_title, extract_readable = row

    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)

    new_value = not getattr(state, field, False)
    setattr(state, field, new_value)

    if field == "is_read":
        state.read_at = datetime.now(timezone.utc) if new_value else None
        delta = -1 if new_value else 1
        await db.execute(
            update(UserFeed)
            .where(UserFeed.feed_id == article.feed_id, UserFeed.user_id == user.id)
            .values(unread_count=func.greatest(UserFeed.unread_count + delta, 0))
        )

    if field == "is_starred":
        if new_value:
            state.user_starred = True
        else:
            state.unstar_dwell_seconds = state.dwell_seconds

    if field == "is_starred" and new_value and extract_readable and article.readable_status == "skipped":
        article.readable_status = "pending"

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
        readable_error=article.readable_error,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read,
        is_starred=state.is_starred,
        is_archived=state.is_archived,
        read_at=state.read_at,
        labels=await _fetch_labels(article_id, user.id, db),
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
            (UserFeed.id != None)
            | (UserArticleState.is_starred == True)
            | (UserArticleState.is_archived == True),
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
        was_read = bool(state.is_read)
        state.is_read = payload.is_read
        state.read_at = datetime.now(timezone.utc) if payload.is_read else None
        if was_read != payload.is_read:
            delta = -1 if payload.is_read else 1
            await db.execute(
                update(UserFeed)
                .where(UserFeed.feed_id == article.feed_id, UserFeed.user_id == user.id)
                .values(unread_count=func.greatest(UserFeed.unread_count + delta, 0))
            )

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
        readable_error=article.readable_error,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read,
        is_starred=state.is_starred,
        is_archived=state.is_archived,
        read_at=state.read_at,
        labels=await _fetch_labels(article_id, user.id, db),
    )
