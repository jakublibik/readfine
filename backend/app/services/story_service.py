"""Reading side of story grouping: the rest of the coverage of one piece of news.

``app.fetcher.stories`` builds the groups, this reads them back for one reader (and,
in ``mark_group_read``, closes one off when the reader is done with it). The
split matters: ``story_id`` is global, so a group routinely holds articles from feeds
this reader doesn't subscribe to. Everything here goes through
``add_article_access_joins`` + ``article_access_predicate``, the same gate as every
other article read path, and nothing outside this module should query members itself.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.schemas.article import ArticleListItem, StoryMember, SuppressedArticle
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

# And the one the suppression rule writes (fetcher.stories.suppress_seen): this article
# repeats news the reader has already read. The same column also carries reads written
# by the URL dedup, by a filter and by finishing a story, so everything the settings
# page says about suppression keys on this value alone.
SUPPRESSED_BY_SIMILAR = "similar"

# How far back the list of hidden articles in settings reaches. The same 7 days as the
# counter above it on purpose: the list is meant to be that number, not something near
# it. Reaching further would also promise more than retention keeps — a hidden article
# has no engagement by definition, so nothing holds it back from the age delete.
SUPPRESSED_DAYS = 7

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
                UserArticleState.suppressed_at,
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
            read_by_reader=bool(r.is_read) and r.suppressed_at is None,
            is_starred=bool(r.is_starred),
        )
        for r in rows
    ]


def _suppressed_query(columns, user_id: int, days: int):
    """Articles the suppression rule hid from this reader inside the window.

    Asked of ``hidden_at``, which is written once and never cleared, and not of the
    ``suppressed_at`` the decision itself stands on. Every human read takes that one
    off, and checking a hidden article is reading it, so a list built on it emptied
    itself as it was read — including the entries the reader had just decided were
    wrong. What the feature did is a different question from what it is still doing,
    and this is the first one.

    Behind the same access gate as everything else here, so a feed the reader has since
    dropped takes its hidden articles with it, and trimmed articles are left out for the
    reason the footer leaves them out: the row offers to open the article, and there is
    no longer an article there to open. The counter in settings is built on this too, so
    the number and the list under it can never disagree.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return add_article_access_joins(select(*columns), user_id).where(
        article_access_predicate(),
        Article.trimmed_at.is_(None),
        UserArticleState.hidden_at >= cutoff,
    )


async def count_suppressed(
    user_id: int, db: AsyncSession, days: int = SUPPRESSED_DAYS
) -> int:
    """How many articles the suppression rule hid from this reader lately."""
    return await db.scalar(
        _suppressed_query([func.count(Article.id)], user_id, days)
    ) or 0


