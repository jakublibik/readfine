"""Reading side of story grouping: the rest of the coverage of one piece of news.

``app.fetcher.stories`` builds the groups, this reads them back for one reader (and,
in ``mark_group_read``, closes one off when the reader is done with it). The
split matters: ``story_id`` is global, so a group routinely holds articles from feeds
this reader doesn't subscribe to. Everything here goes through
``add_article_access_joins`` + ``article_access_predicate``, the same gate as every
other article read path, and nothing outside this module should query members itself.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.schemas.article import ArticleListItem, StoryMember
from app.services.article import add_article_access_joins, article_access_predicate

# A group is small by construction (measured median 2, largest 13), so this is a guard
# against a pathological cluster, not a page size. Nothing paginates the footer.
MEMBER_LIMIT = 25

# The three values of UserSettings.story_dedup. Everyone starts on COLLAPSE, which is
# what the list does today; SUPPRESS adds hiding an article that repeats one the reader
# has read, and OFF leaves the list untouched and the footer out.
DEDUP_OFF = "off"
DEDUP_COLLAPSE = "collapse"
DEDUP_SUPPRESS = "collapse_suppress"
DEDUP_VALUES = (DEDUP_OFF, DEDUP_COLLAPSE, DEDUP_SUPPRESS)

# The value ``suppressed_by`` carries when the machine read is "the reader finished the
# story this belongs to". Named because two functions have to agree on it exactly:
# ``mark_group_read`` writes it and ``reopen_group`` is allowed to undo nothing else.
SUPPRESSED_BY_STORY = "story"

# Time in front of an article that counts as having read it. Same number the stats and
# the retention pass use for the same question; it lives here because this is where it
# decides something — it clears the machine's ``suppressed_at``.
ENGAGED_DWELL_SECONDS = 30


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


def row_count(collapsing: bool = True):
    """SQL count of rows as a collapsing view draws them, for the badge above it.

    ``collapsing=False`` gives a plain count of articles, which is what a reader with
    the feature off must see: their list folds nothing, so a badge that counted stories
    would stand above a list with more rows in it than the number says.

    One per story, one per article that has none: ``coalesce(story_id, -id)`` keys a
    grouped article by its story and an ungrouped one by itself, and ids being positive
    is what keeps the two halves from ever colliding.

    Always scoped to the view it labels, never summed across views. A story runs across
    feeds, so two of its articles in two feeds of one folder are two rows in each feed's
    own list and one row in the folder's, and a folder counter added up from its feeds
    would therefore say something no list ever shows. The per-feed counters stay a plain
    count of articles for the same reason: a feed's own list does not collapse.

    Measured at 33 ms against 30 ms for the plain count over 15k unread articles, so the
    distinct is not what makes a badge expensive.
    """
    if not collapsing:
        return func.count()
    return func.count(func.distinct(func.coalesce(Article.story_id, -Article.id)))


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
    """Fill in ``story_others`` / ``story_read`` on the rows of one rendered page.

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
        item.story_read = max(read - (1 if item.is_read else 0), 0)


async def mark_group_read(
    user_id: int, article_ids: list[int], db: AsyncSession
) -> list[int]:
    """Mark the rest of each article's story read, and say which articles that was.

    Reading one article of a story settles the story: the row in the list stands for
    the event, not for one newsroom's write-up of it, so once the reader is done with
    it the other five must not come back as unread in a label view, in a feed, or on
    the next fetch. Without this the group is only folded away where the list folds
    it, and every other view still counts it as six unread articles.

    The members are marked the way the URL dedup and the filter action mark theirs,
    with ``suppressed_at`` set: the reader read one article, and the rest were closed
    on their behalf. Nothing reads that column yet; it is what will keep the
    suppression rule (phase 4) from chaining, since that only ever triggers on a read
    the reader made themselves. Without it, an article arriving tomorrow could be
    hidden for resembling one of these — one nobody ever looked at. Retention is not
    involved either way: it ignores ``is_read`` on purpose (purge_service, which
    counts dwell, an opened link, or a star).

    Never touches an article that is already read, so a member read properly keeps its
    own read stamp and stays a human read. Does not commit — the caller owns the
    transaction, which is also what keeps this atomic with the read that caused it.
    """
    if not article_ids:
        return []

    stories = select(Article.story_id).where(
        Article.id.in_(article_ids), Article.story_id.is_not(None)
    )
    members = (await db.execute(
        add_article_access_joins(select(Article.id), user_id).where(
            Article.story_id.in_(stories),
            Article.id.not_in(article_ids),
            Article.trimmed_at.is_(None),
            article_access_predicate(),
            (UserArticleState.is_read.is_(None)) | (UserArticleState.is_read.is_(False)),
        )
    )).scalars().all()
    if not members:
        return []

    now = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(UserArticleState)
        .values([
            {"user_id": user_id, "article_id": aid, "is_read": True,
             "is_starred": False, "is_archived": False,
             "read_at": now, "suppressed_at": now, "suppressed_by": SUPPRESSED_BY_STORY}
            for aid in members
        ])
        .on_conflict_do_update(
            index_elements=["user_id", "article_id"],
            set_={"is_read": True, "read_at": now, "suppressed_at": now,
                  "suppressed_by": SUPPRESSED_BY_STORY},
            # The row may have been read between the select above and here; the guard
            # is what makes sure this never restamps somebody's own reading.
            where=(UserArticleState.__table__.c.is_read.is_not(True)),
        )
    )
    return list(members)


async def reopen_group(user_id: int, article_id: int, db: AsyncSession) -> list[int]:
    """Undo ``mark_group_read``: the reader says they have not read this after all.

    Closing a story is the one part of reading a folded row that reaches articles the
    reader never opened, so taking the read mark off that row has to reach them back.
    Otherwise the undo is not one: five articles stay read because of a click that has
    since been taken back, and nothing in the list says why.

    Only members still carrying ``suppressed_by='story'`` are touched, so a member the
    reader went on to read properly keeps its own read mark — spending time on an
    article clears the stamp (the dwell and link-opened handlers), and so does marking
    it read by hand.

    Does not commit; the caller owns the transaction, which is what keeps this atomic
    with the un-read that caused it.
    """
    stories = select(Article.story_id).where(
        Article.id == article_id, Article.story_id.is_not(None)
    )
    members = (await db.execute(
        add_article_access_joins(select(Article.id), user_id).where(
            Article.story_id.in_(stories),
            Article.id != article_id,
            Article.trimmed_at.is_(None),
            article_access_predicate(),
            UserArticleState.is_read.is_(True),
            UserArticleState.suppressed_by == SUPPRESSED_BY_STORY,
        )
    )).scalars().all()
    if not members:
        return []

    await db.execute(
        update(UserArticleState)
        .where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id.in_(members),
        )
        .values(is_read=False, read_at=None, suppressed_at=None, suppressed_by=None)
    )
    return list(members)
