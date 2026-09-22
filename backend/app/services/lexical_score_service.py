"""Writing the lexical relevance score: at fetch time, for every subscriber.

Unlike AI scoring, which runs only on articles a filter labeled, this runs on
everything that arrives. That is the whole point: a new account has no filters,
so under the label gate it would never see a score at all.

It is its own pass over the feed's subscribers rather than a step inside
`filter_service.apply_filters_to_new_articles`, which skips any subscriber with
no filters — exactly the readers this exists for.

**A zero is stored like any other score**, although 56% of the articles nobody
labeled score exactly that. Leaving them out would halve the rows, and it was
how this first shipped, but it makes 0.0 indistinguishable from "never scored"
and those two have to behave differently in one place that matters: a filter
saying `score < 0.3 -> mark read` reads a missing row as NULL, so the articles
with no overlap at all, the least relevant there are, would be the ones that
escape it while a 0.25 got swept. Measured cost of storing them: around 340
bytes a row including indexes, so tens of megabytes at production size.

A row still only appears once the reader has a profile, so "no row" keeps one
honest meaning: this article was never scored for them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.services import relevance_corpus_service
from app.services.relevance_service import (
    CorpusStats,
    Profile,
    article_text,
    lexical_score,
    parse_profile,
)

logger = logging.getLogger(__name__)

# Raw characters of body read per article, same reasoning as the corpus job: the
# scorer looks at 300 characters of stripped text, and this is what that costs
# with markup around it.
_BODY_CHARS = 4000


class Scorable(NamedTuple):
    """The three fields scoring needs, so a caller never has to load a body.

    The backfill walks thousands of articles and would otherwise pull every
    extracted article text into memory to read the first 300 characters of it.
    """

    id: int
    title: str
    body: str | None

    @classmethod
    def of(cls, article: Article) -> "Scorable":
        return cls(article.id, article.title,
                   article.readable_content or article.content)


async def load_profiles(db: AsyncSession,
                        user_ids: list[int]) -> dict[int, Profile]:
    """Parsed interest profiles of the users who have lexical scoring to do.

    A user with the feature off, or with an empty profile, is left out entirely
    rather than mapped to an empty profile: the caller's `if not profiles` is
    then the whole gate, and no article gets tokenized for nothing.
    """
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(UserSettings.user_id, UserSettings.ai_preference_text)
        .where(UserSettings.user_id.in_(user_ids),
               UserSettings.basic_scoring_enabled == True)  # noqa: E712
    )).all()
    profiles = {}
    for user_id, text in rows:
        profile = parse_profile(text)
        if profile:
            profiles[user_id] = profile
    return profiles


async def score_articles_for_users(db: AsyncSession, articles: Sequence[Scorable],
                                   profiles: dict[int, Profile],
                                   stats: CorpusStats) -> int:
    """Score each article for each user and write the scores that are non-zero.

    The article text is tokenized once per article, not once per article and
    user: the corpus statistics are instance-wide, so only the profile differs
    between readers.
    """
    if not articles or not profiles:
        return 0

    existing = {
        (s.user_id, s.article_id): s
        for s in (await db.scalars(
            select(UserArticleState).where(
                UserArticleState.user_id.in_(profiles),
                UserArticleState.article_id.in_([a.id for a in articles]),
            )
        )).all()
    }

    written = 0
    for article in articles:
        text = article_text(article.title, article.body)
        for user_id, profile in profiles.items():
            score = lexical_score(text, profile, stats)
            if score is None:
                continue  # the scorer had nothing to say; not the same as 0.0
            state = existing.get((user_id, article.id))
            if state is None:
                state = UserArticleState(user_id=user_id, article_id=article.id)
                db.add(state)
                existing[(user_id, article.id)] = state
            if state.lexical_score != score:
                state.lexical_score = score
                written += 1
    return written


async def score_new_articles(db: AsyncSession, feed_id: int,
                             articles: list[Article]) -> int:
    """Fetch-time entry point: score a feed's new articles for its subscribers.

    Silent no-op before the corpus statistics exist (a fresh install, up to the
    first nightly build) and for a feed whose subscribers have no profile.
    """
    if not articles:
        return 0
    user_ids = list(await db.scalars(
        select(UserFeed.user_id).where(UserFeed.feed_id == feed_id)))
    profiles = await load_profiles(db, user_ids)
    if not profiles:
        return 0
    stats = await relevance_corpus_service.get_stats(db)
    if stats is None:
        return 0
    return await score_articles_for_users(
        db, [Scorable.of(a) for a in articles], profiles, stats)


# ── backfill after a profile change ───────────────────────────────────────────

# Unread and published within the last week. One rule for both cases the plan
# names (a profile typed by hand and one generated automatically), because two
# windows would mean two answers to "why does this article have no score".
BACKFILL_DAYS = 7
_BACKFILL_CHUNK = 500
# Accounts caught up per pass. A backfill is seconds of work, but the first pass
# after the release has every account with a profile due at once, and the point
# of a cap is that this does not become one long transaction.
_BACKFILL_USERS_PER_RUN = 5


async def backfill_user(db: AsyncSession, user_id: int, profile: Profile,
                        stats: CorpusStats, *, days: int = BACKFILL_DAYS) -> int:
    """Score the reader's recent unread articles against the current profile.

    Deliberately not everything unread: on an account with twelve thousand unread
    articles the first profile save would otherwise write twelve thousand rows,
    and again after every automatic regeneration. Older articles stay unscored,
    and Stats and missed gems are what lead back to them, not the ordering.

    Chunked and committed as it goes, so an interrupted run leaves the work it
    already did and the account simply stays due.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    written = 0
    last_id = 0
    while True:
        rows = (await db.execute(
            select(Article.id, Article.title,
                   func.coalesce(
                       func.nullif(func.left(Article.readable_content, _BODY_CHARS), ""),
                       func.left(Article.content, _BODY_CHARS)))
            .join(UserFeed, UserFeed.feed_id == Article.feed_id)
            .outerjoin(UserArticleState,
                       and_(UserArticleState.article_id == Article.id,
                            UserArticleState.user_id == user_id))
            .where(UserFeed.user_id == user_id,
                   Article.id > last_id,
                   func.coalesce(Article.published_at, Article.fetched_at) >= cutoff,
                   or_(UserArticleState.user_id.is_(None),
                       UserArticleState.is_read == False))  # noqa: E712
            .order_by(Article.id)
            .limit(_BACKFILL_CHUNK)
        )).all()
        if not rows:
            break
        last_id = rows[-1][0]
        written += await score_articles_for_users(
            db, [Scorable(*r) for r in rows], {user_id: profile}, stats)
        await db.commit()
    return written