async def list_suppressed(
    user_id: int, db: AsyncSession, days: int = SUPPRESSED_DAYS
) -> list[SuppressedArticle]:
    """The hidden articles themselves, newest first, with what each lost out to.

    This is the only way to see what the third setting actually did. Unpaginated on
    purpose: it is a week of a handful a day, and a reader for whom it runs long is
    being told something worth knowing by the length itself.
    """
    rows = (await db.execute(
        _suppressed_query(
            [
                Article.id,
                Article.title,
                Article.story_id,
                Feed.title.label("feed_title"),
                UserFeed.custom_title,
                UserArticleState.hidden_at,
                UserArticleState.suppressed_by,
            ],
            user_id, days,
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .order_by(UserArticleState.hidden_at.desc(), Article.id.desc())
    )).all()
    if not rows:
        return []

    against = await _suppressed_against([r.id for r in rows], user_id, db)
    return [
        SuppressedArticle(
            id=r.id,
            title=r.title,
            feed_title=r.custom_title or r.feed_title,
            hidden_at=r.hidden_at,
            # Still out of the list, or has the reader been at it since. Reading one is
            # what undoes the hiding, so a list that says nothing about it would look
            # like it was ignoring what the reader had just done.
            still_hidden=r.suppressed_by == SUPPRESSED_BY_SIMILAR,
            instead_of=against.get(r.id, (None, None))[0],
            match=against.get(r.id, (None, None))[1],
        )
        for r in rows
    ]


async def _suppressed_against(
    article_ids: list[int], user_id: int, db: AsyncSession
) -> dict[int, tuple[str, float]]:
    """For each hidden article, the read headline it most likely lost out to.

    Reconstructed, not recalled. What ``suppress_seen`` writes is that the article was
    hidden, not which article decided it, and adding a column for that would have to be
    kept true through unsubscribes, retention and the reader changing their mind about
    what they have read. So the counterpart is found again the same way it was found the
    first time: it shares the story, the reader read it themselves, and the two headlines
    clear the suppression threshold. Where several qualify, the closest wins.

    Follow-up cues are applied for the same reason the rule applies them — a headline
    the rule would never have hidden anything for cannot be the reason this one went.

    The answer can therefore differ from what really happened: the reader may since have
    un-read that article, or it may be gone. Then the row simply says less, which is why
    the score and the headline travel together and are both optional.
    """
    # Imported here rather than at the top: fetcher.stories imports this module, and the
    # matching rules live with the thresholds they were measured against.
    from app.fetcher.stories import SUPPRESS_THRESHOLD, reads_as_follow_up

    hidden = aliased(Article)
    match = func.similarity(hidden.title_norm, Article.title_norm).label("match")
    rows = (await db.execute(
        add_article_access_joins(
            select(
                hidden.id.label("hidden_id"),
                hidden.title_norm.label("hidden_norm"),
                Article.title,
                Article.title_norm,
                match,
            ).select_from(Article),
            user_id,
        )
        .join(hidden, hidden.story_id == Article.story_id)
        .where(
            hidden.id.in_(article_ids),
            Article.id != hidden.id,
            Article.trimmed_at.is_(None),
            article_access_predicate(),
            UserArticleState.is_read.is_(True),
            UserArticleState.suppressed_at.is_(None),
            match >= SUPPRESS_THRESHOLD,
        )
        .order_by(hidden.id, match.desc())
    )).all()

    best: dict[int, tuple[str, float]] = {}
    for r in rows:
        if r.hidden_id in best:
            continue
        if reads_as_follow_up(r.hidden_norm or "", r.title_norm or ""):
            continue
        best[r.hidden_id] = (r.title, float(r.match))
    return best


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

    Also counts a digest, where it is the SQL twin of ``catchup_service.fold_stories``:
    the estimate above the form is this, the prompt is that, and they have to agree.
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

    A digest folds the same groups by a different rule (``catchup_service.fold_stories``,
    highest score first), because it has no reader ordering to defer to and samples by
    score. Two rules on purpose; change one and look at the other.

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


async def annotate(
    items: list[ArticleListItem],
    user_id: int,
    db: AsyncSession,
    in_scope: set[int] | None = None,
) -> None:
    """Fill in ``story_others`` / ``story_read`` on the rows of one rendered page.

    One batch query for the whole page, in the spirit of the label batch in
    ``list_articles``. Counting on the page itself would not do: a read member is
    absent from an unread-only page and a collapsed group can reach past the page
    boundary, so the badge has to ask the group, not the page.

    ``in_scope`` is the article ids this view would actually give back on unfolding,
    which in a filtered list is not the whole group: a label view unfolds the members
    carrying that label, because those are the rows it folded and the only ones that
    belong in it. Passing None means the view holds the whole group, which is the
    unfiltered case and what every caller did before scoped views existed. The row ends
    up with both numbers, so it can offer to unfold what it has and still say how big
    the story is; see ``story_line`` in article_row.html.

    "Read" here means read by this reader. Finishing a story marks the rest of it read
    on their behalf (``mark_group_read``), so counting those would make the badge say
    "3 read" the moment the reader opened one article and nothing after that: a number
    that is either 0 or all of them tells nobody anything. Counted off the rows rather
    than as an aggregate, because the row doing the counting is a member of its own
    group and has to take itself out of the number, which needs its own answer to the
    same question.
    """
    story_ids = {item.story_id for item in items if item.story_id is not None}
    if not story_ids:
        return

    rows = (await db.execute(
        add_article_access_joins(
            select(Article.id, Article.story_id,
                   UserArticleState.is_read, UserArticleState.suppressed_at),
            user_id,
        )
        .where(
            Article.story_id.in_(story_ids),
            Article.trimmed_at.is_(None),
            article_access_predicate(),
        )
    )).all()

    totals: dict[int, int] = {}
    scoped: dict[int, int] = {}
    reads: dict[int, int] = {}
    read_by_reader: set[int] = set()
    for r in rows:
        totals[r.story_id] = totals.get(r.story_id, 0) + 1
        if in_scope is None or r.id in in_scope:
            scoped[r.story_id] = scoped.get(r.story_id, 0) + 1
        if r.is_read and r.suppressed_at is None:
            reads[r.story_id] = reads.get(r.story_id, 0) + 1
            read_by_reader.add(r.id)

    for item in items:
        if item.story_id is None:
            continue
        # The row itself is one of the members it just counted, hence the subtraction
        # on every number. It is always in there: it came out of a list query behind
        # the same access gate, and it is in scope by definition, since the view drew
        # it. The exception is a row the caller passed no scope for, where the two
        # counts are the same number by construction.
        item.story_total = max(totals.get(item.story_id, 0) - 1, 0)
        item.story_others = max(scoped.get(item.story_id, 0) - 1, 0)
        item.story_read = max(
            reads.get(item.story_id, 0) - (1 if item.id in read_by_reader else 0), 0
        )


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
