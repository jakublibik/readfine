"""Statistics service — reading stats, feed quality, AI stats, label stats."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UserSettings
from app.services.filter_service import SUPPRESSED_BY_FILTER
# The same number everywhere it is asked: half a minute in front of an article is what
# counts as having read it. It decides something in story_service (it clears the
# machine's suppressed_at), which is why that is where it lives.
from app.services.story_service import ENGAGED_DWELL_SECONDS
from app.utils.datetime_format import current_viewer_tz, resolve_tz


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FeedStatRow:
    user_feed_id: int
    feed_title: str
    total_articles: int
    read_pct: float        # (dwell >= 30s OR link opened) / total
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
    labeled_backlog: int    # unread + has label (all time)
    starred_backlog: int    # starred (all time)
    per_day: list[DailyRead]
    active_hour: int | None      # 0-23, None if < 7 records
    active_day: int | None       # 0=Mon … 6=Sun, None if < 7 records
    avg_time_to_read_hours: float | None
    avg_dwell_seconds: float | None
    top_feeds_by_dwell: list[TopFeedDwell]


@dataclass
class ScoreBand:
    floor: int | None     # inclusive, None on the bottom band
    ceiling: int | None   # inclusive, None on the top band
    articles: int
    at_floor_or_better: int   # this band plus every band above it


@dataclass
class IntakeColumn:
    """One reading of the month: everything, or only what carries a label.

    The first five numbers partition ``fetched`` exactly: every article either never
    reached the reader (one of the three machine reads), reached them folded into
    somebody else's row, or was a row of its own. Nothing here may be counted twice or
    left out, which is the point of the block.

    ``read`` and ``starred`` are not a sixth and seventh step and must not be drawn as
    one. They are counted over everything that reached the reader, folded rows
    included, because a folded article is one unfold away and gets read that way all
    the time. In the block they sit under the funnel without a bar, which is what says
    they do not nest inside the row above them.
    """
    fetched: int
    suppressed: int       # hidden as a repeat of a story already read
    filtered: int         # a filter's mark-read action took it
    url_dupes: int        # the same link had already arrived in another feed
    folded: int           # folded into another row of the same story
    rows_shown: int       # what was actually there to go past
    read: int             # dwell >= 30s OR link opened
    starred: int          # ever starred


@dataclass
class IntakeStats:
    """A month's worth of articles and what became of them, article by article.

    Two readings of the same month, because they answer different questions and the
    reader who works through labels is asking the second one. ``labeled`` is not
    ``all`` with a smaller first number: a label changes every line of the funnel, and
    it changes ``folded`` the most, since a list filtered to one label folds the
    members carrying that label and nothing else.

    ``labeled`` is None for a reader with no labels in the window, where a second
    column of zeroes would be noise.
    """
    all: IntakeColumn
    labeled: IntakeColumn | None
    # One set of bands per scorer, never merged. The AI score exists only on
    # labelled articles and the lexical one on everything, so a single spread over
    # whichever number happened to be there would describe neither scorer.
    bands: list[ScoreBand]
    scored: int
    unscored: int
    lexical_bands: list[ScoreBand]
    lexical_scored: int


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
    read_rate: float   # dwell >= 30s OR link opened


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
    is_estimated: bool = False  # priced via provider fallback (model not in catalog)
    is_placeholder: bool = False
    row_type: str = "operation"  # "operation" | "separator" | "subtotal" | "total"


@dataclass
class AiCostStats:
    period_days: int
    operations: list[OperationCostRow]
    # True when either slot runs on a custom endpoint, which is why some rows
    # carry tokens but no price.
    has_unpriced_provider: bool = False
    # True when the table shows any priced usage at all. Separate from the flag
    # above on purpose: prices are worked out from the model each slot holds
    # *now*, so switching a slot re-prices everything behind it — and that is
    # least obvious right after moving off a custom endpoint, when a month of
    # unpriced runs suddenly acquires the new model's rate. Keying the note on
    # the custom provider still being configured would hide it exactly then.
    prices_follow_current_model: bool = False


# ── Feed stats (for Settings → Feeds stats toggle) ────────────────────────────

async def get_feed_stats(user_id: int, db: AsyncSession, days: int = 30) -> list[FeedStatRow]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        text("""
            SELECT
                uf.id AS user_feed_id,
                COALESCE(uf.custom_title, f.title) AS feed_title,
                COUNT(DISTINCT a.id) AS total_articles,
                COUNT(DISTINCT CASE WHEN uas.dwell_seconds >= 30 OR uas.link_opened THEN a.id END) AS read_count,
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


