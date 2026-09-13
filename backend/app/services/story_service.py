"""Reading side of story grouping: the rest of the coverage of one piece of news.

``app.fetcher.stories`` builds the groups, this reads them back for one reader. The
split matters: ``story_id`` is global, so a group routinely holds articles from feeds
this reader doesn't subscribe to. Everything here goes through
``add_article_access_joins`` + ``article_access_predicate``, the same gate as every
other article read path, and nothing outside this module should query members itself.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.schemas.article import ArticleListItem, StoryMember
from app.services.article import add_article_access_joins, article_access_predicate

# A group is small by construction (measured median 2, largest 13), so this is a guard
# against a pathological cluster, not a page size. Nothing paginates the footer.
MEMBER_LIMIT = 25


def _members_query(columns, user_id: int, story_id: int, exclude_article_id: int):
    """Members of one story that this user may see, minus the article being read.

    Trimmed articles are left out for the same reason the list leaves them out: the
    retention pass stripped them down to a snippet and they are gone from every other
    view, so offering one here would open a stub.
    """
    return add_article_access_joins(select(*columns), user_id).where(
        Article.story_id == story_id,
        Article.id != exclude_article_id,
        Article.trimmed_at.is_(None),
        article_access_predicate(),
    )


async def count_members(
    user_id: int, story_id: int | None, article_id: int, db: AsyncSession
) -> int:
    """How many other sources this reader can actually open.

    Asked on every article open, so it stays a count: the footer only needs to know
    whether to render, and the members themselves are fetched when it is unfolded.
    A group can also shrink to nothing visible, through unsubscribing or retention,
    which is why "story_id is set" is never treated as "there is something to show".
    """
    if story_id is None:
        return 0
    return await db.scalar(
        _members_query([func.count(Article.id)], user_id, story_id, article_id)
    ) or 0


async def list_members(
    user_id: int, story_id: int | None, article_id: int, db: AsyncSession
) -> list[StoryMember]:
    """The other coverage, newest first, whatever state it is in.

    Read, starred and (from the suppression branch) hidden articles all belong here:
    the point of the footer is that it shows what the reader would otherwise not find,
    and filtering it by state would hide exactly the articles it exists to surface.
    """
    if story_id is None:
        return []
    rows = (await db.execute(
        _members_query(
            [
                Article.id,
                Article.title,
                Article.url,
                Article.published_at,
                Article.fetched_at,
                Feed.title.label("feed_title"),
                UserFeed.custom_title,
                UserArticleState.is_read,
                UserArticleState.is_starred,
            ],
            user_id, story_id, article_id,
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .order_by(
            func.coalesce(Article.published_at, Article.fetched_at).desc(),
            Article.id.desc(),
        )
        .limit(MEMBER_LIMIT)
    )).all()

    return [
        StoryMember(
            id=r.id,
            title=r.title,
            url=r.url,
            feed_title=r.custom_title or r.feed_title,
            published_at=r.published_at or r.fetched_at,
            is_read=bool(r.is_read),
            is_starred=bool(r.is_starred),
        )
        for r in rows
    ]


def collapse_page(
    items: list[ArticleListItem], already_shown: list[int] | None = None
) -> list[ArticleListItem]:
    """Keep the first row of each story and drop the rest of that group.

    First in the list's current ordering, so the representative is the newest member
    in a newest-first view and the oldest in an oldest-first one. Either way it is the
    row the reader would have looked at anyway.

    ``already_shown`` carries the stories the pages before this one have a row for, so
    a group split by a page boundary is still one group: its remaining members are
    dropped here rather than reappearing further down the list as a second copy of the
    same story. See ``parse_shown`` for where that list comes from.

    The dropped rows are not rendered hidden, they are not rendered at all. A hidden
    ``.article-row`` would be picked up by the IntersectionObserver in app.js, whose
    initial callback fires for a target that intersects nothing: a collapsed row has a
    zero-height rect, that passes the "above the list's top edge" test, and the group
    would mark itself read without anyone seeing it.
    """
    seen: set[int] = set(already_shown or ())
    kept = []
    for item in items:
        if item.story_id is not None:
            if item.story_id in seen:
                continue
            seen.add(item.story_id)
        kept.append(item)
    return kept


# Enough for a long scroll and small enough that the "load more" address stays short:
# a few percent of articles are in a group at all, so a page contributes single digits.
# Overflow drops the oldest, which is safe — a story only reaches 72 hours back, and
# anything dropped is by then far outside the window the next page can still touch.
MAX_SHOWN_STORIES = 300


def parse_shown(raw: str | None) -> list[int]:
    """Read the list of already-rendered stories out of the "load more" address.

    Client input, so it is parsed rather than trusted: whatever is not a number is
    dropped and the length is capped. It can only ever subtract rows from the reader's
    own next page — it takes no part in the access query, and the ids in it are the
    article ids the list has already put in the DOM — so the worst a doctored value
    does is hide something from the person who sent it.
    """
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out[-MAX_SHOWN_STORIES:]


def next_shown(already_shown: list[int], items: list[ArticleListItem]) -> list[int]:
    """The story list to hand to the page after this one."""
    shown = list(already_shown)
    known = set(shown)
    for item in items:
        if item.story_id is not None and item.story_id not in known:
            known.add(item.story_id)
            shown.append(item.story_id)
    return shown[-MAX_SHOWN_STORIES:]


async def annotate(items: list[ArticleListItem], user_id: int, db: AsyncSession) -> None:
    """Fill in ``story_others`` / ``story_seen`` on the rows of one rendered page.

    One batch query for the whole page, in the spirit of the label batch in
    ``list_articles``. Counting on the page itself would not do: a read member is
    absent from an unread-only page and a collapsed group can reach past the page
    boundary, so the badge has to ask the group, not the page.
    """
    story_ids = {item.story_id for item in items if item.story_id is not None}
    if not story_ids:
        return

    rows = (await db.execute(
        add_article_access_joins(
            select(
                Article.story_id,
                func.count(Article.id),
                func.count(Article.id).filter(UserArticleState.is_read.is_(True)),
            ),
            user_id,
        )
        .where(
            Article.story_id.in_(story_ids),
            Article.trimmed_at.is_(None),
            article_access_predicate(),
        )
        .group_by(Article.story_id)
    )).all()
    stats = {story_id: (total, read) for story_id, total, read in rows}

    for item in items:
        if item.story_id is None:
            continue
        # The row itself is one of the members it just counted, hence the subtraction
        # on both numbers. It is always in there: it came out of a list query behind
        # the same access gate.
        total, read = stats.get(item.story_id, (0, 0))
        item.story_others = max(total - 1, 0)
        item.story_seen = read - (1 if item.is_read else 0) > 0
