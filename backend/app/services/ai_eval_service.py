"""Offline evaluation of AI scoring: does `ai_score` predict real engagement?

Reads only existing columns on `user_article_states` (`ai_score` + engagement) —
no new logging or tables. `ai_score` is written once at scoring time; engagement
accrues on the same row afterwards, so scores from before/after a profile change
can be compared by limiting the time window.

Engaged label = `user_starred OR dwell_seconds >= 60 OR link_opened`. `is_read`
is deliberately excluded: it is set even for articles the user never saw
(mark-all-read, auto-read on scroll), so it carries no signal — consistent with
the profile-generation groups.

Caveats, in descending order of how much they move the number:

1. **A filter with an `ai_score` condition breaks the measurement.** An action of
   `mark_read` or `archive` on a low-score rule keeps those articles out of the
   unread list, so they cannot be engaged with and their label is an effect of
   the score being measured, not evidence about it. Measured on the production
   instance in 2026-08: a `< 30 -> mark_read` rule covered 49% of scored
   articles with an engagement rate of 0.09% there, and it carried the reported
   AUC from 0.69 to 0.85. **AUC is therefore an upper bound whenever such a
   filter is active**, not the lower bound this docstring used to claim. There is
   no way to correct for it here; the fix is to read the number knowing which
   filters ran, or to drop the affected band by hand.
2. The window is cut at the purge horizon (see `effective_window`), because past
   it only starred/archived survivors remain and their engagement rate approaches
   100%.
3. `created_at` on the state row is not the time the score was written:
   retroactively applying a filter scores older articles against a newer profile,
   and a state row can predate scoring when the user touched the article first.
   Articles therefore land in the window by arrival, not by scoring regime. The
   offline test in `scripts/embedding_eval/` splits by `article_ai_jobs.
   processed_at` instead, which is why its segments and this window differ.
4. With `user_id=None` the aggregate pools users with different base rates, and
   any single window pools profile regimes the same way.
5. It is observational, not randomized, and un-engaged may just mean unseen.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Keep the window clear of the purge horizon rather than ending exactly on it:
# purge runs daily and articles land in the sample by arrival, not by score.
RETENTION_MARGIN_DAYS = 5


# ── pure computation (unit-testable) ─────────────────────────────────────────

def compute_auc(pairs: list[tuple[float, bool]]) -> float | None:
    """Rank-based AUC (Mann–Whitney) with tie handling.

    Probability that a random engaged article scores higher than a random
    non-engaged one. Returns None when one class is absent.
    """
    n_pos = sum(1 for _, e in pairs if e)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    ordered = sorted(pairs, key=lambda p: p[0])
    ranks = [0.0] * len(ordered)
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0  # 1-based ranks i+1..j, averaged for ties
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j
    sum_ranks_pos = sum(r for r, (_, e) in zip(ranks, ordered) if e)
    return (sum_ranks_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def calibration_buckets(pairs: list[tuple[float, bool]], n_buckets: int = 5) -> list[dict]:
    """Group by score bucket; report engagement rate per bucket (should rise)."""
    buckets = [
        {"lo": b / n_buckets, "hi": (b + 1) / n_buckets, "count": 0, "engaged": 0, "rate": None}
        for b in range(n_buckets)
    ]
    for score, engaged in pairs:
        idx = min(int(score * n_buckets), n_buckets - 1)
        buckets[idx]["count"] += 1
        if engaged:
            buckets[idx]["engaged"] += 1
    for b in buckets:
        if b["count"]:
            b["rate"] = b["engaged"] / b["count"]
    return buckets


def effective_window(days: int, purge_after_days: int | None) -> tuple[int, dict]:
    """Shorten the requested window so it stays inside the un-purged zone.

    Past T1 (`default_purge_after_days`) purge deletes un-engaged articles and
    keeps starred/archived ones, so an older window holds mostly survivors and
    every metric computed on it looks better than the scoring was. Returns the
    window to use plus what happened, so the UI can say why it differs from the
    button the admin pressed.
    """
    if not purge_after_days:
        return days, {"clamped": False, "requested_days": days,
                      "effective_days": days, "purge_after_days": None}
    limit = max(purge_after_days - RETENTION_MARGIN_DAYS, 1)
    return min(days, limit), {
        "clamped": days > limit,
        "requested_days": days,
        "effective_days": min(days, limit),
        "purge_after_days": purge_after_days,
        "margin_days": RETENTION_MARGIN_DAYS,
    }


def window_presets(purge_after_days: int | None) -> list[int]:
    """Window buttons worth offering, given how long articles survive.

    Fixed presets up to a year made three of the buttons do the same thing once
    the window started being clamped: with 60-day retention, 90d, 180d and 365d
    all come back as 55d. Offer only what differs, ending on the longest honest
    window.
    """
    limit = (max(purge_after_days - RETENTION_MARGIN_DAYS, 1) if purge_after_days
             else 365)
    presets = [d for d in (7, 14, 30, 60, 90, 180, 365) if d < limit]
    return presets + [limit]


async def exposure_floor(db: AsyncSession, user_id: int) -> dict | None:
    """The score below which this user's own filters hide articles from them.

    A rule like `ai_score < 30 -> mark_read` keeps those articles out of the
    unread list, so nobody can engage with them and their engagement label
    becomes an effect of the score being measured. The AUC then partly grades the
    score against ground truth it wrote itself.

    Returns the widest such band (the highest threshold, since a wider band
    suppresses more) or None. This says a filter *can* have suppressed those
    articles, not that it did: the rule may sit behind an AND with other
    conditions or be scoped to a few feeds, both of which narrow what it caught.
    Treating it as an upper bound on the damage is the safe direction.
    """
    rows = (await db.execute(text("""
        SELECT f.name, c.value, a.action_type
        FROM filters f
        JOIN filter_conditions c ON c.filter_id = f.id
        JOIN filter_actions a ON a.filter_id = f.id
        WHERE f.user_id = :uid AND f.is_active
          AND c.field = 'ai_score' AND c.operator = 'lt'
          AND a.action_type IN ('mark_read', 'archive')
    """), {"uid": user_id})).all()
    best = None
    for name, value, action_type in rows:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            continue  # the column is text; a malformed rule is not our problem here
        if best is None or threshold > best["threshold"]:
            best = {"threshold": threshold, "floor": threshold / 100.0,
                    "filter_name": name, "action": action_type}
    return best


async def exposure_filter_users(db: AsyncSession) -> int:
    """How many users have such a rule — for the aggregate view, which cannot
    correct for it because each user's threshold is their own."""
    return await db.scalar(text("""
        SELECT count(DISTINCT f.user_id)
        FROM filters f
        JOIN filter_conditions c ON c.filter_id = f.id
        JOIN filter_actions a ON a.filter_id = f.id
        WHERE f.is_active AND c.field = 'ai_score' AND c.operator = 'lt'
          AND a.action_type IN ('mark_read', 'archive')
    """)) or 0


