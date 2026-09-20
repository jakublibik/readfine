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
an article has to arrive inside the window of the group's root and then clear two tests
at once: it has to match at least half the members, and its mean similarity to every
member has to reach ``MEMBERSHIP_MEAN``. Counting matches alone lets a stock phrase
("what you need to know about") carry an article from one story into another, because
each step of the chain does satisfy the arithmetic. Taking the mean alone goes wrong the
other way: it counts near misses as evidence, and a family of headlines differing in one
name is nothing but near misses. Both tests, so both failures are covered; see
``_pick_group`` and ``story_params``.

The grouping is global, shared by every user. A group can therefore contain articles from
feeds a given reader doesn't subscribe to, so anything user-facing has to filter members
through ``article_access_predicate``.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.fetcher.story_params import (
    COLLAPSE_THRESHOLD,
    MAX_GROUP_SIZE,
    MEMBERSHIP_MEAN,
    MEMBERSHIP_SHARE,
    MIN_TITLE_CHARS,
    SUPPRESS_THRESHOLD,
    WINDOW_HOURS,
)
from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.services.story_service import DEDUP_SUPPRESS, SUPPRESSED_BY_SIMILAR

logger = logging.getLogger(__name__)

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

    rows, articles = await _find_pairs(new_ids, db)
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
        if articles[new_id].story_id is not None:
            # Already in a group, so there is nothing to decide: grouping happens once,
            # and the article is a candidate for the others from here on rather than a
            # decision of its own. Reachable when a manual refresh lands inside a
            # scheduler round, whose post-gather pass then sweeps up articles the
            # refresh has already grouped. Without this the article would be measured
            # against its own group, where it scores 1.0 against itself and lifts its
            # own mean, and would be appended to the membership a second time, making
            # the group look bigger to everything after it in the batch.
            # app.scripts.backfill_stories._assign skips the same case the same way.
            continue
        matches: dict[int, list[float]] = {}
        for pair in by_new[new_id]:
            if pair.cand_id in pending:
                continue  # its turn has not come; it will find this one instead
            group_id = joined.get(pair.cand_id)
            if group_id is None:
                group_id = pair.cand_story if pair.cand_story is not None else pair.cand_id
            matches.setdefault(group_id, []).append(pair.similarity)

        chosen = await _pick_group(matches, groups, articles[new_id], db)
        if chosen is None:
            continue
        joined[new_id] = chosen
        # The group this article just joined is what the next article in the batch is
        # measured against, and its story_id is not written until the end of _link, so
        # the query in _load_groups would not find it. Carrying it here is the only
        # thing that keeps a batch behaving like the same articles arriving one fetch
        # apart, which is what the backfill replays and what the tests assert.
        groups[chosen].members.append(new_id)

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
    """What membership is decided against: who is in the group and when it started.

    The members are ids rather than a count because the second test needs to compare
    the newcomer against each of them, and the count is just their number.

    Mutable because the batch updates it as it goes — an article joining a group makes
    the group harder to join for the next one, which is the same arithmetic a later
    fetch would do.
    """

    members: list[int]
    root_ts: datetime

    @property
    def size(self) -> int:
        return len(self.members)


async def _load_groups(group_ids: set[int], db: AsyncSession) -> dict[int, GroupState]:
    """Members and starting time of every group the candidates belong to.

    A group is identified by its root article, so the root's own timestamp is the
    group's, and a candidate with no story_id yet is a group of one that is about to get
    a name. Both cases come out of the same query: the outer join gives the lone
    candidate a single NULL row, which becomes a membership of one.
    """
    if not group_ids:
        return {}
    root = Article.__table__.alias("root")
    member = Article.__table__.alias("member")
    stmt = (
        select(
            root.c.id,
            func.coalesce(root.c.published_at, root.c.fetched_at),
            member.c.id,
        )
        .select_from(root.outerjoin(member, member.c.story_id == root.c.id))
        .where(root.c.id.in_(group_ids))
    )
    groups: dict[int, GroupState] = {}
    for root_id, root_ts, member_id in (await db.execute(stmt)).all():
        group = groups.get(root_id)
        if group is None:
            group = groups[root_id] = GroupState(members=[], root_ts=root_ts)
        if member_id is not None:
            group.members.append(member_id)
    for root_id, group in groups.items():
        if not group.members:  # a candidate on its own is a group of one
            group.members.append(root_id)
    return groups


