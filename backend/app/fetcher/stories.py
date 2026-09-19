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

A match alone is not membership. The first shipped version joined groups transitively:
one match to any member let an article in, and two groups merged the moment one article
resembled both. On the production corpus that produced groups of 407 and 514 articles
spanning ten days, in which a thousandth of the pairs were over the threshold and plenty
scored zero against each other. A group has to be more than the chain that built it, so
an article now has to match at least half the members and arrive inside the window of
the group's root; see ``_pick_group``.

The grouping is global, shared by every user. A group can therefore contain articles from
feeds a given reader doesn't subscribe to, so anything user-facing has to filter members
through ``article_access_predicate``.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.services.story_service import DEDUP_SUPPRESS, SUPPRESSED_BY_SIMILAR

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

# How much of a group an article has to match to join it, as a fraction of its members.
# A half, which is the weakest rule that still says something about the group rather
# than about one lucky neighbour: it leaves pairs and triples exactly as they were (for
# a group of one or two, half of it is one member, which is the old rule) and bites from
# three members up, where the chains start. Measured on the six worst production groups:
# 514 members became 277 groups of at most 15.
MEMBERSHIP_SHARE = 0.5

# Runaway guard, not a mechanism. With the rule above the largest group measured on the
# production corpus was 17, so this should never fire; it is here because the failure it
# guards against was a 514-member group that nothing noticed for a week.
MAX_GROUP_SIZE = 40

# Words that mark a headline as a different piece from the one it resembles rather than
# another outlet's account of it: an explainer written off the back of the news ("how",
# "why", "what happens"), or a second actor doing the thing that was already reported
# ("joins", "replace"). Where one headline has such a word and the other does not, the
# pair still groups, but it is not evidence the reader has seen this news.
#
# Short and specific on purpose. A wider list was measured and it was worse than nothing:
# vague framing words ("amid", "latest", "more", "instead", "update") fire about as often
# on genuine duplicates as on these, and vetoing on them cost two to ten right decisions
# for every wrong one they caught. English only, because that is the only vocabulary the
# measurement covered; guessing at a Czech or German list is how the thresholds went
# wrong the first time. See scripts/BENCHMARKS.md.
FOLLOW_UP_CUES = frozenset({
    "how", "why", "happens", "explainer", "explained", "explains", "guide",
    "joins", "replace", "successor",
})

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def reads_as_follow_up(title_norm: str, other_norm: str) -> bool:
    """Does exactly one of the two headlines frame itself as a later, separate piece?

    Both sides are checked because the cue has to be what distinguishes them. Two
    how-to guides about the same launch both say "how", which says nothing about whether
    they are the same piece, and the reader who read one has in every meaningful sense
    seen the other.
    """
    words = set(_WORD_SPLIT.split(title_norm))
    other = set(_WORD_SPLIT.split(other_norm))
    return bool((words ^ other) & FOLLOW_UP_CUES)


class Pair(NamedTuple):
    """One near-duplicate match, carrying everything both callers read off it.

    Grouping and hiding are decided from the same match but not on the same terms, so
    the pair holds the score and the cue verdict rather than either caller recomputing
    them: ``_link`` groups on similarity alone, ``suppress_seen`` also wants to know
    whether the two headlines are the same piece of writing.
    """

    new_id: int
    cand_id: int
    cand_story: int | None
    similarity: float
    follow_up: bool


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
    # Committed whatever the grouping decided, because grouping is not the only thing
    # _link writes: it also hides articles from readers who have already read the same
    # news, and a round can do that without grouping anything at all.
    await db.commit()
    return linked


