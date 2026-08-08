"""Article service: listing, detail, state toggles, unread count management."""
import logging
from datetime import date, datetime, timezone

from sqlalchemy import func, literal, literal_column, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate
from app.services.scope_tokens import parse_label_tokens, parse_scope_tokens
from app.utils.datetime_format import current_viewer_tz, format_local
from app.utils.text import strip_html

logger = logging.getLogger(__name__)


# ── article access (tenant isolation — single source of truth) ────────────────

def add_article_access_joins(stmt, user_id: int):
    """Outer-join UserFeed and UserArticleState for the access predicate.

    Both joins are user-scoped in their ON clause, so ``article_access_predicate``
    can decide visibility purely from whether a row matched. Pair the two: every
    article read/write path uses this + ``article_access_predicate`` so the access
    rule lives in exactly one place.
    """
    return (
        stmt
        .outerjoin(
            UserFeed,
            (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user_id),
        )
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id),
        )
    )


def permanently_kept_predicate():
    """Row-level clause for "this UserArticleState keeps its article for good".

    Starred, archived or saved by URL: the three ways a reader says an article is
    not disposable. One definition, because the same question is asked from four
    places that must not drift apart — access (below), retention
    (``purge_service._fully_protected_exists``), and the two points in
    ``services.feed`` where an article survives its feed being deleted.

    Safe to negate: every operand is NOT NULL or an ``IS NOT NULL`` test, so
    ``~permanently_kept_predicate()`` has no three-valued surprises.
    """
    return (
        UserArticleState.is_starred.is_(True)
        | UserArticleState.is_archived.is_(True)
        | UserArticleState.saved_at.is_not(None)
    )


def permanently_kept_exists():
    """Correlated EXISTS: *some* user keeps the current Article for good.

    Any-user semantics, so one reader starring or saving an article pins the row
    for the whole instance. Correlates on ``Article.id``, so the enclosing query
    must select from (or update/delete) ``articles``.
    """
    return (
        select(UserArticleState.article_id)
        .where(UserArticleState.article_id == Article.id, permanently_kept_predicate())
        .correlate(Article)
        .exists()
    )


def article_access_predicate():
    """WHERE clause for "this user may act on this article".

    True when the user is subscribed to the article's feed, OR keeps the article
    for good — starred/archived (access survives unsubscribe / feed deletion) or
    saved by URL (such an article usually has no feed at all). Requires the query
    to have added ``add_article_access_joins(user_id)`` first — the user scoping
    lives in those joins' ON clauses, so this predicate only checks whether a row
    matched.
    """
    return UserFeed.id.is_not(None) | permanently_kept_predicate()


_SNIPPET_LEN = 200


def _make_snippet(summary: str | None, content: str | None) -> str | None:
    """Return a plain-text snippet: summary if usable, otherwise content prefix."""
    for source in (summary, content):
        if not source:
            continue
        text = strip_html(source)
        if len(text) > 20:
            return text[:_SNIPPET_LEN].rsplit(" ", 1)[0] if len(text) > _SNIPPET_LEN else text
    return None


