"""Cross-source story grouping: one piece of news covered by several feeds.

The existing dedup (``rss._dedup_cross_feed``) keys on an exact normalised URL, so it
only ever catches the same article syndicated under the same link. This module handles
the other half: five newsrooms writing about the same event, under five URLs and five
different headlines. Articles that belong together share a ``story_id``, which is the id
of the oldest member — no table of its own, and merging two groups is one UPDATE.

Matching is lexical (pg_trgm trigram similarity over ``articles.title_norm``) and limited
to a 72 h window, because coverage three weeks apart is a new story about the same
subject rather than the same story. The threshold was measured against a production
export by ``scripts/survey_dedup.py``; 0.30 is where precision breaks down, not a round
number somebody liked. Raising it does not buy accuracy, it just finds less.

The grouping is global, shared by every user. A group can therefore contain articles from
feeds a given reader doesn't subscribe to, so anything user-facing has to filter members
through ``article_access_predicate``.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.services.story_service import DEDUP_SUPPRESS

# Measured, not chosen: see scripts/survey_dedup.py and the plan behind it. At 0.30 the
# production corpus collapses ~11 articles a day out of ~240; the precision cliff sits
# between 0.25 and 0.30 and everything above 0.50 is effectively an exact-title match.
COLLAPSE_THRESHOLD = 0.30
WINDOW_HOURS = 72

# Hiding an article outright is a stronger claim than folding it away, so it asks for a
# stronger match, and one made directly against the article the reader read rather than
# through the group. The same export puts this at ~2.4 articles a day hidden and 2 the
# reader would have wanted over 87 days. Below 0.40 that regret climbs fast.
SUPPRESS_THRESHOLD = 0.40

# Titles shorter than this are not compared at all. A trigram score over a handful of
# trigrams swings wildly, and the fetcher's own "Untitled" placeholder (rss.py) would
# otherwise group every title-less item in the database into one story.
MIN_TITLE_CHARS = 12


async def assign_stories(articles: list[Article], db: AsyncSession) -> int:
    """Link freshly inserted articles to the story of their near-duplicates.

    Called after the flush that gives the articles their ids, inside the fetch
    transaction. Returns how many articles came out of it with a story_id.
    """
    return await _link([a.id for a in articles if a.id is not None], db)


async def assign_stories_global(since: datetime, db: AsyncSession) -> int:
    """Post-gather pass over everything fetched since *since*.

    Two feeds covering the same story in one scheduler round can't see each other's
    uncommitted rows, so the per-feed call above misses exactly the pairs that matter
    most. This runs once the round is over, in the same spirit as
    ``rss.dedup_cross_feed_global``.
    """
    ids = (await db.execute(
        select(Article.id).where(
            Article.fetched_at >= since,
            # Articles saved by URL are left out. At insert their title is a placeholder
            # built from the address and the real one arrives minutes later through
            # extraction, so whether one got grouped would depend on nothing but whether
            # the save happened to land inside a fetch round. They stay eligible as
            # counterparts for feed articles, which costs nothing.
            Article.feed_id.is_not(None),
        )
    )).scalars().all()
    linked = await _link(list(ids), db)
    if linked:
        await db.commit()
    return linked


async def _link(new_ids: list[int], db: AsyncSession) -> int:
    if not new_ids:
        return 0

    rows = await _find_pairs(new_ids, db)
    if not rows:
        return 0

    # Union-find where the root is always the smallest id in the component, which is
    # exactly the story_id definition (oldest member wins). Candidates already carrying
    # a story_id pull their whole group in through that id.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != x:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    members: set[int] = set()
    old_stories: set[int] = set()
    for new_id, cand_id, cand_story, _similarity in rows:
        union(new_id, cand_id)
        members.update((new_id, cand_id))
        if cand_story is not None:
            union(cand_id, cand_story)
            old_stories.add(cand_story)

    # Groups that got absorbed into an older one: their other members are not in
    # `members` (they were never candidates here), so they have to move by story_id.
    for old in old_stories:
        root = find(old)
        if root != old:
            await db.execute(
                update(Article).where(Article.story_id == old).values(story_id=root)
            )

    by_root: dict[int, list[int]] = {}
    for member in members:
        by_root.setdefault(find(member), []).append(member)
    for root, ids in by_root.items():
        await db.execute(
            update(Article)
            .where(Article.id.in_(ids), Article.story_id.is_distinct_from(root))
            .values(story_id=root)
        )

    await suppress_seen(rows, db)
    return len(members & set(new_ids))


async def suppress_seen(
    rows: list[tuple[int, int, int | None, float]], db: AsyncSession
) -> int:
    """Hide a new article from readers who have already read the same news.

    Opt-in (``UserSettings.story_dedup == 'collapse_suppress'``), and decided against
    one article the reader read themselves, never against the group. Group membership
    is transitive at 0.30, which builds clusters of up to 13 where the two ends are not
    the same story at all — fine for folding a list, not for taking an article away. So
    the pair has to clear 0.40 directly.

    "Read it themselves" is ``is_read`` with no ``suppressed_at``: a read written by the
    URL dedup, by a filter, or by finishing another story cannot hide anything, or one
    machine decision would quietly feed the next. An article a filter starred is left
    alone too — that is the reader saying in advance they want it.

    Does not commit; the caller owns the fetch transaction. Returns how many (reader,
    article) pairs were hidden.
    """
    seen_by_article: dict[int, list[int]] = {}
    for new_id, cand_id, _story, similarity in rows:
        if similarity >= SUPPRESS_THRESHOLD:
            seen_by_article.setdefault(new_id, []).append(cand_id)
    if not seen_by_article:
        return 0

    now = datetime.now(timezone.utc)
    hidden = 0
    for new_id, seen_ids in seen_by_article.items():
        # One statement per article rather than one for the batch: at 0.40 the measured
        # corpus produces a handful of these a day, so the loop is shorter than the
        # VALUES list it would replace.
        seen = aliased(UserArticleState)
        feed_of_new = select(Article.feed_id).where(Article.id == new_id).scalar_subquery()
        readers = (await db.execute(
            select(seen.user_id)
            .select_from(seen)
            .join(
                UserSettings,
                (UserSettings.user_id == seen.user_id)
                & (UserSettings.story_dedup == DEDUP_SUPPRESS),
            )
            .join(
                UserFeed,
                (UserFeed.user_id == seen.user_id) & (UserFeed.feed_id == feed_of_new),
            )
            .where(
                seen.article_id.in_(seen_ids),
                seen.is_read.is_(True),
                seen.suppressed_at.is_(None),
            )
            .distinct()
        )).scalars().all()
        if not readers:
            continue

        result = await db.execute(
            pg_insert(UserArticleState)
            .values([
                {"user_id": uid, "article_id": new_id, "is_read": True,
                 "is_starred": False, "is_archived": False,
                 "read_at": now, "suppressed_at": now, "suppressed_by": "similar"}
                for uid in readers
            ])
            .on_conflict_do_update(
                index_elements=["user_id", "article_id"],
                set_={"is_read": True, "read_at": now, "suppressed_at": now,
                      "suppressed_by": "similar"},
                where=(
                    UserArticleState.__table__.c.is_read.is_not(True)
                    & UserArticleState.__table__.c.is_starred.is_not(True)
                ),
            )
        )
        hidden += result.rowcount or 0
    return hidden


async def _find_pairs(
    new_ids: list[int], db: AsyncSession
) -> list[tuple[int, int, int | None, float]]:
    """Near-duplicate (new article, candidate) pairs, with the candidate's story.

    One query per new article rather than a single self-join. The self-join measured
    forty times slower on the development corpus: the two plans cost almost the same on
    paper (8047 against 8066), so the planner flips between a bitmap scan of the trigram
    index and a sequential scan that computes similarity() for every row in the table —
    a coin toss that turns 0.15 s into 6 s. Probing one title at a time leaves no such
    choice, and the cost then scales visibly with the number of new articles.
    """
    # The `%` operator is what lets the GIN trigram index answer this at all, but it
    # reads the pg_trgm.similarity_threshold GUC, whose default (0.3) matching ours is a
    # coincidence waiting to bite. SET LOCAL keeps the value inside this transaction —
    # a plain SET would leak into whatever request reuses this pooled connection next.
    await db.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {COLLAPSE_THRESHOLD}"))

    article_ts = func.coalesce(Article.published_at, Article.fetched_at)
    new_rows = (await db.execute(
        select(Article.id, Article.feed_id, Article.title_norm, article_ts)
        .where(
            Article.id.in_(new_ids),
            func.length(Article.title_norm) >= MIN_TITLE_CHARS,
        )
    )).all()

    pairs: list[tuple[int, int, int | None, float]] = []
    for article_id, feed_id, title_norm, ts in new_rows:
        candidates = await db.execute(_candidate_stmt(article_id, feed_id, title_norm, ts))
        pairs.extend(
            (article_id, cand_id, story, similarity)
            for cand_id, story, similarity in candidates
        )
    return pairs


def _candidate_stmt(article_id: int, feed_id: int | None, title_norm: str, ts: datetime):
    """Articles that cover the same story as one given article.

    The score comes back with the pair because the two thresholds are read off the same
    match: 0.30 folds the coverage together, 0.40 is what ``suppress_seen`` needs.
    """
    window = timedelta(hours=WINDOW_HOURS)
    cand_ts = func.coalesce(Article.published_at, Article.fetched_at)
    return select(
        Article.id, Article.story_id, func.similarity(Article.title_norm, title_norm)
    ).where(
        Article.id != article_id,
        # Same-feed near-duplicates are a newsroom reposting or correcting itself, which
        # is not what this is for. IS DISTINCT FROM also keeps two orphaned articles
        # (feed deleted, feed_id NULL) from pairing up.
        Article.feed_id.is_distinct_from(feed_id),
        func.length(Article.title_norm) >= MIN_TITLE_CHARS,
        Article.title_norm.bool_op("%")(title_norm),
        func.similarity(Article.title_norm, title_norm) >= COLLAPSE_THRESHOLD,
        cand_ts >= ts - window,
        cand_ts <= ts + window,
    )