def score_histogram(scores: list[float], n_bins: int = 20) -> list[dict]:
    """Distribution of scores in 0.05-wide bins — reveals LLM self-quantization."""
    bins = [0] * n_bins
    for s in scores:
        bins[min(int(s * n_bins), n_bins - 1)] += 1
    return [
        {"lo": i / n_bins, "hi": (i + 1) / n_bins, "count": c}
        for i, c in enumerate(bins)
    ]


# ── DB-backed report ──────────────────────────────────────────────────────────

async def get_scoring_eval(db: AsyncSession, days: int = 90, user_id: int | None = None) -> dict:
    """Scoring-quality metrics for the last `days`, all users or a single one.

    The window is clamped to the purge horizon (`effective_window`), so asking
    for more days than retention keeps returns the longest honest window instead
    of a prettier number computed on survivors. Read the module docstring before
    trusting the AUC: an active `ai_score` filter with a `mark_read` action makes
    it an upper bound.

    Note: with `user_id=None` the aggregate mixes per-user scoring regimes (each
    user has their own profile), which can mask per-user variation — for tuning a
    specific profile, pass `user_id`.
    """
    purge_after_days = await db.scalar(text(
        "SELECT default_purge_after_days FROM app_settings WHERE id = 1"))
    days, retention = effective_window(days, purge_after_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    params = {"cutoff": cutoff}
    user_clause = ""
    if user_id is not None:
        user_clause = " AND user_id = :uid"
        params["uid"] = user_id
    rows = await db.execute(text(f"""
        SELECT ai_score,
               (user_starred OR dwell_seconds >= 60 OR link_opened) AS engaged
        FROM user_article_states
        WHERE ai_score IS NOT NULL
          AND created_at >= :cutoff{user_clause}
    """), params)
    pairs = [(float(s), bool(e)) for s, e in rows]
    n = len(pairs)
    engaged_total = sum(1 for _, e in pairs if e)

    # Second reading for a user whose own filter hides the bottom of the range.
    # Deliberately not called a correction: dropping the band cuts the LLM's
    # score range on its own scale, which pushes its AUC down, while keeping the
    # band leaves a tail of manufactured negatives, which pushes it up. The pair
    # brackets the answer; neither end is it.
    exposure = None
    if user_id is not None:
        floor_info = await exposure_floor(db, user_id)
        if floor_info and pairs:
            kept = [p for p in pairs if p[0] >= floor_info["floor"]]
            dropped = n - len(kept)
            if kept and dropped:
                kept_engaged = sum(1 for _, e in kept if e)
                exposure = floor_info | {
                    "n": len(kept),
                    "dropped": dropped,
                    "dropped_share": dropped / n,
                    "engaged_total": kept_engaged,
                    "engaged_rate": kept_engaged / len(kept),
                    "dropped_engaged_rate": (engaged_total - kept_engaged) / dropped,
                    "auc": compute_auc(kept),
                }
    else:
        affected = await exposure_filter_users(db)
        if affected:
            # No single threshold fits the aggregate, so say who it touches and
            # leave the number uncorrected rather than inventing a common floor.
            exposure = {"aggregate_only": True, "users_affected": affected}

    return {
        "days": days,
        "retention": retention,
        "presets": window_presets(purge_after_days),
        "exposure": exposure,
        "user_id": user_id,
        "n": n,
        "engaged_total": engaged_total,
        "engaged_rate": (engaged_total / n) if n else None,
        "auc": compute_auc(pairs),
        "calibration": calibration_buckets(pairs),
        "histogram": score_histogram([s for s, _ in pairs]),
    }