def body_permanently_empty(article: Article, extract_readable: bool | None) -> bool:
    """True when the article will never have a body to show, so the reader can be
    sent straight to the source.

    Deliberately narrower than the "nothing to render right now" branch in
    article_content.html, which also covers articles still being extracted. The two
    look alike but answer different questions and must not be merged.
    """
    if article.readable_status == "success" and article.readable_content:
        return False
    if article.content:
        return False
    if article.readable_status == "pending":
        # Extraction in flight, or waiting on retry backoff.
        return False
    if article.readable_status == "skipped" and extract_readable:
        # Opening the detail kicks off extraction (see htmx_article_detail).
        return False
    return True


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
    saved_only: bool = False,
    labeled_only: bool = False,
    q: str | None = None,
    sort_order: str = "newest",
    limit: int = 50,
    offset: int = 0,
    cursor_ts: datetime | None = None,
    cursor_id: int | None = None,
) -> list[ArticleListItem]:
    """Return articles visible to the user with their read/star state."""
    # State/label views are anchored on user-owned state (star, archive, save, label)
    # that outlives the feed subscription: the feed may be unsubscribed or deleted
    # (Article.feed_id NULL), and a saved-by-URL article never had one, so the
    # UserFeed/Feed joins must be optional. The user-scoping — and thus tenant
    # isolation — comes from those anchors (UserArticleState.user_id /
    # ArticleLabel.user_id below), never from the feed join, so dropping the
    # subscription requirement here cannot leak other users' articles. Feed-browsing
    # views instead require an active subscription.
    #
    # Full-text search joins that same club, but for a different reason and with a
    # different safety net. It has no state anchor of its own — nothing like
    # `is_starred == True` to scope it — so it must carry ``article_access_predicate``
    # explicitly below. Without the optional join a saved-by-URL article could never
    # be found by search at all (it has no feed, so the inner join drops it), which
    # defeats the point of saving it; the same was quietly true of starred/archived
    # articles left orphaned by an unsubscribe.
    searching = bool(q and q.strip())
    feed_optional = (
        starred_only or archived_only or saved_only
        or label_id is not None or labeled_only or searching
    )
    stmt = select(
        Article,
        UserArticleState,
        Feed.title.label("feed_title"),
        UserFeed.custom_title.label("custom_title"),
        UserFeed.extract_readable.label("extract_readable"),
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

    if searching:
        # The one branch above that has no anchor of its own. This is what keeps
        # search user-scoped, so it must not be dropped or narrowed: the joins are
        # outer here, and without it search would match every article in the table.
        stmt = stmt.where(article_access_predicate())

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
        scope_feed_ids, scope_folder_ids = parse_scope_tokens(scope_include)
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
        any_label, lf_ids = parse_label_tokens(label_filter)
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

    if saved_only:
        stmt = stmt.where(UserArticleState.saved_at.is_not(None))

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
    for article, state, feed_title, custom_title, extract_readable in rows:
        items.append(ArticleListItem(
            id=article.id,
            feed_id=article.feed_id,
            feed_title=custom_title or feed_title,
            url=article.url,
            title=article.title,
            author=article.author,
            summary=article.summary,
            snippet=_make_snippet(article.summary, article.content),
            body_permanently_empty=body_permanently_empty(article, extract_readable),
            readable_active=(
                article.readable_status == "pending" and not article.readable_retries
            ),
            nothing_to_show=not (
                article.readable_content or article.content or article.summary
            ),
            published_at=article.published_at,
            formatted_date=_format_date(article.published_at or article.created_at),
            estimated_read_min=article.estimated_read_min,
            image_url=article.image_url,
            is_read=state.is_read if state else False,
            is_starred=state.is_starred if state else False,
            is_archived=state.is_archived if state else False,
            is_saved=bool(state and state.saved_at),
            ai_score=state.ai_score if state else None,
            labels=labels_by_article.get(article.id, []),
            sort_ts=article.published_at or article.fetched_at,
        ))
    return items


async def get_article_list_item(
    user: User, article_id: int, db: AsyncSession
) -> ArticleListItem | None:
    """One article in list-row form, for re-rendering a single row in place.

    ``get_article`` returns an ArticleResponse, which lacks the fields a row needs
    (snippet, formatted_date, body_permanently_empty), hence this sibling. Access
    goes through the shared predicate, so a saved article with no feed resolves the
    same way it does everywhere else.
    """
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
            UserFeed.extract_readable.label("extract_readable"),
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .where(Article.id == article_id, Article.trimmed_at.is_(None))
    )
    stmt = add_article_access_joins(stmt, user.id).where(article_access_predicate())
    row = (await db.execute(stmt)).first()
    if row is None:
        return None

    article, state, feed_title, custom_title, extract_readable = row
    labels = [
        {"id": lid, "name": lname, "color": lcolor}
        for lid, lname, lcolor in (await db.execute(
            select(Label.id, Label.name, Label.color)
            .join(ArticleLabel, ArticleLabel.label_id == Label.id)
            .where(
                ArticleLabel.article_id == article.id,
                ArticleLabel.user_id == user.id,
            )
            .order_by(Label.position, func.lower(Label.name))
        )).all()
    ]
    return ArticleListItem(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        summary=article.summary,
        snippet=_make_snippet(article.summary, article.content),
        body_permanently_empty=body_permanently_empty(article, extract_readable),
        readable_active=(
            article.readable_status == "pending" and not article.readable_retries
        ),
        nothing_to_show=not (
            article.readable_content or article.content or article.summary
        ),
        published_at=article.published_at,
        formatted_date=_format_date(article.published_at or article.created_at),
        estimated_read_min=article.estimated_read_min,
        image_url=article.image_url,
        is_read=state.is_read if state else False,
        is_starred=state.is_starred if state else False,
        is_archived=state.is_archived if state else False,
        is_saved=bool(state and state.saved_at),
        ai_score=state.ai_score if state else None,
        labels=labels,
        sort_ts=article.published_at or article.fetched_at,
    )