# ── Intake (for Settings → Stats) ────────────────────────────────────────────

# The bands the month's articles are cut into, as (floor, ceiling) in the 0–100 the
# UI shows. Both ends are inclusive, so an article scored 75 is in the top band and
# one scored 25 is in the bottom one: the reader who skips "25 and below" is skipping
# the 25s too, and a band whose edge meant something else would be read wrong.
SCORE_BANDS = ((75, None), (50, 74), (26, 49), (None, 25))

# The value the cross-feed URL dedup stamps (fetcher.rss._dedup_state). Named here
# rather than imported because importing the fetcher into the stats service to read one
# string is not worth the dependency; ``SUPPRESSED_BY_FILTER`` comes from the service
# that writes it, which is next door.
_SUPPRESSED_BY_URL = "url"

# How an article that arrived in the window ended up, in the order the block lists
# them. The three machine reads never reached the reader at all; what is left either
# sat under another article's row or was a row itself.
_INTAKE_BUCKETS = """
    CASE WHEN uas.hidden_at IS NOT NULL         THEN 'suppressed'
         WHEN uas.suppressed_by = :by_filter    THEN 'filtered'
         WHEN uas.suppressed_by = :by_url       THEN 'url_dupe'
         ELSE 'shown' END
"""


async def get_intake_stats(
    user_id: int, db: AsyncSession, *, collapsing: bool, days: int = 30,
) -> IntakeStats:
    """A month of articles and what became of each one.

    Counts every article fetched in the window whatever state it is in now, which is
    what makes the numbers add up to a month's intake rather than to a leftover pile.
    The reader wants to know how much actually came at them and how much of it they
    were spared, and an article they have since read was still one they went past.

    Three things take an article before it is ever seen, and each is a line of its own
    because they are different favours: suppression hides a repeat of a story already
    read, a filter's mark-read action takes what the reader told it to take, and the
    URL dedup drops a link that had already arrived in another feed. All three are
    machine reads, stamped as such, so none of them is mistaken for reading.

    What remains is split by whether story grouping put it under somebody else's row.
    Folding is the one step here with no record behind it: it is derived from the
    article's story and the reader's setting as they stand now, so it answers "how
    much of this month would fold today". The three above are stamped when they happen
    and answer what did happen. With grouping off nothing folds and every article that
    reached the reader is a row.

    The bands cover everything fetched, unread or not: they say what the scorer made
    of the month, which is a different question from what is left to read.

    Retention is the limit on all of it. An article nobody engaged with is deleted
    outright once its feed's horizon passes, so on an instance keeping less than the
    window these numbers read as a floor. ``get_reading_stats`` counts "Fetched" the
    same way, so the two at least understate together.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # The group key: a story for a grouped article, the article itself for an ungrouped
    # one, the expression the sidebar badge counts by (story_service.row_count). With
    # grouping off every article is its own partition, so the rank is always 1 and the
    # same statement counts articles without a second branch to keep in step.
    key = "COALESCE(a.story_id, -a.id)" if collapsing else "a.id"

    row = (await db.execute(
        text(f"""
            WITH classed AS (
                SELECT a.id, a.story_id, a.published_at, a.fetched_at,
                       ROUND(uas.ai_score * 100)::int AS pct,
                       ROUND(uas.lexical_score * 100)::int AS lex_pct,
                       {_INTAKE_BUCKETS} AS bucket,
                       (COALESCE(uas.dwell_seconds, 0) >= :dwell
                        OR COALESCE(uas.link_opened, false))                AS engaged,
                       COALESCE(uas.ever_starred, false)                    AS starred,
                       (al.article_id IS NOT NULL)                          AS labeled
                FROM articles a
                JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
                LEFT JOIN user_article_states uas
                       ON uas.article_id = a.id AND uas.user_id = :uid
                LEFT JOIN (
                    SELECT DISTINCT article_id
                    FROM article_labels WHERE user_id = :uid
                ) al ON al.article_id = a.id
                WHERE a.fetched_at >= :cutoff
            ), ranked AS (
                -- Two ranks, because the two columns fold differently. The first is the
                -- list as it stands; the second is the list filtered to one reader's
                -- labels, where a group folds only the members carrying one, so a
                -- labelled article whose siblings are unlabelled is a row of its own.
                SELECT labeled, engaged, starred,
                       ROW_NUMBER() OVER (
                           PARTITION BY {key}
                           ORDER BY COALESCE(a.published_at, a.fetched_at) DESC, a.id DESC
                       ) AS member_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY {key}, labeled
                           ORDER BY COALESCE(a.published_at, a.fetched_at) DESC, a.id DESC
                       ) AS labeled_rank
                FROM classed a
                WHERE bucket = 'shown'
            )
            SELECT
                (SELECT COUNT(*) FROM classed)                                     AS fetched,
                (SELECT COUNT(*) FROM classed WHERE bucket = 'suppressed')         AS suppressed,
                (SELECT COUNT(*) FROM classed WHERE bucket = 'filtered')           AS filtered,
                (SELECT COUNT(*) FROM classed WHERE bucket = 'url_dupe')           AS url_dupes,
                (SELECT COUNT(*) FROM ranked WHERE member_rank > 1)                AS folded,
                (SELECT COUNT(*) FROM ranked WHERE member_rank = 1)                AS rows_shown,
                (SELECT COUNT(*) FROM classed
                  WHERE bucket = 'shown' AND engaged)                              AS read,
                (SELECT COUNT(*) FROM classed
                  WHERE bucket = 'shown' AND starred)                              AS starred,
                (SELECT COUNT(*) FROM classed WHERE labeled)                       AS l_fetched,
                (SELECT COUNT(*) FROM classed
                  WHERE labeled AND bucket = 'suppressed')                         AS l_suppressed,
                (SELECT COUNT(*) FROM classed
                  WHERE labeled AND bucket = 'filtered')                           AS l_filtered,
                (SELECT COUNT(*) FROM classed
                  WHERE labeled AND bucket = 'url_dupe')                           AS l_url_dupes,
                (SELECT COUNT(*) FROM ranked
                  WHERE labeled AND labeled_rank > 1)                              AS l_folded,
                (SELECT COUNT(*) FROM ranked
                  WHERE labeled AND labeled_rank = 1)                              AS l_rows_shown,
                (SELECT COUNT(*) FROM classed
                  WHERE labeled AND bucket = 'shown' AND engaged)                  AS l_read,
                (SELECT COUNT(*) FROM classed
                  WHERE labeled AND bucket = 'shown' AND starred)                  AS l_starred,
                (SELECT COUNT(*) FROM classed WHERE pct >= 75)                     AS b_top,
                (SELECT COUNT(*) FROM classed WHERE pct >= 50 AND pct < 75)        AS b_mid,
                (SELECT COUNT(*) FROM classed WHERE pct > 25 AND pct < 50)         AS b_low,
                (SELECT COUNT(*) FROM classed WHERE pct <= 25)                     AS b_bottom,
                (SELECT COUNT(*) FROM classed WHERE pct IS NULL)                   AS b_unscored,
                (SELECT COUNT(*) FROM classed WHERE lex_pct >= 75)                  AS lb_top,
                (SELECT COUNT(*) FROM classed WHERE lex_pct >= 50 AND lex_pct < 75) AS lb_mid,
                (SELECT COUNT(*) FROM classed WHERE lex_pct > 25 AND lex_pct < 50)  AS lb_low,
                (SELECT COUNT(*) FROM classed WHERE lex_pct <= 25)                  AS lb_bottom
        """),
        {
            "uid": user_id, "cutoff": cutoff, "dwell": ENGAGED_DWELL_SECONDS,
            "by_filter": SUPPRESSED_BY_FILTER, "by_url": _SUPPRESSED_BY_URL,
        },
    )).one()

    def _bands(counts) -> list[ScoreBand]:
        out: list[ScoreBand] = []
        running = 0
        for (floor, ceiling), count in zip(SCORE_BANDS, counts):
            running += int(count or 0)
            out.append(ScoreBand(
                floor=floor, ceiling=ceiling,
                articles=int(count or 0), at_floor_or_better=running,
            ))
        return out

    bands = _bands([row.b_top, row.b_mid, row.b_low, row.b_bottom])
    lexical_bands = _bands([row.lb_top, row.lb_mid, row.lb_low, row.lb_bottom])

    fetched = int(row.fetched or 0)
    unscored = int(row.b_unscored or 0)
    labeled_fetched = int(row.l_fetched or 0)
    return IntakeStats(
        all=IntakeColumn(
            fetched=fetched,
            suppressed=int(row.suppressed or 0),
            filtered=int(row.filtered or 0),
            url_dupes=int(row.url_dupes or 0),
            folded=int(row.folded or 0),
            rows_shown=int(row.rows_shown or 0),
            read=int(row.read or 0),
            starred=int(row.starred or 0),
        ),
        labeled=IntakeColumn(
            fetched=labeled_fetched,
            suppressed=int(row.l_suppressed or 0),
            filtered=int(row.l_filtered or 0),
            url_dupes=int(row.l_url_dupes or 0),
            folded=int(row.l_folded or 0),
            rows_shown=int(row.l_rows_shown or 0),
            read=int(row.l_read or 0),
            starred=int(row.l_starred or 0),
        ) if labeled_fetched else None,
        bands=bands,
        scored=fetched - unscored,
        unscored=unscored,
        lexical_bands=lexical_bands,
        lexical_scored=sum(b.articles for b in lexical_bands),
    )


# ── Reading stats (for Settings → Stats) ─────────────────────────────────────

async def get_reading_stats(user_id: int, db: AsyncSession, days: int = 30) -> ReadingStats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    tz = current_viewer_tz.get()  # IANA name of the viewer's timezone (defaults to UTC)

    # Streak — consecutive days with dwell >= 30s, using read_at as timestamp
    streak_result = await db.execute(
        text("""
            WITH read_days AS (
                SELECT DISTINCT (read_at AT TIME ZONE :tz)::date AS d
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
            WHERE last_day >= (now() AT TIME ZONE :tz)::date - INTERVAL '1 day'
            ORDER BY last_day DESC
            LIMIT 1
        """),
        {"uid": user_id, "tz": tz},
    )
    streak = int(streak_result.scalar() or 0)

    # Labeled backlog — unread + has label (all time)
    labeled_backlog_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT a.id)
            FROM articles a
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            JOIN article_labels al ON al.article_id = a.id AND al.user_id = :uid
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            WHERE (uas.is_read IS NULL OR uas.is_read = false)
              AND a.trimmed_at IS NULL
        """),
        {"uid": user_id},
    )
    labeled_backlog = int(labeled_backlog_result.scalar() or 0)

    # Starred backlog — currently starred (all time, "to read" pile)
    starred_backlog_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT a.id)
            FROM articles a
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            WHERE uas.is_starred = true
              AND a.trimmed_at IS NULL
        """),
        {"uid": user_id},
    )
    starred_backlog = int(starred_backlog_result.scalar() or 0)

    # Per-day reads (last 7 days, dwell >= 30s), grouped by the viewer's local date.
    # Widened to 8 absolute days so the oldest visible local day isn't truncated at
    # large UTC offsets.
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=8)
    per_day_result = await db.execute(
        text("""
            SELECT
                (read_at AT TIME ZONE :tz)::date AS d,
                COUNT(*) AS cnt
            FROM user_article_states
            WHERE user_id = :uid
              AND read_at IS NOT NULL
              AND dwell_seconds >= 30
              AND read_at >= :cutoff_7d
            GROUP BY (read_at AT TIME ZONE :tz)::date
            ORDER BY d
        """),
        {"uid": user_id, "cutoff_7d": cutoff_7d, "tz": tz},
    )
    per_day_map = {str(r.d): int(r.cnt) for r in per_day_result.fetchall()}
    today = datetime.now(resolve_tz(tz)).date()
    per_day = [
        DailyRead(
            date=str(today - timedelta(days=6 - i)),
            count=per_day_map.get(str(today - timedelta(days=6 - i)), 0),
        )
        for i in range(7)
    ]

    # Active hour (0-23, in the viewer's timezone)
    hour_result = await db.execute(
        text("""
            SELECT EXTRACT(HOUR FROM read_at AT TIME ZONE :tz)::int AS h, COUNT(*) AS cnt
            FROM user_article_states
            WHERE user_id = :uid AND read_at IS NOT NULL AND dwell_seconds >= 30
            GROUP BY h
            ORDER BY cnt DESC
            LIMIT 1
        """),
        {"uid": user_id, "tz": tz},
    )
    hour_row = hour_result.first()

    # Active day (0=Mon … 6=Sun, PostgreSQL DOW: 0=Sun … 6=Sat → convert; viewer's tz)
    dow_result = await db.execute(
        text("""
            SELECT EXTRACT(DOW FROM read_at AT TIME ZONE :tz)::int AS dow, COUNT(*) AS cnt
            FROM user_article_states
            WHERE user_id = :uid AND read_at IS NOT NULL AND dwell_seconds >= 30
            GROUP BY dow
            ORDER BY cnt DESC
            LIMIT 1
        """),
        {"uid": user_id, "tz": tz},
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
        labeled_backlog=labeled_backlog,
        starred_backlog=starred_backlog,
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

    # Overlooked gems — high score, never opened (dwell=0, link_opened=false).
    #
    # Scoring runs per article, but a story group is one piece of news covered by
    # several sources, so the group has to be taken into account twice here. Read any
    # member of it and the news is not missed, whichever row carried it; and a group
    # whose members all scored high would otherwise fill the ten rows with one event
    # told five times. Hence the NOT EXISTS (engagement anywhere in the group) and one
    # row per group, the highest-scoring one.
    #
    # The sibling check looks only at this reader's own engagement, so it needs no
    # access join: a state row with time on it is a read they made, whether or not they
    # still subscribe to the feed it came from. Articles outside any group key on their
    # own negated id, which no story_id can collide with.
    gems_result = await db.execute(
        text("""
            WITH candidates AS (
                SELECT a.id, a.title, COALESCE(uf.custom_title, f.title) AS feed_title,
                       uas.ai_score, uas.is_starred,
                       COALESCE(a.story_id, -a.id) AS story_key
                FROM user_article_states uas
                JOIN articles a ON a.id = uas.article_id
                JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
                JOIN feeds f ON f.id = uf.feed_id
                WHERE uas.user_id = :uid
                  AND uas.ai_score >= 0.7
                  AND uas.dwell_seconds = 0
                  AND uas.link_opened = false
                  AND a.fetched_at >= :cutoff
                  AND NOT EXISTS (
                      SELECT 1
                      FROM articles sib
                      JOIN user_article_states sib_uas
                        ON sib_uas.article_id = sib.id AND sib_uas.user_id = :uid
                      WHERE a.story_id IS NOT NULL
                        AND sib.story_id = a.story_id
                        AND (sib_uas.dwell_seconds >= :dwell OR sib_uas.link_opened)
                  )
            )
            SELECT id, title, feed_title, ai_score, is_starred
            FROM (
                SELECT DISTINCT ON (story_key)
                       id, title, feed_title, ai_score, is_starred
                FROM candidates
                ORDER BY story_key, ai_score DESC, id
            ) one_per_story
            ORDER BY ai_score DESC
            LIMIT 10
        """),
        {"uid": user_id, "cutoff": cutoff, "dwell": ENGAGED_DWELL_SECONDS},
    )
    gems = [
        GemArticle(article_id=r.id, title=r.title, feed_title=r.feed_title, ai_score=round(float(r.ai_score), 2), is_starred=bool(r.is_starred))
        for r in gems_result.fetchall()
    ]

    # AI got it wrong — low score, ever_starred
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
                COUNT(DISTINCT CASE WHEN uas.dwell_seconds >= 30 OR uas.link_opened THEN a.id END) AS read_count
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
    _PROVIDER_FALLBACK_MODEL,
)


def _calc_cost(
    model: str | None,
    provider: str | None,
    input_tokens: int,
    output_tokens: int,
) -> tuple[float | None, bool]:
    """Return (est_cost, is_estimated). When the configured model isn't in the
    catalog, fall back to a representative model for the provider and flag the
    result as estimated. Returns (None, False) only when neither the model nor a
    provider fallback can be priced."""
    if not model:
        return None, False
    # A custom endpoint has no price list of ours, and leaving it out of
    # _PROVIDER_FALLBACK_MODEL is not enough on its own: the catalog is consulted
    # by model name first, and proxies serve models under names like "gpt-4o", so
    # a local run would be billed at OpenAI's rate and not even be flagged as an
    # estimate. Whatever the model calls itself, we do not know what it costs.
    if provider == "custom":
        return None, False
    key = _MODEL_ALIAS_MAP.get(model, model)
    input_cost_per_m = _MODEL_INPUT_COST_PER_M.get(key)
    is_estimated = False
    if input_cost_per_m is None:
        fallback = _PROVIDER_FALLBACK_MODEL.get(provider or "")
        if fallback is None:
            return None, False
        key = fallback
        input_cost_per_m = _MODEL_INPUT_COST_PER_M[key]
        is_estimated = True
    output_multiplier = _OUTPUT_COST_MULTIPLIER.get(key, 4.0)
    output_cost_per_m = input_cost_per_m * output_multiplier
    cost = round(
        input_tokens * input_cost_per_m / 1_000_000
        + output_tokens * output_cost_per_m / 1_000_000,
        4,
    )
    return cost, is_estimated


def _sum_costs(values: list[float | None]) -> float | None:
    """Sum of the priced entries, None when nothing in the group has a price.

    What makes a mixed table work: with scoring on a local endpoint and the main
    slot on a paid provider, the total is the paid work rather than nothing.
    """
    priced = [v for v in values if v is not None]
    return round(sum(priced), 4) if priced else None


async def get_ai_cost_stats(user_id: int, db: AsyncSession, days: int = 30) -> AiCostStats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    prev_cutoff = cutoff - timedelta(days=days)

    s = (await db.execute(
        text("""
            SELECT ai_fast_model, ai_quality_model, ai_fast_provider, ai_quality_provider
            FROM user_settings WHERE user_id = :uid
        """),
        {"uid": user_id},
    )).one_or_none()
    fast_model = s[0] if s else None
    quality_model = s[1] if s else None
    fast_provider = s[2] if s else None
    quality_provider = s[3] if s else None

    def _prov(slot: str) -> str | None:
        return fast_provider if slot == "fast" else quality_provider

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

    def _trend(current: float | None, previous: float | None) -> float | None:
        # Trend is cost-based: % change in this period's est. cost vs the previous
        # period's tokens priced at the current model. None when either side has no
        # priced usage.
        if not previous or current is None:
            return None
        return round((current - previous) / previous * 100, 1)

    # Accumulate each row's previous-period cost per slot, so the subtotal and the
    # total can derive a trend without a separate query. Costs rather than tokens,
    # because the catch-up row prices historical runs by the model that actually
    # ran them and its tokens can't be re-priced with one model afterwards.
    prev_cost_by_slot: dict[str, list[float | None]] = {"fast": [], "quality": []}

    ops_config = [
        ("scoring", "Scoring", "fast"),
        ("summary", "Summary", "quality"),
        ("context", "Context", "quality"),
    ]

    operation_rows = []
    for op, label, slot in ops_config:
        model = fast_model if slot == "fast" else quality_model
        cnt, inp, out = await _op_stats(op, cutoff)
        _, prev_inp, prev_out = await _op_stats(op, prev_cutoff)
        est_cost, est_flag = _calc_cost(model, _prov(slot), inp, out)
        prev_cost = _calc_cost(model, _prov(slot), prev_inp, prev_out)[0]
        prev_cost_by_slot[slot].append(prev_cost)
        operation_rows.append(OperationCostRow(
            operation=op,
            label=label,
            slot=slot,
            count=cnt,
            input_tokens=inp,
            output_tokens=out,
            est_cost=est_cost,
            is_estimated=est_flag,
            trend_pct=_trend(est_cost, prev_cost),
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
            SELECT
                COALESCE(SUM(msg_count), 0),
                COALESCE(SUM(in_tok), 0),
                COALESCE(SUM(out_tok), 0)
            FROM (
                SELECT jsonb_array_length(messages) AS msg_count,
                       total_input_tokens AS in_tok,
                       total_output_tokens AS out_tok
                FROM article_ai_chats
                WHERE user_id = :uid
                  AND updated_at >= :prev_cutoff AND updated_at < :cutoff
                UNION ALL
                SELECT 2 AS msg_count,
                       input_tokens AS in_tok,
                       output_tokens AS out_tok
                FROM general_chat_log
                WHERE user_id = :uid
                  AND created_at >= :prev_cutoff AND created_at < :cutoff
            ) t
        """),
        {"uid": user_id, "prev_cutoff": prev_cutoff, "cutoff": cutoff},
    )
    prev_chat_row = prev_chat_result.one()
    prev_chat_in_tok = int(prev_chat_row[1] or 0)
    prev_chat_out_tok = int(prev_chat_row[2] or 0)

    # Interest profile generation — from ai_usage_logs
    async def _usage_log_stats(operation: str, period_cutoff: datetime) -> tuple[int, int, int]:
        r = await db.execute(
            text("""
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM ai_usage_logs
                WHERE user_id = :uid
                  AND operation = :op
                  AND created_at >= :cutoff
                  AND created_at < :end_cutoff
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

    pref_cnt, pref_inp, pref_out = await _usage_log_stats("preference_generation", cutoff)
    _, prev_pref_inp, prev_pref_out = await _usage_log_stats("preference_generation", prev_cutoff)
    pref_cost, pref_est = _calc_cost(quality_model, quality_provider, pref_inp, pref_out)
    prev_pref_cost = _calc_cost(quality_model, quality_provider, prev_pref_inp, prev_pref_out)[0]
    prev_cost_by_slot["quality"].append(prev_pref_cost)
    operation_rows.append(OperationCostRow(
        operation="preference_generation",
        label="Interest profile",
        slot="quality",
        count=pref_cnt,
        input_tokens=pref_inp,
        output_tokens=pref_out,
        est_cost=pref_cost,
        is_estimated=pref_est,
        trend_pct=_trend(pref_cost, prev_pref_cost),
    ))

    css_cnt, css_inp, css_out = await _usage_log_stats("css_selector_generation", cutoff)
    _, prev_css_inp, prev_css_out = await _usage_log_stats("css_selector_generation", prev_cutoff)
    css_cost, css_est = _calc_cost(quality_model, quality_provider, css_inp, css_out)
    prev_css_cost = _calc_cost(quality_model, quality_provider, prev_css_inp, prev_css_out)[0]
    prev_cost_by_slot["quality"].append(prev_css_cost)
    operation_rows.append(OperationCostRow(
        operation="css_selector_generation",
        label="CSS selector generation",
        slot="quality",
        count=css_cnt,
        input_tokens=css_inp,
        output_tokens=css_out,
        est_cost=css_cost,
        is_estimated=css_est,
        trend_pct=_trend(css_cost, prev_css_cost),
    ))

    chat_cost, chat_est = _calc_cost(quality_model, quality_provider, chat_in_tok, chat_out_tok)
    prev_chat_cost = _calc_cost(quality_model, quality_provider, prev_chat_in_tok, prev_chat_out_tok)[0]
    prev_cost_by_slot["quality"].append(prev_chat_cost)
    operation_rows.append(OperationCostRow(
        operation="chat",
        label="Chat",
        slot="quality",
        count=chat_count,
        input_tokens=chat_in_tok,
        output_tokens=chat_out_tok,
        est_cost=chat_cost,
        is_estimated=chat_est,
        trend_pct=_trend(chat_cost, prev_chat_cost),
    ))

    # Catch me up — one row, priced per model that actually ran. catchup_logs
    # records the model with every run, so runs from before the digest moved to
    # the main model are priced by the model that wrote them rather than by
    # whatever is configured today.
    async def _catchup_stats(
        period_cutoff: datetime,
    ) -> tuple[int, int, int, float | None, bool]:
        r = await db.execute(
            text("""
                SELECT model, provider,
                       COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM catchup_logs
                WHERE user_id = :uid
                  AND created_at >= :cutoff
                  AND created_at < :end_cutoff
                GROUP BY model, provider
            """),
            {
                "uid": user_id,
                "cutoff": period_cutoff,
                "end_cutoff": period_cutoff + timedelta(days=days),
            },
        )
        cnt = inp = out = 0
        costs: list[float | None] = []
        estimated = False
        for model, provider, group_cnt, group_inp, group_out in r:
            cnt += int(group_cnt or 0)
            inp += int(group_inp or 0)
            out += int(group_out or 0)
            # A run that found no articles logs no model and spends nothing.
            if not model:
                continue
            cost, est_flag = _calc_cost(model, provider, int(group_inp or 0), int(group_out or 0))
            costs.append(cost)
            estimated = estimated or est_flag
        return cnt, inp, out, _sum_costs(costs), estimated

    cu_cnt, cu_inp, cu_out, cu_cost, cu_est = await _catchup_stats(cutoff)
    if cu_cnt == 0:
        # Cold start: no runs yet → estimated cost of one run on the main model
        from app.services.catchup_service import estimate_catchup_tokens  # noqa: PLC0415
        est_inp, est_out = estimate_catchup_tokens(200, include_snippet=True)
        est, est_flag = _calc_cost(quality_model, quality_provider, est_inp, est_out)
        operation_rows.append(OperationCostRow(
            operation="catch_me_up",
            label="Catch me up & Briefings",
            slot="quality",
            count=0,
            input_tokens=0,
            output_tokens=0,
            est_cost=est,
            is_estimated=est_flag,
            trend_pct=None,
            is_placeholder=True,
        ))
    else:
        prev_cu_cost = (await _catchup_stats(prev_cutoff))[3]
        prev_cost_by_slot["quality"].append(prev_cu_cost)
        operation_rows.append(OperationCostRow(
            operation="catch_me_up",
            label="Catch me up & Briefings",
            slot="quality",
            count=cu_cnt,
            input_tokens=cu_inp,
            output_tokens=cu_out,
            est_cost=cu_cost,
            is_estimated=cu_est,
            trend_pct=_trend(cu_cost, prev_cu_cost),
        ))

    # Subtotals
    real_rows = [r for r in operation_rows if not r.is_placeholder]
    fast_rows = [r for r in real_rows if r.slot == "fast"]
    quality_rows = [r for r in real_rows if r.slot == "quality"]

    def _subtotal(
        rows: list[OperationCostRow], label: str, slot: str,
    ) -> tuple[OperationCostRow, float | None]:
        inp = sum(r.input_tokens for r in rows)
        out = sum(r.output_tokens for r in rows)
        # Summed from the rows rather than re-priced from the tokens: the catch-up
        # row can hold runs from several models and there is no one price for them.
        cost = _sum_costs([r.est_cost for r in rows])
        prev_cost = _sum_costs(prev_cost_by_slot[slot])
        row = OperationCostRow(
            operation=f"_total_{slot}",
            label=label,
            slot=slot,
            count=0,
            input_tokens=inp,
            output_tokens=out,
            est_cost=cost,
            is_estimated=any(r.is_estimated for r in rows),
            trend_pct=_trend(cost, prev_cost),
            row_type="subtotal",
        )
        return row, prev_cost

    # Scoring is the only operation on the fast slot, so its subtotal would just
    # repeat the row. It is still aggregated here, for the grand total.
    fast_total, fast_prev_cost = _subtotal(fast_rows, "Scoring total", "fast")
    quality_total, quality_prev_cost = _subtotal(quality_rows, "Main total", "quality")

    all_inp = fast_total.input_tokens + quality_total.input_tokens
    all_out = fast_total.output_tokens + quality_total.output_tokens
    # A subtotal with usage but no price (unknown provider → no fallback) makes the
    # grand total a lower bound; flag it estimated so it's never shown as exact.
    def _unpriced_usage(row: OperationCostRow) -> bool:
        return row.est_cost is None and (row.input_tokens + row.output_tokens) > 0
    total_cost = (
        (fast_total.est_cost or 0.0) + (quality_total.est_cost or 0.0)
        if fast_total.est_cost is not None or quality_total.est_cost is not None
        else None
    )
    total_prev_cost = (
        (fast_prev_cost or 0.0) + (quality_prev_cost or 0.0)
        if fast_prev_cost is not None or quality_prev_cost is not None
        else None
    )
    grand_total = OperationCostRow(
        operation="_total_all",
        label="Total",
        slot="",
        count=0,
        input_tokens=all_inp,
        output_tokens=all_out,
        est_cost=total_cost,
        is_estimated=(
            fast_total.is_estimated or quality_total.is_estimated
            or _unpriced_usage(fast_total) or _unpriced_usage(quality_total)
        ),
        trend_pct=_trend(total_cost, total_prev_cost),
        row_type="total",
    )

    operation_rows += [quality_total, grand_total]

    return AiCostStats(
        period_days=days,
        operations=operation_rows,
        has_unpriced_provider="custom" in (fast_provider, quality_provider),
        prices_follow_current_model=any(
            r.est_cost is not None and not r.is_placeholder
            for r in operation_rows
        ),
    )
