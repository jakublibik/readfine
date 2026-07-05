"""Article service: listing, detail, state toggles, unread count management."""
import json
import logging
import re
from datetime import date, datetime, timezone

from sqlalchemy import func, literal, literal_column, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate
from app.utils.datetime_format import current_viewer_tz, format_local

logger = logging.getLogger(__name__)


def _parse_label_filter(label_filter: str | None) -> tuple[bool, list[int]]:
    """Parse the label-filter JSON (same shape as the scope selector).

    Returns (any_label, label_ids). "any" means "has at least one label" and
    takes precedence over specific ids. Empty/invalid means no label filtering.
    """
    if not label_filter:
        return False, []
    try:
        items = json.loads(label_filter)
    except (json.JSONDecodeError, TypeError):
        return False, []
    if "any" in items:
        return True, []
    ids: list[int] = []
    for item in items:
        if isinstance(item, str) and item.startswith("label:"):
            try:
                ids.append(int(item[6:]))
            except ValueError:
                pass
    return False, ids


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SNIPPET_LEN = 200


async def _recalc_unread_for_feeds(user_id: int, feed_ids, db: AsyncSession) -> None:
    """Recalculate unread_count for the given feeds in one statement.

    `feed_ids` may be a list of ints or a single-column subquery (SELECT feed_id …).
    Uses a correlated subquery so the update is a single SQL statement regardless
    of how many feeds are affected.
    """
    if isinstance(feed_ids, (list, tuple, set)) and not feed_ids:
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
            Article.trimmed_at.is_(None),
            (UserArticleState.is_read == False) | UserArticleState.is_read.is_(None),
        )
        .correlate(UserFeed)
        .scalar_subquery()
    )
    await db.execute(
        update(UserFeed)
        .where(UserFeed.user_id == user_id, UserFeed.feed_id.in_(feed_ids))
        .values(unread_count=unread_subq)
    )