async def _fetch_labels(article_id: int, user_id: int, db: AsyncSession) -> list[dict]:
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
    stmt = add_article_access_joins(
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        ).outerjoin(Feed, Feed.id == Article.feed_id),
        user.id,
    ).where(
        Article.id == article_id,
        article_access_predicate(),
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
        summary=article.summary,
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
        is_saved=bool(state and state.saved_at),
        read_at=state.read_at if state else None,
        share_token=state.share_token if state else None,
        ai_summary=state.ai_summary if state else None,
        ai_summary_truncated=state.ai_summary_truncated if state else False,
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
    saved_only: bool = False,
    labeled_only: bool = False,
) -> None:
    """Bulk mark as read all articles in scope with fetched_at <= before.

    Starred/archived/saved scopes only UPDATE (state row is guaranteed to exist).
    All other scopes upsert to handle articles with and without existing state rows.
    """
    now = datetime.now(timezone.utc)

    if starred_only or archived_only or saved_only:
        # Articles in these views already have a state row by definition – plain UPDATE suffices.
        # Drive the UPDATE from a subquery so we never materialize IDs into Python
        # (which previously blew past asyncpg's 32767-parameter limit on large feeds).
        if starred_only:
            filter_cond = UserArticleState.is_starred == True
        elif archived_only:
            filter_cond = UserArticleState.is_archived == True
        else:
            filter_cond = UserArticleState.saved_at.is_not(None)
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
        add_article_access_joins(select(Article.id), user_id).where(
            Article.id.in_(article_ids),
            article_access_predicate(),
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


async def _load_article_for_write(user: User, article_id: int, db: AsyncSession):
    """Load an article the user may act on, plus their state (created if missing) and
    the display fields. Returns ``(article, state, feed_title, custom_title,
    extract_readable)`` or ``None`` when inaccessible. Shared by the toggle and
    update paths so both use one query and one access check."""
    stmt = add_article_access_joins(
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
            UserFeed.extract_readable,
        ).outerjoin(Feed, Feed.id == Article.feed_id),
        user.id,
    ).where(
        Article.id == article_id,
        article_access_predicate(),
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None
    article, state, feed_title, custom_title, extract_readable = row
    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)
    return article, state, feed_title, custom_title, extract_readable


def _state_response(article, state, feed_title, custom_title, labels) -> ArticleResponse:
    """Build the ArticleResponse returned by the state-write endpoints."""
    return ArticleResponse(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        content=article.content,
        content_source=article.content_source,
        summary=article.summary,
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
        is_saved=state.saved_at is not None,
        read_at=state.read_at,
        labels=labels,
    )


async def toggle_article_state(
    user: User,
    article_id: int,
    field: str,
    db: AsyncSession,
) -> ArticleResponse | None:
    """Toggle a single boolean field (is_read/is_starred/is_archived) in one DB round-trip."""
    assert field in {"is_read", "is_starred", "is_archived"}
    loaded = await _load_article_for_write(user, article_id, db)
    if loaded is None:
        return None
    article, state, feed_title, custom_title, extract_readable = loaded

    new_value = not getattr(state, field, False)
    setattr(state, field, new_value)

    if field == "is_read":
        state.read_at = datetime.now(timezone.utc) if new_value else None

    if field == "is_starred":
        _apply_star_side_effects(state, article, starred=new_value, extract_readable=bool(extract_readable))

    await db.commit()
    await db.refresh(state)
    labels = await _fetch_labels(article_id, user.id, db)
    return _state_response(article, state, feed_title, custom_title, labels)


async def update_article_state(
    user: User,
    article_id: int,
    payload: ArticleStateUpdate,
    db: AsyncSession,
) -> ArticleResponse | None:
    """Set is_read / is_starred / is_archived / is_saved from a payload. Creates
    UserArticleState if needed. One round-trip: load, apply, commit, respond from
    loaded data."""
    loaded = await _load_article_for_write(user, article_id, db)
    if loaded is None:
        return None
    article, state, feed_title, custom_title, extract_readable = loaded

    if payload.is_read is not None:
        state.is_read = payload.is_read
        state.read_at = datetime.now(timezone.utc) if payload.is_read else None

    if payload.is_starred is not None:
        was_starred = bool(state.is_starred)
        state.is_starred = payload.is_starred
        if payload.is_starred != was_starred:
            _apply_star_side_effects(
                state, article, starred=payload.is_starred, extract_readable=bool(extract_readable)
            )

    if payload.is_archived is not None:
        state.is_archived = payload.is_archived

    if payload.is_saved is not None:
        # saved_at is a timestamp rather than a flag (retention reads it, and it is
        # what exempts the article from a purge), so the payload's boolean is turned
        # into one here. Nothing is fetched either way: this pins an article the user
        # can already reach, whereas save_article_by_url is what imports an address
        # and extracts it. Re-saving a feedless article dropped from Saved therefore
        # goes through that path and not this one, since unsaving it took away the
        # access this write needs.
        state.saved_at = datetime.now(timezone.utc) if payload.is_saved else None

    await db.commit()
    await db.refresh(state)
    labels = await _fetch_labels(article_id, user.id, db)
    return _state_response(article, state, feed_title, custom_title, labels)
