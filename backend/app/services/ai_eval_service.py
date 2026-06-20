"""Offline evaluation of AI scoring: does `ai_score` predict real engagement?

Reads only existing columns on `user_article_states` (`ai_score` + engagement) —
no new logging or tables. `ai_score` is written once at scoring time; engagement
accrues on the same row afterwards, so scores from before/after a profile change
can be compared by limiting the time window.

Engaged label = `user_starred OR dwell_seconds >= 60 OR link_opened`. `is_read`
is deliberately excluded: it is set even for articles the user never saw
(mark-all-read, auto-read on scroll), so it carries no signal — consistent with
the profile-generation groups.

Caveats: measurement is observational (not randomized) and the engaged label is
contaminated by exposure bias (an un-engaged article may simply never have been
shown/seen). Treat AUC as a lower bound, not a precise quality figure.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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

    Note: with `user_id=None` the aggregate mixes per-user scoring regimes (each
    user has their own profile), which can mask per-user variation — for tuning a
    specific profile, pass `user_id`.
    """
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
    return {
        "days": days,
        "user_id": user_id,
        "n": n,
        "engaged_total": engaged_total,
        "engaged_rate": (engaged_total / n) if n else None,
        "auc": compute_auc(pairs),
        "calibration": calibration_buckets(pairs),
        "histogram": score_histogram([s for s, _ in pairs]),
    }