async def _link(new_ids: list[int], db: AsyncSession) -> int:
    """Place each new article in at most one existing group, oldest article first.

    Deliberately not union-find any more. Joining is one-directional: an article joins a
    group that is already there, and two groups never merge because one article happens
    to resemble both. That merge is what turned a stray match into a group of 514, and
    nothing is lost by refusing it — the article still lands in whichever of the two it
    fits better, and the other group stays the story it was.

    New articles are walked in id order so that a batch behaves exactly like the same
    articles arriving one fetch apart, and so the group's id stays the id of its oldest
    member: a candidate that is itself waiting in this batch is skipped, and picked up
    when its own turn comes.
    """
    if not new_ids:
        return 0

    rows, timestamps = await _find_pairs(new_ids, db)
    if not rows:
        return 0

    # Hiding is a pairwise decision against an article the reader actually read, so it
    # is taken from the matches themselves and is unaffected by what the grouping below
    # makes of them. Kept first for that reason: the two are independent.
    await suppress_seen(rows, db)

    by_new: dict[int, list[Pair]] = {}
    for pair in rows:
        by_new.setdefault(pair.new_id, []).append(pair)

    pending = set(new_ids)
    groups = await _load_groups(
        {p.cand_story if p.cand_story is not None else p.cand_id for p in rows}, db
    )

    joined: dict[int, int] = {}
    for new_id in sorted(by_new):
        pending.discard(new_id)
        matches: dict[int, list[float]] = {}
        for pair in by_new[new_id]:
            if pair.cand_id in pending:
                continue  # its turn has not come; it will find this one instead
            group_id = joined.get(pair.cand_id)
            if group_id is None:
                group_id = pair.cand_story if pair.cand_story is not None else pair.cand_id
            matches.setdefault(group_id, []).append(pair.similarity)

        chosen = _pick_group(matches, groups, timestamps[new_id])
        if chosen is None:
            continue
        joined[new_id] = chosen
        groups[chosen].size += 1

    if not joined:
        return 0

    by_group: dict[int, list[int]] = {}
    for article_id, group_id in joined.items():
        by_group.setdefault(group_id, []).append(article_id)

    linked = 0
    for group_id, ids in by_group.items():
        # The root is included: a candidate that was on its own until now is what the
        # group is named after, and this is where it gets its own story_id.
        result = await db.execute(
            update(Article)
            .where(Article.id.in_([*ids, group_id]),
                   Article.story_id.is_distinct_from(group_id))
            .values(story_id=group_id)
        )
        linked += result.rowcount or 0
    return linked


@dataclass
class GroupState:
    """What membership is decided against: how big the group is and when it started.

    Mutable because the batch updates it as it goes — an article joining a group makes
    the group harder to join for the next one, which is the same arithmetic a later
    fetch would do.
    """

    size: int
    root_ts: datetime


async def _load_groups(group_ids: set[int], db: AsyncSession) -> dict[int, GroupState]:
    """Size and starting time of every group the candidates belong to.

    A group is identified by its root article, so the root's own timestamp is the
    group's, and a candidate with no story_id yet is a group of one that is about to get
    a name. Both cases come out of the same query.
    """
    if not group_ids:
        return {}
    root = Article.__table__.alias("root")
    stmt = (
        select(
            root.c.id,
            func.coalesce(root.c.published_at, root.c.fetched_at),
            select(func.count())
            .select_from(Article.__table__)
            .where(Article.__table__.c.story_id == root.c.id)
            .scalar_subquery(),
        )
        .select_from(root)
        .where(root.c.id.in_(group_ids))
    )
    return {
        row[0]: GroupState(size=max(1, row[2]), root_ts=row[1])
        for row in (await db.execute(stmt)).all()
    }


