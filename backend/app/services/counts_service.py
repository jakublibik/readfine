"""Counts for the badges over the reading views: the sidebar, the mobile title bar,
and the number a row shows after the reader marks it read or refreshes a feed.

Two kinds, kept apart on purpose. The sidebar asks about every feed, folder and label
at once, so it runs grouped queries of its own. Those carry the list's own predicates
(``unread_clause``, ``visible_article_clause``) and count rows the way the list draws
them (``story_service.row_count``). Everything else asks about one view, and goes
through ``count_articles``, which is the list's query with a count in place of the
rows: a badge over one view cannot disagree with the list it stands above, because
it is that list.

Each sidebar counter is asked for separately rather than added up from the feed ones.
A story runs across feeds, so two of its articles in two feeds of one folder are one
row in the folder's list; summing would count them twice.
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.label import ArticleLabel
from app.models.user import User, UserSettings
from app.services.article import count_articles, unread_clause, visible_article_clause
from app.services.story_service import (
    DEDUP_COLLAPSE, DEDUP_OFF, collapses_stories, row_count,
)


@dataclass
class SidebarCounts:
    """Every badge in the sidebar. Field names are the template's context keys."""
    nav_total: int = 0
    nav_unread: int = 0
    nav_starred: int = 0
    nav_unread_starred: int = 0
    nav_archived: int = 0
    nav_unread_archived: int = 0
    nav_saved: int = 0
    nav_unread_saved: int = 0
    nav_labeled: int = 0
    nav_unread_labeled: int = 0
    feed_total_counts: dict[int, int] = field(default_factory=dict)
    feed_unread_counts: dict[int, int] = field(default_factory=dict)
    # Keyed by folder id, None for feeds outside a folder.
    folder_total_counts: dict[int | None, int] = field(default_factory=dict)
    folder_unread_counts: dict[int | None, int] = field(default_factory=dict)
    label_counts: dict[int, int] = field(default_factory=dict)
    label_unread_counts: dict[int, int] = field(default_factory=dict)


async def story_dedup_of(db: AsyncSession, user_id: int) -> str:
    """The reader's story dedup setting, which decides whether their lists fold."""
    value = await db.scalar(
        select(UserSettings.story_dedup).where(UserSettings.user_id == user_id)
    )
    return value or DEDUP_COLLAPSE