async def _pick_group(
    matches: dict[int, list[float]],
    groups: dict[int, "GroupState"],
    article: "NewArticle",
    db: AsyncSession,
) -> int | None:
    """Which group, if any, this article belongs to: the best one it truly matches.

    Four things have to hold, and the two in the middle are the whole point. Matching
    half the members says the article resembles the group rather than one member of it,
    and a mean similarity to *every* member says the members it did not match are at
    least in the same neighbourhood. Either one alone has a failure mode the other
    covers; the module docstring and ``story_params.MEMBERSHIP_MEAN`` say which.

    The window is measured from the root rather than from the member that matched,
    which gives a group a finite life: without it a group re-anchors on its newest
    member at every fetch and crawls forward indefinitely, which is how groups came to
    span ten days inside a 72 h rule.

    The mean is left until last because it is the only test that costs a query, and by
    then there is usually at most one candidate left to run it for.
    """
    shortlist: list[tuple[int, float]] = []
    wanted: set[int] = set()
    for group_id, hits in matches.items():
        group = groups.get(group_id)
        if group is None:
            # Root purged from under the group. Its members are older than the root, so
            # the group is outside the window anyway.
            continue
        if group.size >= MAX_GROUP_SIZE:
            # Documented as something that should never happen, so say when it does
            # rather than let the article quietly stay on its own. Checked before the
            # share test, which changes nothing about the outcome (both refuse the
            # group) and everything about the log: at 40 members the share test asks
            # for 20 matches, so behind it the warning would only ever fire for an
            # article that had already cleared a bar no runaway group would clear.
            logger.warning(
                "story %s is at the %s-member cap; article %s left ungrouped",
                group_id, MAX_GROUP_SIZE, article.id,
            )
            continue
        if len(hits) < group.size * MEMBERSHIP_SHARE:
            continue
        if abs((article.ts - group.root_ts).total_seconds()) > WINDOW_HOURS * 3600:
            continue
        shortlist.append((group_id, sum(hits) / len(hits)))
        wanted.update(group.members)

    if not shortlist:
        return None
    sims = await _similarity_to(article.title_norm, wanted, db)

    best, best_score = None, 0.0
    for group_id, score in shortlist:
        members = groups[group_id].members
        mean = sum(sims.get(m, 0.0) for m in members) / len(members)
        if mean < MEMBERSHIP_MEAN:
            continue
        if score > best_score:
            best, best_score = group_id, score
    return best


async def _similarity_to(
    title_norm: str, ids: set[int], db: AsyncSession
) -> dict[int, float]:
    """Trigram similarity of one title against a set of articles, scored by Postgres.

    Not computed in Python on purpose. Reimplementing pg_trgm here would mean two
    definitions of the same number that have to agree forever, and the one place the
    survey script does reimplement it needed a fix after it silently dropped every
    non-Latin script (see ``survey_dedup.check_trgm``). There is a database in the
    transaction already; it can answer.

    No index is involved and none is wanted: the ids are known, so this is a handful of
    primary-key lookups and a similarity() per row, unlike the candidate search which
    has the whole table to narrow down.
    """
    if not ids:
        return {}
    rows = await db.execute(
        select(Article.id, func.similarity(Article.title_norm, title_norm))
        .where(Article.id.in_(ids))
    )
    return {article_id: float(score or 0.0) for article_id, score in rows}


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


class NewArticle(NamedTuple):
    """What the grouping decision reads about a new article.

    All of it is already loaded by ``_find_pairs`` on its way to the candidate search,
    so it travels with it rather than being fetched again: the timestamp places the
    article against a group's root, the normalised title scores it against the members,
    and the story_id says whether the article is new to the grouping at all.
    """

    id: int
    ts: datetime
    title_norm: str
    story_id: int | None


async def _find_pairs(
    new_ids: list[int], db: AsyncSession
) -> tuple[list[Pair], dict[int, NewArticle]]:
    """Near-duplicate (new article, candidate) pairs, with the candidate's story.

    Also returns what the grouping needs to know about each new article itself, which
    is already loaded here.

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
        select(Article.id, Article.feed_id, Article.title_norm, article_ts,
               Article.story_id)
        .where(
            Article.id.in_(new_ids),
            func.length(Article.title_norm) >= MIN_TITLE_CHARS,
        )
    )).all()

    pairs: list[Pair] = []
    articles: dict[int, NewArticle] = {}
    for article_id, feed_id, title_norm, ts, story_id in new_rows:
        articles[article_id] = NewArticle(article_id, ts, title_norm, story_id)
        candidates = await db.execute(_candidate_stmt(article_id, feed_id, title_norm, ts))
        pairs.extend(
            Pair(article_id, cand_id, story, similarity,
                 reads_as_follow_up(title_norm, cand_norm))
            for cand_id, story, similarity, cand_norm in candidates
        )
    return pairs, articles


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