def _pick_group(
    matches: dict[int, list[float]],
    groups: dict[int, "GroupState"],
    ts: datetime,
) -> int | None:
    """Which group, if any, this article belongs to: the best one it truly matches.

    Three things have to hold, and the first is the whole point. Matching half the
    members means the article resembles the group, not one member of it; a chain of
    pairwise matches can walk a group anywhere, and this is what stops it walking.

    The window is measured from the root rather than from the member that matched,
    which gives a group a finite life: without it a group re-anchors on its newest
    member at every fetch and crawls forward indefinitely, which is how groups came to
    span ten days inside a 72 h rule.
    """
    best, best_score = None, 0.0
    for group_id, hits in matches.items():
        group = groups.get(group_id)
        if group is None:
            # Root purged from under the group. Its members are older than the root, so
            # the group is outside the window anyway.
            continue
        if len(hits) < group.size * MEMBERSHIP_SHARE:
            continue
        if group.size >= MAX_GROUP_SIZE:
            continue
        if abs((ts - group.root_ts).total_seconds()) > WINDOW_HOURS * 3600:
            continue
        score = sum(hits) / len(hits)
        if score > best_score:
            best, best_score = group_id, score
    return best


async def suppress_seen(rows: list[Pair], db: AsyncSession) -> int:
    """Hide a new article from readers who have already read the same news.

    Opt-in (``UserSettings.story_dedup == 'collapse_suppress'``), and decided against
    one article the reader read themselves, never against the group. Group membership
    is transitive at 0.30, which builds clusters of up to 13 where the two ends are not
    the same story at all — fine for folding a list, not for taking an article away. So
    the pair has to clear 0.40 directly, and it has to not read as a follow-up: an
    explainer written off the back of a report covers the same news and is still
    something the reader has not read.

    "Read it themselves" is ``is_read`` with no ``suppressed_at``: a read written by the
    URL dedup, by a filter, or by finishing another story cannot hide anything, or one
    machine decision would quietly feed the next. An article a filter starred is left
    alone too — that is the reader saying in advance they want it.

    Does not commit; the caller owns the fetch transaction. Returns how many (reader,
    article) pairs were hidden.
    """
    seen_by_article: dict[int, list[int]] = {}
    for pair in rows:
        if pair.similarity >= SUPPRESS_THRESHOLD and not pair.follow_up:
            seen_by_article.setdefault(pair.new_id, []).append(pair.cand_id)
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
                 "read_at": now, "suppressed_at": now,
                 "suppressed_by": SUPPRESSED_BY_SIMILAR, "hidden_at": now}
                for uid in readers
            ])
            .on_conflict_do_update(
                index_elements=["user_id", "article_id"],
                # hidden_at is written here and nowhere else, and nothing ever clears
                # it: the reader's own reading takes the two columns beside it off, and
                # the record of what was hidden has to survive that. See 0101.
                set_={"is_read": True, "read_at": now, "suppressed_at": now,
                      "suppressed_by": SUPPRESSED_BY_SIMILAR, "hidden_at": now},
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
) -> tuple[list[Pair], dict[int, datetime]]:
    """Near-duplicate (new article, candidate) pairs, with the candidate's story.

    Also returns each new article's timestamp, which the grouping needs to measure it
    against the root of a group it might join, and which is already loaded here.

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

    pairs: list[Pair] = []
    timestamps: dict[int, datetime] = {}
    for article_id, feed_id, title_norm, ts in new_rows:
        timestamps[article_id] = ts
        candidates = await db.execute(_candidate_stmt(article_id, feed_id, title_norm, ts))
        pairs.extend(
            Pair(article_id, cand_id, story, similarity,
                 reads_as_follow_up(title_norm, cand_norm))
            for cand_id, story, similarity, cand_norm in candidates
        )
    return pairs, timestamps


def _candidate_stmt(article_id: int, feed_id: int | None, title_norm: str, ts: datetime):
    """Articles that cover the same story as one given article.

    The score comes back with the pair because the two thresholds are read off the same
    match: 0.30 folds the coverage together, 0.40 is what ``suppress_seen`` needs. The
    candidate's own title comes back for the same reason, since hiding also asks whether
    the two headlines read as the same piece of writing.
    """
    window = timedelta(hours=WINDOW_HOURS)
    cand_ts = func.coalesce(Article.published_at, Article.fetched_at)
    return select(
        Article.id, Article.story_id,
        func.similarity(Article.title_norm, title_norm), Article.title_norm,
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
