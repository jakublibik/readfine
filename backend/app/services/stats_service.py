"""Statistics service — reading stats, feed quality, AI stats, label stats."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UserSettings


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FeedStatRow:
    user_feed_id: int
    feed_title: str
    total_articles: int
    read_pct: float        # dwell >= 30s / total
    labeled_pct: float
    starred_pct: float     # ever_starred / total
    avg_dwell: float | None  # seconds, only articles with dwell > 0
    avg_ai_score: float | None
    signal: float          # 0-1 weighted score


@dataclass
class DailyRead:
    date: str   # YYYY-MM-DD
    count: int


@dataclass
class TopFeedDwell:
    title: str
    avg_dwell_seconds: float


@dataclass
class ReadingStats:
    streak: int
    backlog: int
    total_articles: int     # last 30d
    labeled_count: int
    read_count: int         # dwell >= 30s
    starred_count: int      # ever_starred
    per_day: list[DailyRead]
    active_hour: int | None      # 0-23, None if < 7 records
    active_day: int | None       # 0=Mon … 6=Sun, None if < 7 records
    avg_time_to_read_hours: float | None
    avg_dwell_seconds: float | None
    top_feeds_by_dwell: list[TopFeedDwell]


@dataclass
class AiCalibration:
    avg_score_starred: float | None
    avg_score_not_starred: float | None
    gap: int | None           # (avg_starred - avg_not_starred) * 100, rounded
    min_score_starred: int | None  # min AI score among starred articles * 100


@dataclass
class GemArticle:
    article_id: int
    title: str
    feed_title: str
    ai_score: float
    is_starred: bool = False


@dataclass
class AiStats:
    scoring_coverage_pct: float | None
    calibration: AiCalibration
    gems: list[GemArticle]   # high score, never opened
    wrong: list[GemArticle]  # low score, ever_starred


@dataclass
class LabelRow:
    label_id: int
    name: str
    color: str
    article_count: int
    star_rate: float
    read_rate: float   # dwell >= 30s


@dataclass
class LabelStats:
    label_coverage_pct: float
    labels: list[LabelRow]


@dataclass
class OperationCostRow:
    operation: str
    label: str
    slot: str          # "fast" or "quality"
    count: int
    input_tokens: int
    output_tokens: int
    est_cost: float | None
    trend_pct: float | None   # positive = up, negative = down, None = no prev data
    is_placeholder: bool = False
    row_type: str = "operation"  # "operation" | "separator" | "subtotal" | "total"


@dataclass
class AiCostStats:
    period_days: int
    operations: list[OperationCostRow]


# ── Feed stats (for Settings → Feeds stats toggle) ────────────────────────────

async def get_feed_stats(user_id: int, db: AsyncSession, days: int = 30) -> list[FeedStatRow]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        text("""
            SELECT
                uf.id AS user_feed_id,
                COALESCE(uf.custom_title, f.title) AS feed_title,
                COUNT(DISTINCT a.id) AS total_articles,
                COUNT(DISTINCT CASE WHEN uas.dwell_seconds >= 30 THEN a.id END) AS read_count,
                COUNT(DISTINCT CASE WHEN uas.ever_starred THEN a.id END) AS starred_count,
                COUNT(DISTINCT al_sub.article_id) AS labeled_count,
                COUNT(DISTINCT CASE WHEN uas.dwell_seconds > 0 THEN a.id END) AS opened_count,
                AVG(CASE WHEN uas.dwell_seconds > 0 THEN uas.dwell_seconds::float END) AS avg_dwell,
                AVG(uas.ai_score) AS avg_ai_score
            FROM user_feeds uf
            JOIN feeds f ON f.id = uf.feed_id
            LEFT JOIN articles a ON a.feed_id = uf.feed_id AND a.fetched_at >= :cutoff
            LEFT JOIN user_article_states uas
                ON uas.article_id = a.id AND uas.user_id = :uid
            LEFT JOIN (
                SELECT DISTINCT article_id FROM article_labels WHERE user_id = :uid
            ) al_sub ON al_sub.article_id = a.id
            WHERE uf.user_id = :uid
            GROUP BY uf.id, f.title, uf.custom_title
            ORDER BY LOWER(COALESCE(uf.custom_title, f.title))
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    rows = result.fetchall()

    stats = []
    for row in rows:
        total = int(row.total_articles or 0)
        read = int(row.read_count or 0)
        starred = int(row.starred_count or 0)
        labeled = int(row.labeled_count or 0)
        opened = int(row.opened_count or 0)

        starred_rate = starred / total if total > 0 else 0.0
        opened_rate = opened / total if total > 0 else 0.0
        labeled_rate = labeled / total if total > 0 else 0.0
        signal = starred_rate * 0.5 + opened_rate * 0.3 + labeled_rate * 0.2

        stats.append(FeedStatRow(
            user_feed_id=int(row.user_feed_id),
            feed_title=row.feed_title or "",
            total_articles=total,
            read_pct=round(read / total * 100, 1) if total > 0 else 0.0,
            labeled_pct=round(labeled / total * 100, 1) if total > 0 else 0.0,
            starred_pct=round(starred / total * 100, 1) if total > 0 else 0.0,
            avg_dwell=round(float(row.avg_dwell), 0) if row.avg_dwell else None,
            avg_ai_score=round(float(row.avg_ai_score), 2) if row.avg_ai_score is not None else None,
            signal=round(signal, 2),
        ))
    return stats


