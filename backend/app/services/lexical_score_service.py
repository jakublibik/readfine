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

A row still only appears once the reader has terms, so "no row" keeps one
honest meaning: this article was never scored for them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Sequence

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.services import relevance_corpus_service
from app.services.relevance_service import (
    CorpusStats,
    article_text,
    lexical_score,
    parse_terms,
    terms_needed,
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


Terms = tuple[str, ...]


async def load_terms(db: AsyncSession, user_ids: list[int]) -> dict[int, Terms]:
    """Parsed term lists of the users who have lexical scoring to do.

    A user with the feature off, or with no terms, is left out entirely rather
    than mapped to an empty list: the caller's `if not terms` is then the whole
    gate, and no article gets tokenized for nothing.
    """
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(UserSettings.user_id, UserSettings.relevance_terms)
        .where(UserSettings.user_id.in_(user_ids),
               UserSettings.basic_scoring_enabled == True)  # noqa: E712
    )).all()
    out = {}
    for user_id, text in rows:
        terms = parse_terms(text)
        if terms:
            out[user_id] = tuple(terms)
    return out


async def stats_for(db: AsyncSession,
                    terms_by_user: dict[int, Terms]) -> CorpusStats | None:
    """The corpus statistics, with the prefix counts these term lists need."""
    stats = await relevance_corpus_service.get_stats(db)
    if stats is None:
        return None
    prefixes: set[str] = set()
    for terms in terms_by_user.values():
        prefixes |= terms_needed(terms)[1]
    return await relevance_corpus_service.with_prefixes(db, stats, prefixes)


async def score_articles_for_users(db: AsyncSession, articles: Sequence[Scorable],
                                   terms_by_user: dict[int, Terms],
                                   stats: CorpusStats) -> int:
    """Score each article for each user and write the scores that changed.

    `stats` has to carry the prefix counts of every term list here, see
    `stats_for`.
    """
    if not articles or not terms_by_user:
        return 0

    existing = {
        (s.user_id, s.article_id): s
        for s in (await db.scalars(
            select(UserArticleState).where(
                UserArticleState.user_id.in_(terms_by_user),
                UserArticleState.article_id.in_([a.id for a in articles]),
            )
        )).all()
    }

    written = 0
    for article in articles:
        text = article_text(article.title, article.body)
        for user_id, terms in terms_by_user.items():
            score = lexical_score(text, terms, stats)
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
    first build) and for a feed whose subscribers have no terms.
    """
    if not articles:
        return 0
    user_ids = list(await db.scalars(
        select(UserFeed.user_id).where(UserFeed.feed_id == feed_id)))
    terms_by_user = await load_terms(db, user_ids)
    if not terms_by_user:
        return 0
    stats = await stats_for(db, terms_by_user)
    if stats is None:
        return 0
    return await score_articles_for_users(
        db, [Scorable.of(a) for a in articles], terms_by_user, stats)


def scoring_active(settings: UserSettings) -> bool:
    """Does this reader's basic relevance produce scores: switched on, with terms?"""
    return bool(settings.basic_scoring_enabled and parse_terms(settings.relevance_terms))


async def clear_scores(db: AsyncSession, settings: UserSettings) -> int:
    """Drop every basic score the reader has, for when their scoring stops.

    Switching basic relevance off or emptying the list stops new scores, but the
    ones already written would otherwise keep showing in the list and keep
    feeding Catch me up and score filters, from a list the reader no longer has.
    Resetting the backfill stamp makes turning it back on catch the last week up
    again, the same as saving new terms. Does not commit.
    """
    settings.lexical_backfill_at = None
    result = await db.execute(
        update(UserArticleState)
        .where(UserArticleState.user_id == settings.user_id,
               UserArticleState.lexical_score.isnot(None))
        .values(lexical_score=None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


# ── backfill after a change to the terms ──────────────────────────────────────

# Unread and published within the last week, whichever way the terms were saved
# (by hand, at onboarding, from the seed), because two windows would mean two
# answers to "why does this article have no score".
BACKFILL_DAYS = 7
_BACKFILL_CHUNK = 500
# Accounts caught up per pass. A backfill is seconds of work, but the first pass
# after the release has every account with terms due at once, and the point
# of a cap is that this does not become one long transaction.
_BACKFILL_USERS_PER_RUN = 5


async def backfill_user(db: AsyncSession, user_id: int, terms: Terms,
                        stats: CorpusStats, *, days: int = BACKFILL_DAYS) -> int:
    """Score the reader's recent unread articles against their current terms.

    Deliberately not everything unread: on an account with twelve thousand unread
    articles every save of the terms would otherwise write twelve thousand rows.
    Older articles stay unscored,
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
            db, [Scorable(*r) for r in rows], {user_id: terms}, stats)
        await db.commit()
    return written


async def process_due_backfills(db: AsyncSession) -> int:
    """Catch up the accounts whose terms are newer than their last backfill.

    Only the basic terms make an account due. Regenerating the AI profile does
    not touch the lexical score, so it has nothing to catch up.
    """
    due = dict((await db.execute(
        select(UserSettings.user_id, UserSettings.relevance_terms_updated_at)
        .where(UserSettings.basic_scoring_enabled == True,  # noqa: E712
               UserSettings.relevance_terms.isnot(None),
               UserSettings.relevance_terms_updated_at.isnot(None),
               or_(UserSettings.lexical_backfill_at.is_(None),
                   UserSettings.lexical_backfill_at
                   < UserSettings.relevance_terms_updated_at))
        .order_by(UserSettings.lexical_backfill_at.asc().nulls_first())
        .limit(_BACKFILL_USERS_PER_RUN)
    )).all())
    if not due:
        return 0

    terms_by_user = await load_terms(db, list(due))
    stats = await stats_for(db, terms_by_user)
    if stats is None:
        return 0

    done = 0
    for user_id, terms_updated_at in due.items():
        terms = terms_by_user.get(user_id)
        # Stamped even when there is nothing to score (a list with no usable
        # term left in it), or the account would come up due forever.
        if terms is not None:
            written = await backfill_user(db, user_id, terms, stats)
            logger.info("lexical backfill: user=%s wrote %d scores", user_id, written)
        # Stamped with the save it caught up with, not with the time it finished.
        # Terms saved while this ran (a few suggestion clicks in a row) are newer
        # than that, so the account stays due and the next pass scores them. A
        # save between the query above and `load_terms` only costs a repeat run.
        settings = await db.get(UserSettings, user_id)
        settings.lexical_backfill_at = terms_updated_at
        await db.commit()
        done += 1
    return done