async def _recalculate_unread_counts(
    user_id: int, article_ids: list[int], db: AsyncSession
) -> None:
    """Recalculate unread_count for feeds touched by the given article IDs.

    For small, already-materialized batches (e.g. scroll-based mark-read).
    """
    if not article_ids:
        return
    affected_feed_ids = select(Article.feed_id.distinct()).where(Article.id.in_(article_ids))
    await _recalc_unread_for_feeds(user_id, affected_feed_ids, db)


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
    # Uses the per-request viewer timezone (set in the auth dependency).
    return format_local(dt, current_viewer_tz.get(), "short")


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
    scope_include: str | None = None,
    label_id: int | None = None,
    label_filter: str | None = None,
    unread_only: bool = False,
    read_status: str | None = None,
    starred_only: bool = False,
    archived_only: bool = False,
    labeled_only: bool = False,
    q: str | None = None,
    sort_order: str = "newest",
    limit: int = 50,
    offset: int = 0,
    cursor_ts: datetime | None = None,
    cursor_id: int | None = None,
) -> list[ArticleListItem]:
    """Return articles visible to the user with their read/star state."""
    # State/label views are anchored on user-owned state (star, archive, label)
    # that outlives the feed subscription: the feed may be unsubscribed or deleted
    # (Article.feed_id NULL), so the UserFeed/Feed joins must be optional. The
    # user-scoping — and thus tenant isolation — comes from those anchors
    # (UserArticleState.user_id / ArticleLabel.user_id below), never from the feed
    # join, so dropping the subscription requirement here cannot leak other users'
    # articles. Feed-browsing views instead require an active subscription.
    feed_optional = starred_only or archived_only or label_id is not None or labeled_only
    stmt = select(
        Article,
        UserArticleState,
        Feed.title.label("feed_title"),
        UserFeed.custom_title.label("custom_title"),
    )
    uas_join = (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id)
    uf_join = (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id)
    if feed_optional:
        # UserArticleState stays an outer join: a labeled article may have no state
        # row yet, and for starred/archived the `is_*` filters below make an outer
        # join equivalent to an inner one.
        stmt = (
            stmt
            .outerjoin(UserArticleState, uas_join)
            .outerjoin(Feed, Feed.id == Article.feed_id)
            .outerjoin(UserFeed, uf_join)
        )
    else:
        # Normal view: user must be subscribed to the feed
        stmt = (
            stmt
            .join(UserFeed, uf_join)
            .join(Feed, Feed.id == Article.feed_id)
            .outerjoin(UserArticleState, uas_join)
        )

    # Retention-trimmed articles are body-stripped stubs kept only for the interest
    # profile — never shown in the UI.
    stmt = stmt.where(Article.trimmed_at.is_(None))

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)

    if folder_id is not None:
        if folder_id == 0:
            stmt = stmt.where(UserFeed.folder_id == None)
        else:
            stmt = stmt.where(UserFeed.folder_id == folder_id)

    # Multi-select scope (same JSON format as filters/catchup: ["feed:1","folder:2"]).
    # Empty lists mean "all feeds" — no restriction. Feed ownership is already
    # enforced by the UserFeed join above, so unknown ids simply match nothing.
    if scope_include:
        from app.services.catchup_service import _parse_scope  # noqa: PLC0415
        scope_feed_ids, scope_folder_ids = _parse_scope(scope_include)
        if scope_feed_ids or scope_folder_ids:
            clauses = []
            if scope_feed_ids:
                clauses.append(Article.feed_id.in_(scope_feed_ids))
            for fid in scope_folder_ids:
                if fid == 0:
                    clauses.append(UserFeed.folder_id.is_(None))
                else:
                    clauses.append(UserFeed.folder_id == fid)
            stmt = stmt.where(or_(*clauses))

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

    # Search label filter (multi-select): "any" = has at least one label,
    # otherwise articles carrying at least one of the selected labels.
    if label_filter:
        any_label, lf_ids = _parse_label_filter(label_filter)
        cond = (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id)
        if any_label:
            stmt = stmt.where(select(ArticleLabel.article_id).where(cond).exists())
        elif lf_ids:
            stmt = stmt.where(
                select(ArticleLabel.article_id)
                .where(cond & ArticleLabel.label_id.in_(lf_ids))
                .exists()
            )

    if unread_only:
        stmt = stmt.where(
            (UserArticleState.is_read == False) | (UserArticleState.is_read == None)
        )

    # Search status filter (tri-state): "unread" / "read" / anything else = all.
    if read_status == "unread":
        stmt = stmt.where(
            (UserArticleState.is_read == False) | (UserArticleState.is_read == None)
        )
    elif read_status == "read":
        stmt = stmt.where(UserArticleState.is_read == True)

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
        coalesced = func.coalesce(Article.published_at, Article.fetched_at)
        # Search honours its own sort selector; default is relevance (ts_rank).
        if sort_order == "newest":
            stmt = stmt.order_by(coalesced.desc(), Article.id.desc())
        elif sort_order == "oldest":
            stmt = stmt.order_by(coalesced.asc(), Article.id.asc())
        else:
            stmt = stmt.order_by(
                func.ts_rank(fts_vec, tsquery).desc(),
                coalesced.desc(),
                Article.id.desc(),
            )
    else:
        coalesced = func.coalesce(Article.published_at, Article.fetched_at)
        if sort_order == "oldest":
            # id tiebreaker keeps the total order deterministic (matches
            # ix_articles_sort_ts) and is required for stable keyset pagination
            stmt = stmt.order_by(coalesced.asc(), Article.id.asc())
            if cursor_ts is not None and cursor_id is not None:
                stmt = stmt.where(
                    tuple_(coalesced, Article.id) > tuple_(cursor_ts, cursor_id)
                )
        else:
            stmt = stmt.order_by(coalesced.desc(), Article.id.desc())
            if cursor_ts is not None and cursor_id is not None:
                stmt = stmt.where(
                    tuple_(coalesced, Article.id) < tuple_(cursor_ts, cursor_id)
                )
    stmt = stmt.limit(limit)
    # Keyset pagination (cursor) supersedes offset; offset stays for the FTS
    # branch and the REST API, which keep offset/limit semantics.
    if cursor_ts is None:
        stmt = stmt.offset(offset)

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
            .order_by(ArticleLabel.article_id, Label.position, func.lower(Label.name))
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
            sort_ts=article.published_at or article.fetched_at,
        ))
    return items