async def sidebar_counts(
    db: AsyncSession, user_id: int, *,
    feed_ids: list[int], label_ids: list[int], story_dedup: str,
) -> SidebarCounts:
    """All sidebar badges for one reader, one query per kind of counter.

    Feed counters are a plain count of articles, since a feed's own list does not
    fold stories. Every other counter counts rows as its list draws them, which is
    one per story unless the reader has the feature off.
    """
    rows_drawn = row_count(story_dedup != DEDUP_OFF)
    uas_join = (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id)
    uf_join = (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user_id)
    visible = visible_article_clause()
    unread = unread_clause()
    c = SidebarCounts()

    # All articles, by folder in the same pass: the subscription join gives both.
    def _subscribed(*cols):
        return (
            select(*cols).select_from(Article)
            .join(UserFeed, uf_join)
            .outerjoin(UserArticleState, uas_join)
        )

    c.nav_total = await db.scalar(_subscribed(rows_drawn).where(visible)) or 0
    c.nav_unread = await db.scalar(_subscribed(rows_drawn).where(visible, unread)) or 0
    c.folder_total_counts = dict((await db.execute(
        _subscribed(UserFeed.folder_id, rows_drawn).where(visible).group_by(UserFeed.folder_id)
    )).all())
    c.folder_unread_counts = dict((await db.execute(
        _subscribed(UserFeed.folder_id, rows_drawn).where(visible, unread)
        .group_by(UserFeed.folder_id)
    )).all())

    # Starred, archived and saved don't fold (collapses_stories), so these count
    # articles. They are anchored on the reader's own state rows, not on a
    # subscription, the same as their lists.
    is_unread = UserArticleState.is_read.is_(False)
    starred = UserArticleState.is_starred.is_(True)
    archived = UserArticleState.is_archived.is_(True)
    saved = UserArticleState.saved_at.is_not(None)
    states = (await db.execute(
        select(
            row_count(False).filter(starred),
            row_count(False).filter(starred & is_unread),
            row_count(False).filter(archived),
            row_count(False).filter(archived & is_unread),
            row_count(False).filter(saved),
            row_count(False).filter(saved & is_unread),
        )
        .select_from(UserArticleState)
        .join(Article, Article.id == UserArticleState.article_id)
        .where(UserArticleState.user_id == user_id, visible)
    )).one()
    (c.nav_starred, c.nav_unread_starred, c.nav_archived, c.nav_unread_archived,
     c.nav_saved, c.nav_unread_saved) = (n or 0 for n in states)

    # Labeled: an article with two labels is one row in the list, so the label is
    # asked for as EXISTS, not joined in (which would count it once per label when
    # nothing folds).
    has_label = (
        select(ArticleLabel.article_id)
        .where(ArticleLabel.article_id == Article.id, ArticleLabel.user_id == user_id)
        .exists()
    )
    labeled = select(rows_drawn).select_from(Article).outerjoin(UserArticleState, uas_join)
    c.nav_labeled = await db.scalar(labeled.where(visible, has_label)) or 0
    c.nav_unread_labeled = await db.scalar(labeled.where(visible, has_label, unread)) or 0

    if feed_ids:
        per_feed = (
            select(Article.feed_id, row_count(False)).select_from(Article)
            .outerjoin(UserArticleState, uas_join)
            .where(Article.feed_id.in_(feed_ids), visible)
            .group_by(Article.feed_id)
        )
        c.feed_total_counts = dict((await db.execute(per_feed)).all())
        c.feed_unread_counts = dict((await db.execute(per_feed.where(unread))).all())

    if label_ids:
        per_label = (
            select(ArticleLabel.label_id, rows_drawn).select_from(ArticleLabel)
            .join(Article, Article.id == ArticleLabel.article_id)
            .outerjoin(UserArticleState, uas_join)
            .where(
                ArticleLabel.user_id == user_id,
                ArticleLabel.label_id.in_(label_ids),
                visible,
            )
            .group_by(ArticleLabel.label_id)
        )
        c.label_counts = dict((await db.execute(per_label)).all())
        c.label_unread_counts = dict((await db.execute(per_label.where(unread))).all())

    return c


async def view_count(
    user: User, db: AsyncSession, *, story_dedup: str, unread: bool = False,
    feed_id: int | None = None, folder_id: int | None = None,
    label_id: int | None = None, labeled_only: bool = False,
    starred_only: bool = False, archived_only: bool = False, saved_only: bool = False,
) -> int:
    """Rows the list of one sidebar view draws, all pages together (``unread``: only
    the unread ones). Folds stories exactly where that list does."""
    collapsing = collapses_stories(
        story_dedup=story_dedup, feed_id=feed_id, starred_only=starred_only,
        archived_only=archived_only, saved_only=saved_only,
    )
    return await count_articles(
        user, db, collapsing=collapsing, unread_only=unread,
        feed_id=feed_id, folder_id=folder_id, label_id=label_id,
        labeled_only=labeled_only, starred_only=starred_only,
        archived_only=archived_only, saved_only=saved_only,
    )


async def view_badge(
    user: User, db: AsyncSession, *, story_dedup: str, **view,
) -> tuple[int, int]:
    """``(unread, total)`` for one sidebar view, the pair a badge is drawn from."""
    unread = await view_count(user, db, story_dedup=story_dedup, unread=True, **view)
    total = await view_count(user, db, story_dedup=story_dedup, **view)
    return unread, total


async def mark_read_total(
    user: User, db: AsyncSession, *,
    starred_only: bool = False, archived_only: bool = False, saved_only: bool = False,
    labeled_only: bool = False, label_id: int | None = None,
) -> int:
    """The total a sidebar row shows after its ✓, for the view that ✓ covered.

    One view only, picked with ``mark_scope_read``'s precedence (starred, archived,
    saved, then label), so a request with several flags counts what was marked.
    """
    story_dedup = await story_dedup_of(db, user.id)
    if starred_only:
        view = {"starred_only": True}
    elif archived_only:
        view = {"archived_only": True}
    elif saved_only:
        view = {"saved_only": True}
    elif label_id is not None:
        view = {"label_id": label_id}
    elif labeled_only:
        view = {"labeled_only": True}
    else:
        view = {}
    return await view_count(user, db, story_dedup=story_dedup, **view)
