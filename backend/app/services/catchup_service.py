"""Catch me up service — article fetching, sampling and metadata building."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil, floor
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.services.article import add_article_access_joins, article_access_predicate
from app.services.scope_tokens import parse_label_tokens, parse_scope_tokens
from app.services.story_service import DEDUP_OFF, DEDUP_SUPPRESS, row_count
from app.utils.text import strip_html

# ── Sampling constants ────────────────────────────────────────────────────────
_CATCHUP_COVERAGE_RATIO = 0.6          # scoring enabled
_CATCHUP_COVERAGE_RATIO_NO_SCORE = 0.8  # scoring disabled


@dataclass
class CatchupArticle:
    id: int
    title: str
    feed_title: str
    published_at: datetime | None
    fetched_at: datetime
    folder_id: int | None
    ai_score: float | None
    ai_summary: str | None
    readable_content: str | None
    content: str | None
    # Story grouping. ``story_id`` and ``has_readable`` come from the query and decide
    # which member of a group represents it (see fold_stories); the two counts are
    # filled in afterwards and mean different things on purpose. ``folded_count`` is how
    # many rows of *this digest* went away behind this one, which is what the sampling
    # uses to give a group back the weight folding took from it. ``source_count`` is how
    # much of the group the reader could open at all, scope or no scope, which is what
    # the prompt is told: a briefing narrowed to one feed folds nothing and can still
    # say that four other outlets ran the story.
    story_id: int | None = None
    has_readable: bool = False
    folded_count: int = 0
    source_count: int = 0


# ── Period helpers ────────────────────────────────────────────────────────────

def _period_to_start_dt(period: str, tz_str: str | None) -> datetime:
    """Convert a named period to a UTC *start* datetime, respecting user timezone.

    Only a lower bound is returned — callers filter `>= start_dt` with no upper
    bound. So every period means "since X, up to now":
      today      → since today 00:00
      yesterday  → since yesterday 00:00 (intentionally includes today so far;
                   the UI labels this "Yesterday+")
      7days      → rolling last 7 days
    """
    try:
        tz = ZoneInfo(tz_str or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        return today.astimezone(timezone.utc)
    elif period == "yesterday":
        return (today - timedelta(days=1)).astimezone(timezone.utc)
    else:  # 7days
        return (today - timedelta(days=7)).astimezone(timezone.utc)


# ── Story mode ────────────────────────────────────────────────────────────────

def resolve_story_mode(settings) -> tuple[bool, bool]:
    """(fold the coverage together, leave out what was kept out of unread).

    One function because three callers have to agree: the estimate above the form, the
    digest the button builds, and the scheduled briefing. Two of them counting a
    different set than the third is the failure ``_catchup_stmt`` exists to prevent, and
    it has happened before on the reading side, where two of three callers forgot to
    pass a filter into the story scope.

    Both answers come from ``UserSettings.story_dedup``, the setting that already says
    what to do about repeated coverage in the list. ``None`` settings means an account
    that has none yet, which behaves as it did before any of this: nothing folds and
    nothing is left out.
    """
    if settings is None:
        return False, False
    mode = settings.story_dedup or DEDUP_OFF
    return mode != DEDUP_OFF, mode == DEDUP_SUPPRESS


# ── Scope helpers ─────────────────────────────────────────────────────────────

async def validate_scope(user_id: int, scope_include: str | None, db: AsyncSession) -> None:
    """Raise ValueError if scope_include contains items not belonging to the user."""
    from app.services.filter_service import _validate_scope_list  # noqa: PLC0415

    if not scope_include:
        return
    try:
        items = json.loads(scope_include)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("Invalid scope_include JSON")
    await _validate_scope_list(user_id, items, db)


# ── Snippet helper ────────────────────────────────────────────────────────────

def _snippet(article: CatchupArticle) -> str:
    """Return up to 150 chars of normalized text: ai_summary → readable_content → content."""
    if article.ai_summary:
        return strip_html(article.ai_summary)[:200]
    if article.readable_content:
        return strip_html(article.readable_content)[:150]
    if article.content:
        return strip_html(article.content)[:150]
    return ""


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _catchup_stmt(
    selection,
    user_id: int,
    tz_str: str | None,
    period: str,
    scope_include: str | None,
    filter_status: str,
    label_filter: str | None,
    filter_score_min: float | None,
    exclude_hidden: bool = False,
):
    """Build the catchup query over `selection` with all filters applied.

    Shared by fetch_catchup_articles and count_catchup_articles so the estimate
    shown in the UI can never be counted over a different set than the digest
    is built from.

    ``exclude_hidden`` leaves out what suppression kept out of the unread list. Those
    articles are read with no time spent on them, so they pass every other filter here,
    including "not opened", and a briefing would hand back exactly what the reader was
    spared. It keys on ``hidden_at`` rather than ``suppressed_at``: the latter is
    written by four different paths (URL dedup, a filter, finishing a story, similarity)
    and any human read clears it, while hidden_at is written only when an article was
    taken away and is never cleared.
    """
    start_dt = _period_to_start_dt(period, tz_str)
    feed_ids, folder_ids = parse_scope_tokens(scope_include)

    stmt = (
        select(*selection)
        # Explicit, because a count() selection carries no entity to join from.
        .select_from(Article)
        .join(Feed, Article.feed_id == Feed.id)
        .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user_id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id),
        )
        .where(
            func.coalesce(Article.published_at, Article.fetched_at) >= start_dt,
            # Exclude retention-trimmed stubs (body stripped) — same as the reader
            # listing; otherwise digests/counts include articles the user can't see.
            Article.trimmed_at.is_(None),
        )
    )

    # Scope filter
    if feed_ids or folder_ids:
        clauses = []
        if feed_ids:
            clauses.append(Article.feed_id.in_(feed_ids))
        if folder_ids:
            for fid in folder_ids:
                if fid == 0:
                    clauses.append(UserFeed.folder_id.is_(None))
                else:
                    clauses.append(UserFeed.folder_id == fid)
        stmt = stmt.where(or_(*clauses))

    # Status filter
    if filter_status == "not_opened":
        stmt = stmt.where(
            func.coalesce(UserArticleState.dwell_seconds, 0) == 0
        )

    # Label filter (same JSON shape as search): "any" = has at least one label,
    # otherwise articles carrying at least one of the selected labels.
    if label_filter:
        any_label, lf_ids = parse_label_tokens(label_filter)
        cond = (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user_id)
        if any_label:
            stmt = stmt.where(exists(select(ArticleLabel.article_id).where(cond)))
        elif lf_ids:
            stmt = stmt.where(
                exists(
                    select(ArticleLabel.article_id).where(
                        cond & ArticleLabel.label_id.in_(lf_ids)
                    )
                )
            )

    # Score filter
    if filter_score_min is not None:
        stmt = stmt.where(UserArticleState.ai_score >= filter_score_min)

    # Kept out of the unread list as a repeat of something already read. The join is an
    # outer one, so an article with no state row at all has a NULL here and stays.
    if exclude_hidden:
        stmt = stmt.where(UserArticleState.hidden_at.is_(None))

    return stmt


async def fetch_catchup_articles(
    user_id: int,
    tz_str: str | None,
    db: AsyncSession,
    period: str,
    scope_include: str | None,
    filter_status: str,
    label_filter: str | None,
    filter_score_min: float | None,
    exclude_hidden: bool = False,
) -> list[CatchupArticle]:
    """Fetch articles matching the given catchup parameters."""
    # Lightweight projection: bodies (content / readable_content / ai_summary) are
    # NOT selected here — they're only needed to build snippets for the <=limit
    # articles that survive sampling, and only when include_snippet is on. Pulling
    # full bodies for the whole period window would transfer megabytes generate
    # mostly discards. populate_snippet_sources loads them for the sampled subset.
    stmt = _catchup_stmt(
        (
            Article.id,
            Article.title,
            Feed.title.label("feed_title"),
            Article.published_at,
            Article.fetched_at,
            UserFeed.folder_id,
            UserArticleState.ai_score,
            Article.story_id,
            # Whether there is an extracted body, not how long it is: length() would
            # detoast every body in the window, which is the cost this projection is
            # built to avoid. fold_stories uses it to break a tie, because the first
            # report of an event is often the short wire piece extraction failed on.
            Article.readable_content.isnot(None).label("has_readable"),
        ),
        user_id=user_id, tz_str=tz_str, period=period,
        scope_include=scope_include, filter_status=filter_status,
        label_filter=label_filter, filter_score_min=filter_score_min,
        exclude_hidden=exclude_hidden,
    )

    rows = await db.execute(stmt)
    return [
        CatchupArticle(
            id=r.id,
            title=r.title,
            feed_title=r.feed_title,
            published_at=r.published_at,
            fetched_at=r.fetched_at,
            folder_id=r.folder_id,
            ai_score=r.ai_score,
            ai_summary=None,
            readable_content=None,
            content=None,
            story_id=r.story_id,
            has_readable=bool(r.has_readable),
        )
        for r in rows
    ]


async def count_catchup_articles(
    user_id: int,
    tz_str: str | None,
    db: AsyncSession,
    period: str,
    scope_include: str | None,
    filter_status: str,
    label_filter: str | None,
    filter_score_min: float | None,
    collapsing: bool = False,
    exclude_hidden: bool = False,
) -> int:
    """Count articles matching the given catchup parameters.

    The estimate route only needs the size of the selection, so it counts in the
    database instead of materializing every row in the period window.

    ``collapsing`` counts what the digest will actually send: one row per story, the
    same ``COUNT(DISTINCT coalesce(story_id, -id))`` the reader's badges use, which is
    the SQL twin of what fold_stories does in Python. The two have to agree, or the form
    promises a number of articles the digest then does not have; the integration test
    over both is what keeps them honest.
    """
    stmt = _catchup_stmt(
        (row_count(collapsing),),
        user_id=user_id, tz_str=tz_str, period=period,
        scope_include=scope_include, filter_status=filter_status,
        label_filter=label_filter, filter_score_min=filter_score_min,
        exclude_hidden=exclude_hidden,
    )
    return int((await db.execute(stmt)).scalar_one())


async def populate_snippet_sources(
    articles: list[CatchupArticle], user_id: int, db: AsyncSession
) -> None:
    """Load ai_summary / readable_content / content onto the given (already
    sampled) articles so build_articles_meta can produce snippets.

    Called after apply_catchup_limit so full bodies are fetched only for the
    <=limit articles that end up in the digest, not every article in the window.
    Mutates the passed CatchupArticle instances in place.
    """
    if not articles:
        return
    ids = [a.id for a in articles]
    rows = (await db.execute(
        select(
            Article.id,
            Article.readable_content,
            Article.content,
            UserArticleState.ai_summary,
        )
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user_id),
        )
        .where(Article.id.in_(ids))
    )).all()
    by_id = {r.id: r for r in rows}
    for a in articles:
        r = by_id.get(a.id)
        if r is not None:
            a.ai_summary = r.ai_summary
            a.readable_content = r.readable_content
            a.content = r.content


# ── Story folding ─────────────────────────────────────────────────────────────

def fold_stories(articles: list[CatchupArticle]) -> list[CatchupArticle]:
    """Keep one article per story and record how many it now stands for.

    Deliberately a second folding rule beside ``story_service.collapse_page``, not a
    parameter on it. The list keeps whichever member comes first in the reader's own
    ordering, because that is the row they were going to look at; a digest has no such
    ordering and samples by score, so the two answers have nothing in common but the
    word. Both are named in each other's docstrings so neither can be changed alone.

    The representative is the highest scoring member, then the one with an extracted
    body, then the oldest. Score first because the sampling that follows is built on it
    and handing it a worse article than the group has would cost the group its place.
    The body next because the snippet is most of what the model sees and the first
    account of an event is often the wire piece extraction failed on. Oldest last: it is
    the first report rather than a reaction to it, it carries the day the news belongs
    to, and it does not change when more coverage arrives, so two runs over the same
    period agree.

    Order is preserved, since the caller's next step reads the list as the query
    returned it.
    """
    groups: dict[int, list[CatchupArticle]] = {}
    for a in articles:
        if a.story_id is None:
            continue
        groups.setdefault(a.story_id, []).append(a)

    keep: dict[int, CatchupArticle] = {}
    for story_id, members in groups.items():
        if len(members) == 1:
            continue
        best = min(members, key=lambda a: (
            -(a.ai_score if a.ai_score is not None else -1),
            0 if a.has_readable else 1,
            _ts(a),
        ))
        best.folded_count = len(members) - 1
        keep[story_id] = best

    return [
        a for a in articles
        if a.story_id is None or a.story_id not in keep or keep[a.story_id] is a
    ]


async def annotate_sources(
    articles: list[CatchupArticle], user_id: int, db: AsyncSession
) -> None:
    """Fill in ``source_count``: the rest of the group this reader could open.

    The whole group, not the part that got into this digest. A briefing scoped to one
    feed folds nothing, because grouping only ever pairs articles across feeds, and the
    number still says the thing worth saying: other outlets ran this too. The list drew
    the same distinction first, between ``story_others`` and ``story_total``.

    Behind the same access gate as the reader's footer. The grouping is global, so a
    group routinely holds articles from feeds this reader does not take, and counting
    those would promise coverage they cannot open. Trimmed articles are left out for the
    reason the list leaves them out: retention stripped them to a stub.

    One query for the page, run after sampling, so it asks about the handful of rows
    that made it rather than the whole window.
    """
    story_ids = {a.story_id for a in articles if a.story_id is not None}
    if not story_ids:
        return

    rows = (await db.execute(
        add_article_access_joins(
            select(Article.story_id, func.count(Article.id)), user_id
        )
        .where(
            Article.story_id.in_(story_ids),
            Article.trimmed_at.is_(None),
            article_access_predicate(),
        )
        .group_by(Article.story_id)
    )).all()
    totals = {r[0]: r[1] for r in rows}

    for a in articles:
        if a.story_id is None:
            continue
        # The article itself is one of the members it just counted. It is always in
        # there: it came out of the digest query, which is behind the same access gate.
        a.source_count = max(totals.get(a.story_id, 0) - 1, 0)


# ── Sampling ──────────────────────────────────────────────────────────────────

def _ts(article: CatchupArticle) -> float:
    """Return Unix timestamp for sorting (published_at fallback fetched_at)."""
    dt = article.published_at or article.fetched_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _date_key(article: CatchupArticle) -> str:
    """Return YYYY-MM-DD string in UTC for grouping."""
    dt = article.published_at or article.fetched_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def apply_catchup_limit(
    articles: list[CatchupArticle],
    limit: int,
    scoring_available: bool,
) -> list[CatchupArticle]:
    """Hybrid per-day + score-based sampling.

    Pass 1 (coverage): take base_quota articles from each day, sorted score DESC → date DESC.
    Pass 2 (quality):  fill remaining slots from the leftover pool, sorted score DESC → date DESC.
    """
    if len(articles) <= limit:
        return articles

    # Group by day
    by_day: dict[str, list[CatchupArticle]] = {}
    for a in articles:
        key = _date_key(a)
        by_day.setdefault(key, []).append(a)

    ratio = _CATCHUP_COVERAGE_RATIO if scoring_available else _CATCHUP_COVERAGE_RATIO_NO_SCORE
    base_quota = max(1, floor(limit * ratio / len(by_day)))

    def score_sort_key(a: CatchupArticle):
        # folded_count is the weight folding took away and gives back. Before it, a
        # story covered five times had five chances of being picked, one per member and
        # one per day it spanned; folded into a single row it has one. Only a tie
        # breaker, so it never moves a row past a better scoring one: how much coverage
        # a story got says something about it, but not more than the score does.
        return (
            -(a.ai_score if a.ai_score is not None else -1),
            -a.folded_count,
            -_ts(a),
        )

    taken_ids: set[int] = set()
    result: list[CatchupArticle] = []

    # Pass 1 — per-day coverage
    for day_articles in by_day.values():
        top = sorted(day_articles, key=score_sort_key)[:base_quota]
        result.extend(top)
        taken_ids.update(a.id for a in top)

    # Pass 2 — score-based fill (spillover)
    remaining = limit - len(result)
    if remaining > 0:
        pool = sorted(
            (a for day in by_day.values() for a in day if a.id not in taken_ids),
            key=score_sort_key,
        )
        result.extend(pool[:remaining])

    # Pass 1 takes >=1 article per active day; when there are more active days
    # than `limit` (small limit over a wide window) that alone can exceed limit,
    # so cap the final result.
    return sorted(result, key=_ts, reverse=True)[:limit]


# ── Metadata builder ──────────────────────────────────────────────────────────

def build_articles_meta(
    articles: list[CatchupArticle],
    include_snippet: bool,
) -> list[dict]:
    """Convert articles to metadata dicts for the AI prompt."""
    out = []
    for a in articles:
        dt = a.published_at or a.fetched_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        entry: dict = {
            "feed": a.feed_title,
            "title": a.title,
            "date": dt.strftime("%Y-%m-%d"),
        }
        if a.source_count:
            entry["sources"] = a.source_count
        if include_snippet:
            entry["snippet"] = _snippet(a)
        out.append(entry)
    return out


# ── Cost estimation ───────────────────────────────────────────────────────────

def estimate_catchup_tokens(article_limit: int, include_snippet: bool) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) estimate for cost calculation."""
    tokens_per_article = 55 if include_snippet else 20
    input_tokens = article_limit * tokens_per_article + 300
    output_tokens = 800
    return input_tokens, output_tokens