async def _fetch_labels(article_id: int, user_id: int, db: AsyncSession) -> list[dict]:
    from app.models.label import Label
    rows = (await db.execute(
        select(ArticleLabel.label_id, Label.name, Label.color)
        .join(Label, Label.id == ArticleLabel.label_id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user_id)
        .order_by(Label.position, func.lower(Label.name))
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
        readable_active=(article.readable_status == "pending" and not article.readable_retries),
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
                .order_by(Label.position, func.lower(Label.name))
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
        # Drive the UPDATE from a subquery so we never materialize IDs into Python
        # (which previously blew past asyncpg's 32767-parameter limit on large feeds).
        filter_cond = (
            UserArticleState.is_starred == True
            if starred_only
            else UserArticleState.is_archived == True
        )
        scope_articles = (
            select(Article.id)
            .join(
                UserArticleState,
                (UserArticleState.article_id == Article.id)
                & (UserArticleState.user_id == user.id),
            )
            .where(Article.fetched_at <= before, filter_cond)
        )
        await db.execute(
            update(UserArticleState)
            .where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id.in_(scope_articles),
                UserArticleState.is_read == False,
            )
            .values(is_read=True, read_at=now)
        )
        affected_feeds = select(Article.feed_id.distinct()).where(Article.id.in_(scope_articles))
        await _recalc_unread_for_feeds(user.id, affected_feeds, db)
        await db.commit()
        return

    # All other scopes: user must be subscribed; upsert to handle missing state rows.
    # A single INSERT … SELECT … ON CONFLICT keeps everything server-side — no IDs
    # round-trip through Python, so feed size is irrelevant.
    def scoped_select(*cols):
        q = (
            select(*cols)
            .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
            .where(Article.fetched_at <= before)
        )
        if feed_id is not None:
            q = q.where(Article.feed_id == feed_id)
        elif folder_id is not None:
            q = q.where(UserFeed.folder_id.is_(None) if folder_id == 0 else UserFeed.folder_id == folder_id)
        elif label_id is not None:
            q = q.join(
                ArticleLabel,
                (ArticleLabel.article_id == Article.id)
                & (ArticleLabel.user_id == user.id)
                & (ArticleLabel.label_id == label_id),
            )
        elif labeled_only:
            q = q.where(
                select(ArticleLabel.article_id)
                .where((ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
                .exists()
            )
        # else: no extra filter → all subscribed articles
        return q

    insert_select = scoped_select(
        literal(user.id), Article.id,
        literal(True), literal(False), literal(False), literal(now),
    )
    stmt = pg_insert(UserArticleState).from_select(
        ["user_id", "article_id", "is_read", "is_starred", "is_archived", "read_at"],
        insert_select,
    ).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        set_={"is_read": True, "read_at": now},
        where=(UserArticleState.__table__.c.is_read == False),
    )
    await db.execute(stmt)
    await _recalc_unread_for_feeds(user.id, scoped_select(Article.feed_id).distinct(), db)
    await db.commit()


async def filter_accessible_article_ids(
    user_id: int, article_ids: list[int], db: AsyncSession
) -> list[int]:
    """Return the subset of article_ids the user may act on.

    Access mirrors get_article: the article belongs to a subscribed feed, or the
    user has starred/archived it. Guards client-driven state writes (batch
    mark-read, dwell) against stale/crafted ids that fall outside the user's
    reading context and would otherwise skew their stats / AI preference.
    """
    if not article_ids:
        return []
    rows = await db.execute(
        select(Article.id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user_id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id),
        )
        .where(
            Article.id.in_(article_ids),
            UserFeed.id.is_not(None)
            | UserArticleState.is_starred.is_(True)
            | UserArticleState.is_archived.is_(True),
        )
    )
    return [r[0] for r in rows.all()]


async def mark_articles_read_batch(user: User, article_ids: list[int], db: AsyncSession) -> None:
    """Mark specific articles as read in one upsert. Used by scroll-based batch mark-read."""
    if not article_ids:
        return
    article_ids = await filter_accessible_article_ids(user.id, article_ids, db)
    if not article_ids:
        return
    now = datetime.now(timezone.utc)
    stmt = pg_insert(UserArticleState).values([
        {"user_id": user.id, "article_id": aid, "is_read": True,
         "is_starred": False, "is_archived": False, "read_at": now}
        for aid in article_ids
    ]).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        set_={"is_read": True, "read_at": now},
        where=(UserArticleState.__table__.c.is_read.is_not(True)),
    )
    await db.execute(stmt)
    await _recalculate_unread_counts(user.id, article_ids, db)
    await db.commit()


def _apply_star_side_effects(state, article, *, starred: bool, extract_readable: bool) -> None:
    """Star/unstar side effects shared by toggle_article_state and update_article_state
    so web and API star behave identically.

    Starring marks user intent (user_starred — a positive AI-preference signal),
    retention protection (ever_starred), starred_at, and triggers readable
    extraction. Unstarring snapshots dwell and treats an unstar within 60s as an
    accidental star (clears ever_starred)."""
    if starred:
        state.user_starred = True
        state.ever_starred = True
        state.starred_at = datetime.now(timezone.utc)
        if extract_readable and article.readable_status == "skipped":
            article.readable_status = "pending"
    else:
        state.unstar_dwell_seconds = state.dwell_seconds
        if state.starred_at and (datetime.now(timezone.utc) - state.starred_at).total_seconds() < 60:
            state.ever_starred = False


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
        _apply_star_side_effects(state, article, starred=new_value, extract_readable=bool(extract_readable))

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
        was_starred = bool(state.is_starred)
        state.is_starred = payload.is_starred
        if payload.is_starred != was_starred:
            _apply_star_side_effects(
                state, article, starred=payload.is_starred, extract_readable=bool(extract_readable)
            )

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