# ── Reading stats (for Settings → Stats) ─────────────────────────────────────

async def get_reading_stats(user_id: int, db: AsyncSession, days: int = 30) -> ReadingStats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_90d = datetime.now(timezone.utc) - timedelta(days=90)

    # Streak — consecutive days with dwell >= 30s, using read_at as timestamp
    streak_result = await db.execute(
        text("""
            WITH read_days AS (
                SELECT DISTINCT DATE(read_at AT TIME ZONE 'UTC') AS d
                FROM user_article_states
                WHERE user_id = :uid
                  AND read_at IS NOT NULL
                  AND dwell_seconds >= 30
            ),
            gaps AS (
                SELECT d,
                       (d - (ROW_NUMBER() OVER (ORDER BY d) || ' days')::INTERVAL)::date AS grp
                FROM read_days
            ),
            streaks AS (
                SELECT grp, COUNT(*) AS streak_len, MAX(d) AS last_day
                FROM gaps
                GROUP BY grp
            )
            SELECT COALESCE(streak_len, 0)
            FROM streaks
            WHERE last_day >= CURRENT_DATE - INTERVAL '1 day'
            ORDER BY last_day DESC
            LIMIT 1
        """),
        {"uid": user_id},
    )
    streak = int(streak_result.scalar() or 0)

    # Backlog — unread + has label + published within 90 days
    backlog_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT a.id)
            FROM articles a
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            JOIN article_labels al ON al.article_id = a.id AND al.user_id = :uid
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            WHERE (uas.is_read IS NULL OR uas.is_read = false)
              AND (a.published_at IS NULL OR a.published_at >= :cutoff_90d)
        """),
        {"uid": user_id, "cutoff_90d": cutoff_90d},
    )
    backlog = int(backlog_result.scalar() or 0)

    # Funnel counts (last 30d)
    funnel_result = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT a.id) AS total,
                COUNT(DISTINCT al_sub.article_id) AS labeled,
                COUNT(DISTINCT CASE WHEN uas.dwell_seconds >= 30 THEN a.id END) AS read,
                COUNT(DISTINCT CASE WHEN uas.ever_starred THEN a.id END) AS starred
            FROM articles a
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            LEFT JOIN (
                SELECT DISTINCT article_id FROM article_labels WHERE user_id = :uid
            ) al_sub ON al_sub.article_id = a.id
            WHERE a.fetched_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    f = funnel_result.one()
    total_articles = int(f.total or 0)
    labeled_count = int(f.labeled or 0)
    read_count = int(f.read or 0)
    starred_count = int(f.starred or 0)

    # Per-day reads (last 7 days, dwell >= 30s)
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    per_day_result = await db.execute(
        text("""
            SELECT
                DATE(read_at AT TIME ZONE 'UTC') AS d,
                COUNT(*) AS cnt
            FROM user_article_states
            WHERE user_id = :uid
              AND read_at IS NOT NULL
              AND dwell_seconds >= 30
              AND read_at >= :cutoff_7d
            GROUP BY DATE(read_at AT TIME ZONE 'UTC')
            ORDER BY d
        """),
        {"uid": user_id, "cutoff_7d": cutoff_7d},
    )
    per_day_map = {str(r.d): int(r.cnt) for r in per_day_result.fetchall()}
    today = date.today()
    per_day = [
        DailyRead(
            date=str(today - timedelta(days=6 - i)),
            count=per_day_map.get(str(today - timedelta(days=6 - i)), 0),
        )
        for i in range(7)
    ]

    # Active hour (0-23)
    hour_result = await db.execute(
        text("""
            SELECT EXTRACT(HOUR FROM read_at AT TIME ZONE 'UTC')::int AS h, COUNT(*) AS cnt
            FROM user_article_states
            WHERE user_id = :uid AND read_at IS NOT NULL AND dwell_seconds >= 30
            GROUP BY h
            ORDER BY cnt DESC
            LIMIT 1
        """),
        {"uid": user_id},
    )
    hour_row = hour_result.first()

    # Active day (0=Mon … 6=Sun, PostgreSQL DOW: 0=Sun … 6=Sat → convert)
    dow_result = await db.execute(
        text("""
            SELECT EXTRACT(DOW FROM read_at AT TIME ZONE 'UTC')::int AS dow, COUNT(*) AS cnt
            FROM user_article_states
            WHERE user_id = :uid AND read_at IS NOT NULL AND dwell_seconds >= 30
            GROUP BY dow
            ORDER BY cnt DESC
            LIMIT 1
        """),
        {"uid": user_id},
    )
    dow_row = dow_result.first()

    # Minimum 7 records to show active hour/day
    records_result = await db.execute(
        text("""
            SELECT COUNT(*) FROM user_article_states
            WHERE user_id = :uid AND read_at IS NOT NULL AND dwell_seconds >= 30
        """),
        {"uid": user_id},
    )
    total_records = int(records_result.scalar() or 0)
    active_hour = int(hour_row.h) if hour_row and total_records >= 7 else None
    # Convert PostgreSQL DOW (0=Sun) to Python weekday (0=Mon)
    if dow_row and total_records >= 7:
        pg_dow = int(dow_row.dow)
        active_day = (pg_dow - 1) % 7  # Sun(0)→6, Mon(1)→0, …
    else:
        active_day = None

    # Avg time to read (publish → read_at), in hours
    time_to_read_result = await db.execute(
        text("""
            SELECT AVG(EXTRACT(EPOCH FROM (uas.read_at - a.published_at)) / 3600.0)
            FROM user_article_states uas
            JOIN articles a ON a.id = uas.article_id
            WHERE uas.user_id = :uid
              AND uas.read_at IS NOT NULL
              AND uas.dwell_seconds >= 30
              AND a.published_at IS NOT NULL
              AND uas.read_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    avg_ttr = time_to_read_result.scalar()
    avg_time_to_read_hours = round(float(avg_ttr), 1) if avg_ttr is not None else None

    # Avg dwell overall + top 3 feeds by avg dwell
    dwell_result = await db.execute(
        text("""
            SELECT AVG(dwell_seconds::float)
            FROM user_article_states
            WHERE user_id = :uid AND dwell_seconds > 0
        """),
        {"uid": user_id},
    )
    avg_dwell_val = dwell_result.scalar()
    avg_dwell_seconds = round(float(avg_dwell_val), 0) if avg_dwell_val else None

    top_dwell_result = await db.execute(
        text("""
            SELECT COALESCE(uf.custom_title, f.title) AS feed_title,
                   AVG(uas.dwell_seconds::float) AS avg_dwell
            FROM user_article_states uas
            JOIN articles a ON a.id = uas.article_id
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            JOIN feeds f ON f.id = uf.feed_id
            WHERE uas.user_id = :uid AND uas.dwell_seconds > 0
            GROUP BY uf.id, uf.custom_title, f.title
            ORDER BY avg_dwell DESC
            LIMIT 3
        """),
        {"uid": user_id},
    )
    top_feeds_by_dwell = [
        TopFeedDwell(title=r.feed_title, avg_dwell_seconds=round(float(r.avg_dwell), 0))
        for r in top_dwell_result.fetchall()
    ]

    return ReadingStats(
        streak=streak,
        backlog=backlog,
        total_articles=total_articles,
        labeled_count=labeled_count,
        read_count=read_count,
        starred_count=starred_count,
        per_day=per_day,
        active_hour=active_hour,
        active_day=active_day,
        avg_time_to_read_hours=avg_time_to_read_hours,
        avg_dwell_seconds=avg_dwell_seconds,
        top_feeds_by_dwell=top_feeds_by_dwell,
    )


