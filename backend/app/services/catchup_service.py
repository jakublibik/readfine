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
from app.services.scope_tokens import parse_label_tokens, parse_scope_tokens
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

async def fetch_catchup_articles(
    user_id: int,
    tz_str: str | None,
    db: AsyncSession,
    period: str,
    scope_include: str | None,
    filter_status: str,
    label_filter: str | None,
    filter_score_min: float | None,
) -> list[CatchupArticle]:
    """Fetch articles matching the given catchup parameters."""
    start_dt = _period_to_start_dt(period, tz_str)
    feed_ids, folder_ids = parse_scope_tokens(scope_include)

    # Lightweight projection: bodies (content / readable_content / ai_summary) are
    # NOT selected here — they're only needed to build snippets for the <=limit
    # articles that survive sampling, and only when include_snippet is on. Pulling
    # full bodies for the whole period window would transfer megabytes the count /
    # cost routes never read and generate mostly discards. populate_snippet_sources
    # loads them for the sampled subset.
    stmt = (
        select(
            Article.id,
            Article.title,
            Feed.title.label("feed_title"),
            Article.published_at,
            Article.fetched_at,
            UserFeed.folder_id,
            UserArticleState.ai_score,
        )
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
        )
        for r in rows
    ]


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
        return (-(a.ai_score if a.ai_score is not None else -1), -_ts(a))

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