async def process_due_backfills(db: AsyncSession) -> int:
    """Catch up the accounts whose profile is newer than their last backfill."""
    due = (await db.execute(
        select(UserSettings.user_id)
        .where(UserSettings.basic_scoring_enabled == True,  # noqa: E712
               UserSettings.ai_preference_text.isnot(None),
               UserSettings.ai_preference_updated_at.isnot(None),
               or_(UserSettings.lexical_backfill_at.is_(None),
                   UserSettings.lexical_backfill_at
                   < UserSettings.ai_preference_updated_at))
        .order_by(UserSettings.lexical_backfill_at.asc().nulls_first())
        .limit(_BACKFILL_USERS_PER_RUN)
    )).scalars().all()
    if not due:
        return 0

    stats = await relevance_corpus_service.get_stats(db)
    if stats is None:
        return 0
    profiles = await load_profiles(db, list(due))

    done = 0
    for user_id in due:
        profile = profiles.get(user_id)
        # Stamped even when there is nothing to score (an unparseable profile, or
        # one that lost its topics), or the account would come up due forever.
        if profile is not None:
            written = await backfill_user(db, user_id, profile, stats)
            logger.info("lexical backfill: user=%s wrote %d scores", user_id, written)
        settings = await db.get(UserSettings, user_id)
        settings.lexical_backfill_at = datetime.now(timezone.utc)
        await db.commit()
        done += 1
    return done