# ── AI stats (for Settings → Stats) ──────────────────────────────────────────

async def get_ai_stats(user_id: int, db: AsyncSession, days: int = 30) -> AiStats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Scoring coverage
    coverage_result = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT a.id) AS total,
                COUNT(DISTINCT CASE WHEN uas.ai_score IS NOT NULL THEN a.id END) AS scored
            FROM articles a
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            WHERE a.fetched_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    cov = coverage_result.one()
    total_cov = int(cov.total or 0)
    scored_cov = int(cov.scored or 0)
    scoring_coverage_pct = round(scored_cov / total_cov * 100, 1) if total_cov > 0 else None

    # Calibration: avg score starred vs non-starred + min starred
    cal_result = await db.execute(
        text("""
            SELECT
                AVG(CASE WHEN uas.ever_starred THEN uas.ai_score END) AS avg_starred,
                AVG(CASE WHEN NOT uas.ever_starred THEN uas.ai_score END) AS avg_not_starred,
                MIN(CASE WHEN uas.ever_starred THEN uas.ai_score END) AS min_starred
            FROM user_article_states uas
            JOIN articles a ON a.id = uas.article_id
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            WHERE uas.user_id = :uid
              AND uas.ai_score IS NOT NULL
              AND a.fetched_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    cal = cal_result.one()
    avg_s = round(float(cal.avg_starred), 2) if cal.avg_starred is not None else None
    avg_n = round(float(cal.avg_not_starred), 2) if cal.avg_not_starred is not None else None
    calibration = AiCalibration(
        avg_score_starred=avg_s,
        avg_score_not_starred=avg_n,
        gap=round((avg_s - avg_n) * 100) if avg_s is not None and avg_n is not None else None,
        min_score_starred=round(float(cal.min_starred) * 100) if cal.min_starred is not None else None,
    )

    # Přehlédnuté poklady — high score, never opened (dwell=0, link_opened=false)
    gems_result = await db.execute(
        text("""
            SELECT a.id, a.title, COALESCE(uf.custom_title, f.title) AS feed_title, uas.ai_score, uas.is_starred
            FROM user_article_states uas
            JOIN articles a ON a.id = uas.article_id
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            JOIN feeds f ON f.id = uf.feed_id
            WHERE uas.user_id = :uid
              AND uas.ai_score >= 0.7
              AND uas.dwell_seconds = 0
              AND uas.link_opened = false
              AND a.fetched_at >= :cutoff
            ORDER BY uas.ai_score DESC
            LIMIT 10
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    gems = [
        GemArticle(article_id=r.id, title=r.title, feed_title=r.feed_title, ai_score=round(float(r.ai_score), 2), is_starred=bool(r.is_starred))
        for r in gems_result.fetchall()
    ]

    # AI se mýlilo — low score, ever_starred
    wrong_result = await db.execute(
        text("""
            SELECT a.id, a.title, COALESCE(uf.custom_title, f.title) AS feed_title, uas.ai_score
            FROM user_article_states uas
            JOIN articles a ON a.id = uas.article_id
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            JOIN feeds f ON f.id = uf.feed_id
            WHERE uas.user_id = :uid
              AND uas.ai_score < 0.3
              AND uas.ever_starred = true
              AND a.fetched_at >= :cutoff
            ORDER BY uas.ai_score ASC
            LIMIT 10
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    wrong = [
        GemArticle(article_id=r.id, title=r.title, feed_title=r.feed_title, ai_score=round(float(r.ai_score), 2))
        for r in wrong_result.fetchall()
    ]

    return AiStats(
        scoring_coverage_pct=scoring_coverage_pct,
        calibration=calibration,
        gems=gems,
        wrong=wrong,
    )


# ── Label stats (for Settings → Stats) ───────────────────────────────────────

async def get_label_stats(user_id: int, db: AsyncSession, days: int = 30) -> LabelStats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Global label coverage % (articles in last 30d with >= 1 label)
    coverage_result = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT a.id) AS total,
                COUNT(DISTINCT al.article_id) AS labeled
            FROM articles a
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            LEFT JOIN article_labels al ON al.article_id = a.id AND al.user_id = :uid
            WHERE a.fetched_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    cov = coverage_result.one()
    total_cov = int(cov.total or 0)
    labeled_cov = int(cov.labeled or 0)
    label_coverage_pct = round(labeled_cov / total_cov * 100, 1) if total_cov > 0 else 0.0

    # Per-label stats
    labels_result = await db.execute(
        text("""
            SELECT
                l.id AS label_id,
                l.name,
                l.color,
                COUNT(DISTINCT a.id) AS article_count,
                COUNT(DISTINCT CASE WHEN uas.ever_starred THEN a.id END) AS starred_count,
                COUNT(DISTINCT CASE WHEN uas.dwell_seconds >= 30 THEN a.id END) AS read_count
            FROM labels l
            JOIN article_labels al ON al.label_id = l.id AND al.user_id = :uid
            JOIN articles a ON a.id = al.article_id
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            WHERE l.user_id = :uid
              AND a.fetched_at >= :cutoff
            GROUP BY l.id, l.name, l.color
            ORDER BY article_count DESC
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    label_rows = []
    for r in labels_result.fetchall():
        count = int(r.article_count or 0)
        starred = int(r.starred_count or 0)
        read = int(r.read_count or 0)
        label_rows.append(LabelRow(
            label_id=int(r.label_id),
            name=r.name,
            color=r.color or "#6366f1",
            article_count=count,
            star_rate=round(starred / count * 100, 1) if count > 0 else 0.0,
            read_rate=round(read / count * 100, 1) if count > 0 else 0.0,
        ))

    return LabelStats(
        label_coverage_pct=label_coverage_pct,
        labels=label_rows,
    )


# ── AI cost stats (for Settings → AI) ────────────────────────────────────────

from app.services.ai_service import (  # noqa: E402
    _MODEL_ALIAS_MAP,
    _MODEL_INPUT_COST_PER_M,
    _OUTPUT_COST_MULTIPLIER,
)


def _calc_cost(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    if not model:
        return None
    key = _MODEL_ALIAS_MAP.get(model, model)
    input_cost_per_m = _MODEL_INPUT_COST_PER_M.get(key)
    if input_cost_per_m is None:
        return None
    output_multiplier = _OUTPUT_COST_MULTIPLIER.get(key, 4.0)
    output_cost_per_m = input_cost_per_m * output_multiplier
    return round(
        input_tokens * input_cost_per_m / 1_000_000
        + output_tokens * output_cost_per_m / 1_000_000,
        4,
    )


async def get_ai_cost_stats(user_id: int, db: AsyncSession, days: int = 30) -> AiCostStats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    prev_cutoff = cutoff - timedelta(days=days)

    s = (await db.execute(
        text("SELECT ai_fast_model, ai_quality_model FROM user_settings WHERE user_id = :uid"),
        {"uid": user_id},
    )).one_or_none()
    fast_model = s[0] if s else None
    quality_model = s[1] if s else None

    async def _op_stats(operation: str, period_cutoff: datetime) -> tuple[int, int, int]:
        """Returns (count, input_tokens, output_tokens) for a period."""
        r = await db.execute(
            text("""
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM article_ai_jobs
                WHERE user_id = :uid
                  AND operation = :op
                  AND status = 'success'
                  AND processed_at >= :cutoff
                  AND processed_at < :end_cutoff
            """),
            {
                "uid": user_id,
                "op": operation,
                "cutoff": period_cutoff,
                "end_cutoff": period_cutoff + timedelta(days=days),
            },
        )
        row = r.one()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    def _trend(current: int, previous: int) -> float | None:
        if previous == 0:
            return None
        return round((current - previous) / previous * 100, 1)

    ops_config = [
        ("scoring", "Scoring", "fast"),
        ("summary", "Summary", "quality"),
        ("context", "Context", "quality"),
    ]

    operation_rows = []
    for op, label, slot in ops_config:
        model = fast_model if slot == "fast" else quality_model
        cnt, inp, out = await _op_stats(op, cutoff)
        prev_cnt, prev_inp, prev_out = await _op_stats(op, prev_cutoff)
        est_cost = _calc_cost(model, inp, out)
        operation_rows.append(OperationCostRow(
            operation=op,
            label=label,
            slot=slot,
            count=cnt,
            input_tokens=inp,
            output_tokens=out,
            est_cost=est_cost,
            trend_pct=_trend(cnt, prev_cnt),
        ))

    # Chat: messages + tokens from article_ai_chats UNION general_chat_log
    chat_result = await db.execute(
        text("""
            SELECT
                COALESCE(SUM(msg_count), 0),
                COALESCE(SUM(in_tok), 0),
                COALESCE(SUM(out_tok), 0)
            FROM (
                SELECT jsonb_array_length(messages) AS msg_count,
                       total_input_tokens AS in_tok,
                       total_output_tokens AS out_tok
                FROM article_ai_chats
                WHERE user_id = :uid AND updated_at >= :cutoff
                UNION ALL
                SELECT 2 AS msg_count,
                       input_tokens AS in_tok,
                       output_tokens AS out_tok
                FROM general_chat_log
                WHERE user_id = :uid AND created_at >= :cutoff
            ) t
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    chat_row = chat_result.one()
    chat_count = int(chat_row[0] or 0)
    chat_in_tok = int(chat_row[1] or 0)
    chat_out_tok = int(chat_row[2] or 0)

    prev_chat_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(msg_count), 0)
            FROM (
                SELECT jsonb_array_length(messages) AS msg_count
                FROM article_ai_chats
                WHERE user_id = :uid
                  AND updated_at >= :prev_cutoff AND updated_at < :cutoff
                UNION ALL
                SELECT 2 AS msg_count
                FROM general_chat_log
                WHERE user_id = :uid
                  AND created_at >= :prev_cutoff AND created_at < :cutoff
            ) t
        """),
        {"uid": user_id, "prev_cutoff": prev_cutoff, "cutoff": cutoff},
    )
    prev_chat_count = int(prev_chat_result.scalar() or 0)

    operation_rows.append(OperationCostRow(
        operation="chat",
        label="Chat",
        slot="quality",
        count=chat_count,
        input_tokens=chat_in_tok,
        output_tokens=chat_out_tok,
        est_cost=_calc_cost(quality_model, chat_in_tok, chat_out_tok),
        trend_pct=_trend(chat_count, prev_chat_count),
    ))

    # Catch me up — per slot from catchup_logs
    async def _catchup_slot_stats(slot: str, period_cutoff: datetime) -> tuple[int, int, int]:
        r = await db.execute(
            text("""
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM catchup_logs
                WHERE user_id = :uid
                  AND model_slot = :slot
                  AND created_at >= :cutoff
                  AND created_at < :end_cutoff
            """),
            {
                "uid": user_id,
                "slot": slot,
                "cutoff": period_cutoff,
                "end_cutoff": period_cutoff + timedelta(days=days),
            },
        )
        row = r.one()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    for cu_slot, cu_model in (("fast", fast_model), ("quality", quality_model)):
        cu_cnt, cu_inp, cu_out = await _catchup_slot_stats(cu_slot, cutoff)
        prev_cu_cnt, _, _ = await _catchup_slot_stats(cu_slot, prev_cutoff)
        # Cold start: no runs yet → estimated per-run cost
        if cu_cnt == 0:
            from app.services.catchup_service import estimate_catchup_tokens  # noqa: PLC0415
            est_inp, est_out = estimate_catchup_tokens(200, include_snippet=True)
            est = _calc_cost(cu_model, est_inp, est_out)
            operation_rows.append(OperationCostRow(
                operation=f"catch_me_up_{cu_slot}",
                label=f"Catch me up ({cu_slot})",
                slot=cu_slot,
                count=0,
                input_tokens=0,
                output_tokens=0,
                est_cost=est,
                trend_pct=None,
                is_placeholder=True,
            ))
        else:
            operation_rows.append(OperationCostRow(
                operation=f"catch_me_up_{cu_slot}",
                label=f"Catch me up ({cu_slot})",
                slot=cu_slot,
                count=cu_cnt,
                input_tokens=cu_inp,
                output_tokens=cu_out,
                est_cost=_calc_cost(cu_model, cu_inp, cu_out),
                trend_pct=_trend(cu_cnt, prev_cu_cnt),
            ))

    # Subtotals
    real_rows = [r for r in operation_rows if not r.is_placeholder]
    fast_rows = [r for r in real_rows if r.slot == "fast"]
    quality_rows = [r for r in real_rows if r.slot == "quality"]

    def _subtotal(rows: list[OperationCostRow], label: str, slot: str, model: str | None) -> OperationCostRow:
        inp = sum(r.input_tokens for r in rows)
        out = sum(r.output_tokens for r in rows)
        return OperationCostRow(
            operation=f"_total_{slot}",
            label=label,
            slot=slot,
            count=0,
            input_tokens=inp,
            output_tokens=out,
            est_cost=_calc_cost(model, inp, out),
            trend_pct=None,
            row_type="subtotal",
        )

    fast_total = _subtotal(fast_rows, "Fast total", "fast", fast_model)
    quality_total = _subtotal(quality_rows, "Quality total", "quality", quality_model)

    all_inp = fast_total.input_tokens + quality_total.input_tokens
    all_out = fast_total.output_tokens + quality_total.output_tokens
    grand_total = OperationCostRow(
        operation="_total_all",
        label="Total",
        slot="",
        count=0,
        input_tokens=all_inp,
        output_tokens=all_out,
        est_cost=(
            (fast_total.est_cost or 0.0) + (quality_total.est_cost or 0.0)
            if fast_total.est_cost is not None or quality_total.est_cost is not None
            else None
        ),
        trend_pct=None,
        row_type="total",
    )

    operation_rows += [fast_total, quality_total, grand_total]

    return AiCostStats(period_days=days, operations=operation_rows)
