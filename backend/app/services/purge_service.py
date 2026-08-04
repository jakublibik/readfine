"""Article purge service: tiered age-based retention.

Lifecycle of an article older than T1 (= default_purge_after_days):
  - starred / archived by any user → kept FULL forever (never trimmed/deleted)
  - engaged by any user (read / opened / ever-starred) → TRIMMED to a profile snippet
    (body stripped, trimmed_at stamped, share revoked) and kept as a hidden stub until
    T2 (= PROFILE_MAX_WINDOW_DAYS); then DELETED
  - otherwise → DELETED immediately

T1 measures article age (fetched_at); T2 measures uas.created_at (mirrors the profile
lookback window). Invariant: admin T1 max (120) < T2 (180).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)

_SNIPPET_CHARS = 300  # profile snippet length kept on trim


# ── retention predicates (correlated EXISTS on the current Article) ───────────

def _fully_protected_exists():
    """True when some user keeps this article FULL forever (starred, archived or saved).
    Never trimmed nor age-deleted.

    Saved-by-URL articles are kept indefinitely on purpose, with no TTL and no cap:
    pasting a URL by hand is the most explicit thing a reader can do, so Saved must not
    be the one place where content quietly expires. Like starred/archived this is
    any-user semantics, so one person saving an article pins the row for the instance.
    Un-saving re-exposes it to purge, which is the intended way out."""
    return (
        select(UserArticleState.article_id)
        .where(
            UserArticleState.article_id == Article.id,
            (UserArticleState.is_starred == True)  # noqa: E712
            | (UserArticleState.is_archived == True)  # noqa: E712
            | (UserArticleState.saved_at.is_not(None)),
        )
        .exists()
    )


def _engaged_exists():
    """True when some user genuinely engaged with this article.

    Engagement = actually read (dwell >= 30s, the stats 'read' threshold),
    link opened, or ever starred. Engaged articles survive the age DELETE —
    instead they are trimmed and kept as profile-signal stubs until T2.

    NOTE: the `is_read` flag is deliberately NOT an engagement signal. It is set
    by scroll-based batch mark-read, the filter `mark_read` action, and
    mark-all-read — i.e. mostly "dismissed without reading". Treating is_read as
    engagement would bloat retention with articles the user never actually read
    (and, absent other signals, often signals the opposite of interest)."""
    return (
        select(UserArticleState.article_id)
        .where(
            UserArticleState.article_id == Article.id,
            (UserArticleState.dwell_seconds >= 30)
            | (UserArticleState.link_opened == True)  # noqa: E712
            | (UserArticleState.ever_starred == True),  # noqa: E712
        )
        .exists()
    )


# ── trim helper ───────────────────────────────────────────────────────────────

async def _trim_engaged(
    db: AsyncSession, *, feed_id: int | None, orphan: bool, cutoff: datetime, now: datetime
) -> int:
    """Trim engaged-but-unprotected articles older than cutoff to a profile snippet.

    Strips the large body (content / readable_content) down to the first
    _SNIPPET_CHARS normalized characters; keeps per-user ai_summary/ai_context;
    revokes share tokens; stamps trimmed_at. Idempotent via trimmed_at IS NULL.
    """
    from app.utils.text import strip_html

    feed_cond = Article.feed_id.is_(None) if orphan else (Article.feed_id == feed_id)
    rows = (await db.execute(
        select(Article.id, Article.content, Article.readable_content).where(
            feed_cond,
            Article.fetched_at < cutoff,
            Article.trimmed_at.is_(None),
            _engaged_exists(),
            ~_fully_protected_exists(),
        )
    )).all()
    if not rows:
        return 0

    updates: list[dict] = []
    for aid, content, readable in rows:
        src = readable if readable else content
        snippet = strip_html(src)[:_SNIPPET_CHARS] if src else None
        if readable is not None:
            # keep the shared snippet in readable_content, drop raw content
            updates.append({"id": aid, "content": None, "readable_content": snippet, "trimmed_at": now})
        elif content is not None:
            updates.append({"id": aid, "content": snippet, "readable_content": None, "trimmed_at": now})
        else:
            updates.append({"id": aid, "content": None, "readable_content": None, "trimmed_at": now})

    await db.execute(update(Article), updates)
    ids = [u["id"] for u in updates]
    # share is bound to the article's lifetime — revoke on trim (old link → clean 404)
    await db.execute(
        update(UserArticleState)
        .where(UserArticleState.article_id.in_(ids), UserArticleState.share_token.isnot(None))
        .values(share_token=None)
    )
    return len(ids)


# ── main purge job ────────────────────────────────────────────────────────────

async def purge_old_articles(db: AsyncSession) -> int:
    """
    Apply tiered age-based retention. Returns total number of deleted articles.

    NULL global default_purge_after_days disables the age pass globally (per-feed
    override still applies). NULL default_purge_keep_count disables the count pass
    globally (this is the new default; per-feed override still applies).
    """
    from app.models.feed import UserFeed
    from app.services.ai_service import PROFILE_MAX_WINDOW_DAYS

    result = await db.execute(
        select(AppSettings.default_purge_after_days, AppSettings.default_purge_keep_count)
        .where(AppSettings.id == 1)
    )
    row = result.one_or_none()
    global_days: int | None = row[0] if row else None
    global_count: int | None = row[1] if row else None

    now = datetime.now(timezone.utc)
    total_deleted = 0
    total_trimmed = 0

    # ── Pass 1: age-based — DELETE unengaged, TRIM engaged ────────────────────
    # Per-feed effective_days = MAX(coalesce(feed override, global)) across subscribers
    # (most generous — only act once everyone is past their horizon).
    feed_days_result = await db.execute(
        select(
            UserFeed.feed_id,
            func.max(func.coalesce(UserFeed.purge_after_days, global_days)).label("effective_days"),
        )
        .group_by(UserFeed.feed_id)
    )
    age_deleted = 0
    for r in feed_days_result:
        if r.effective_days is None:
            continue  # disabled for this feed
        cutoff = now - timedelta(days=r.effective_days)
        res = await db.execute(
            delete(Article).where(
                Article.feed_id == r.feed_id,
                Article.fetched_at < cutoff,
                ~_fully_protected_exists(),
                ~_engaged_exists(),
            )
        )
        age_deleted += res.rowcount or 0
        total_trimmed += await _trim_engaged(db, feed_id=r.feed_id, orphan=False, cutoff=cutoff, now=now)

    # Orphaned articles (feed deleted) — use global default only if set
    if global_days is not None:
        cutoff = now - timedelta(days=global_days)
        res = await db.execute(
            delete(Article).where(
                Article.feed_id.is_(None),
                Article.fetched_at < cutoff,
                ~_fully_protected_exists(),
                ~_engaged_exists(),
            )
        )
        age_deleted += res.rowcount or 0
        total_trimmed += await _trim_engaged(db, feed_id=None, orphan=True, cutoff=cutoff, now=now)

    total_deleted += age_deleted

    # ── Pass 2: count-based (per-feed override; global NULL by default) ────────
    # Policy: keep_count is a hard cap for COLD (unengaged, unprotected) articles
    # only — excess cold articles are deleted below. Engaged excess is NOT trimmed
    # here; engaged articles are governed by the age pass (trim) + T2 (delete), so a
    # feed may exceed keep_count by its engaged articles until they age out. This is
    # intentional over-retention (never deletes more than expected), not data loss.
    # Reachable only via the per-feed API (PATCH /api/v1/feeds purge_keep_count);
    # there is no UI to set the global default_purge_keep_count.
    feed_counts_result = await db.execute(
        select(
            UserFeed.feed_id,
            func.max(func.coalesce(UserFeed.purge_keep_count, global_count)).label("effective_count"),
        )
        .group_by(UserFeed.feed_id)
    )
    feed_counts: dict[int, int] = {
        r.feed_id: r.effective_count
        for r in feed_counts_result
        if r.effective_count is not None
    }

    count_deleted = 0
    if feed_counts:
        ranked = (
            select(
                Article.id,
                Article.feed_id,
                func.row_number()
                .over(
                    partition_by=Article.feed_id,
                    order_by=func.coalesce(Article.published_at, Article.fetched_at).desc(),
                )
                .label("rn"),
            )
            .where(Article.feed_id.in_(feed_counts.keys()))
            .subquery()
        )
        from sqlalchemy import case, literal
        keep_case = case(
            {feed_id: literal(keep) for feed_id, keep in feed_counts.items()},
            value=ranked.c.feed_id,
        )
        excess_result = await db.execute(
            select(ranked.c.id).where(ranked.c.rn > keep_case)
        )
        excess_ids = [r[0] for r in excess_result]

        if excess_ids:
            res = await db.execute(
                delete(Article).where(
                    Article.id.in_(excess_ids),
                    ~_fully_protected_exists(),
                    ~_engaged_exists(),
                )
            )
            count_deleted += res.rowcount or 0

    total_deleted += count_deleted

    # ── Pass 3: T2 — delete trimmed stubs past the profile window ─────────────
    # Drop a stub once no engaged state references it within PROFILE_MAX_WINDOW_DAYS
    # (mirrors the profile lookback, keyed on uas.created_at).
    cutoff_t2 = now - timedelta(days=PROFILE_MAX_WINDOW_DAYS)
    recent_state = (
        select(UserArticleState.article_id)
        .where(
            UserArticleState.article_id == Article.id,
            UserArticleState.created_at >= cutoff_t2,
        )
        .exists()
    )
    res = await db.execute(
        delete(Article).where(
            Article.trimmed_at.isnot(None),
            ~recent_state,
            ~_fully_protected_exists(),
        )
    )
    t2_deleted = res.rowcount or 0
    total_deleted += t2_deleted

    await db.commit()
    logger.info(
        "Purge: deleted %d (age %d, count %d, T2 %d), trimmed %d",
        total_deleted, age_deleted, count_deleted, t2_deleted, total_trimmed,
    )
    return total_deleted
