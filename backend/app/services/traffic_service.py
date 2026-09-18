"""Aggregated visit counts for the public pages, for the admin panel.

What this is *not*: there is no event log, no per-request row, no stored IP and no
cookie. Every request is folded into an in-process counter and only the counts
reach the database. IP and user agent are hashed with a salt that is generated in
memory, never written down and thrown away at midnight; the hash exists to tell two
visits apart within one day and nothing else.

The three shapes it keeps:

- ``page_view_hourly`` — views and identified-bot views per hour and page. Additive,
  so any timezone can be applied at query time.
- ``traffic_source_hourly`` — where the hour's human views came from.
- ``visitor_daily`` — approximate distinct visitors per day. NOT additive, neither
  across hours nor across days; see ``app.models.traffic.VisitorDaily``.

Single-process assumption, the same one the in-memory rate limiter makes: the app
runs with ``--workers 1`` (docker-compose), so this aggregator is the whole of the
instance's traffic. Under several workers each would keep its own counters and its
own visitor sets, and the visitor numbers would be wrong (the same person counted
once per worker) rather than merely approximate.
"""
import hashlib
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.rate_limit import get_client_ip
from app.utils.datetime_format import resolve_tz

logger = logging.getLogger(__name__)

# The public pages, as a fixed list rather than route patterns. Paths carrying a
# token (/verify-email, /reset-password/{token}) must never reach the database even
# by accident, and a fixed list means the stored `path` can hold nothing the client
# chose.
PUBLIC_PATHS = frozenset({
    "/", "/login", "/register", "/features", "/help", "/privacy", "/terms",
})

# visitor_daily.path for "the whole instance" (see the model's warning).
TOTAL_PATH = "*"

# How long the counts are kept. A year-long window compared against the preceding
# year needs twice the window, hence well over 400 days. The volume is tiny: at most
# 168 view rows a day.
RETENTION_DAYS = 800

# Referer is entirely client-controlled, so a single client could otherwise write ten
# thousand distinct hosts into one hour. Past this many distinct sources in an hour
# everything else lands in "other".
MAX_SOURCES_PER_HOUR = 200

# Ceiling on one day's visitor set, so memory can't be inflated either. Above it new
# hashes are dropped and the day is undercounted — for a self-hosted reader this is
# science fiction, but an unbounded set in a long-lived process is not something to
# leave open.
MAX_VISITOR_HASHES = 200_000

# Case-insensitive match on User-Agent. This catches most crawler *volume*, because
# most crawlers say who they are; it does not catch a scraper posing as Chrome. The
# UI calls the column "identified bots" for that reason — it is a lower bound.
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|scrap|curl|wget|python-requests|httpx|go-http|java/|"
    r"libwww|headless|phantom|preview|facebookexternalhit|feedfetcher|feedly|"
    r"inoreader|newsblur|monitoring|uptime|pingdom|semrush|ahrefs|mj12|dotbot|"
    r"petalbot|bytespider|gptbot|claudebot|ccbot|perplexity|applebot|yandex|baidu",
    re.I,
)

_UTM_CLEAN_RE = re.compile(r"[^a-z0-9._-]")
_HOST_CLEAN_RE = re.compile(r"[^a-z0-9.-]")


# ── Process state ─────────────────────────────────────────────────────────────
# Mirrors AppSettings.traffic_stats_enabled so the hot path costs no query. Set at
# startup and again on every admin settings save, like set_ai_enabled.
_enabled: bool = False

# (hour, path) → [views, bot_views]
_views: dict[tuple[datetime, str], list[int]] = {}
# hour → {source: views}. Nested so the per-hour source cap is a len() away.
_sources: dict[datetime, dict[str, int]] = {}
# visitor_daily.path → set of truncated hashes for the current day
_visitors: dict[str, set[bytes]] = {}

# The day the visitor sets belong to, in the instance owner's timezone. None until
# the first flush, which is also what makes the first flush load the baseline.
_day: date | None = None
# Generated at import, not at the first flush: requests arriving in the minute before
# that flush have to be hashed with a real salt too. Replaced on every day rotation
# and never written anywhere.
_salt: bytes = secrets.token_bytes(32)
# What visitor_daily already holds for _day. Without it the first flush after a
# restart would overwrite a whole day with the handful of visits since boot.
_baseline: dict[str, int] = {}

# The owner's timezone changes about once in an instance's life, and the flush runs
# every minute, so looking it up each time is 1440 pointless joins a day. Only the
# lookup is cached: the day itself is still recomputed from the clock on every flush,
# so a rollover is never late. The cost of the cache is that moving the owner's
# timezone takes up to this long to take effect.
_OWNER_TZ_TTL_SECONDS = 600
_owner_tz_cache: tuple[float, str] | None = None


def get_enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


async def apply_enabled(db: AsyncSession, enabled: bool) -> None:
    """Point the mirror at a saved setting, emptying the process when it goes off.

    Switching off is not just a boolean: the flush job stops the moment the flag
    drops, so without a last flush the minute since the previous one is lost, and
    without the discard the day's visitor hashes would sit in memory for the life of
    the process with nothing left to write them to.
    """
    was_enabled = _enabled
    set_enabled(enabled)
    if was_enabled and not enabled:
        try:
            await flush(db)
        except Exception as exc:
            logger.warning("Traffic stats: final flush on disable failed: %s", exc)
        discard_state()


def discard_state() -> None:
    """Drop every in-memory counter, the day's visitor hashes included.

    Called when an admin switches counting off (after a last flush), so the hashes
    do not sit in the process for the rest of its life with nothing left to write
    them. Also how the tests start from a known state.
    """
    global _views, _sources, _visitors, _day, _salt, _baseline, _owner_tz_cache
    _views = {}
    _sources = {}
    _visitors = {}
    _day = None
    _salt = secrets.token_bytes(32)
    _baseline = {}
    _owner_tz_cache = None


# ── Recording ─────────────────────────────────────────────────────────────────

def _is_bot(user_agent: str) -> bool:
    """A missing or empty UA counts as a bot: no browser omits it."""
    if not user_agent.strip():
        return True
    return bool(_BOT_RE.search(user_agent))


def _source_for(request: Request) -> str:
    """Normalized traffic source: a campaign tag, a referring host, or "direct"."""
    utm = request.query_params.get("utm_source")
    if utm:
        value = _UTM_CLEAN_RE.sub("", utm.lower())[:60]
        if value:
            return f"utm:{value}"

    referer = request.headers.get("referer")
    if referer:
        try:
            host = (urlsplit(referer).hostname or "").lower()
        except ValueError:
            host = ""
        host = _HOST_CLEAN_RE.sub("", host).removeprefix("www.")[:80]
        own = (request.url.hostname or "").lower().removeprefix("www.")
        if host and host != own:
            return host

    # Most of the time this is what we get, and it does not mean "typed the address
    # in": a link from an email, a mobile app or an https→http hop sends nothing, and
    # a cross-origin referrer is trimmed to the bare origin by the sending page's
    # policy. The admin page says so next to the table.
    return "direct"


def record(request: Request, response: Response) -> None:
    """Count one request if it is a public page view. Never raises.

    Called from the security-headers middleware, which is the outermost layer, so
    everything below it — including TrustedHost's 400 on a foreign Host — has already
    decided the status code by the time we look.
    """
    if not _enabled:
        return
    try:
        _record_inner(request, response)
    except Exception as exc:  # counting must never take a request down with it
        logger.warning("Traffic stats: failed to record a request: %s", exc)


def _record_inner(request: Request, response: Response) -> None:
    if request.method != "GET":
        return
    path = request.url.path
    if path not in PUBLIC_PATHS:
        return
    if response.status_code != 200:
        return
    if not response.headers.get("content-type", "").startswith("text/html"):
        return
    # An HTMX fragment is someone already on the page, not an arrival.
    if request.headers.get("HX-Request"):
        return
    # Signed-in traffic is out of scope: /features, /help, /privacy and /terms have no
    # auth guard and answer 200 to anyone, so without this our own users would be
    # inflating "public page traffic". The raw cookie, not request.session — the
    # session middleware sits further in than this call site — and its validity does
    # not matter here. Bots don't send one.
    if request.cookies.get("session"):
        return

    user_agent = request.headers.get("user-agent", "")
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    counts = _views.setdefault((hour, path), [0, 0])

    if _is_bot(user_agent):
        counts[1] += 1
        # Bots stop here: they would land in "direct" almost without exception and
        # swamp the one source category that says the least.
        return
    counts[0] += 1

    bucket = _sources.setdefault(hour, {})
    source = _source_for(request)
    if source not in bucket and len(bucket) >= MAX_SOURCES_PER_HOUR:
        source = "other"
    bucket[source] = bucket.get(source, 0) + 1

    digest = hashlib.sha256(
        _salt + get_client_ip(request).encode() + user_agent.encode()
    ).digest()[:16]
    for key in (TOTAL_PATH, path):
        seen = _visitors.setdefault(key, set())
        if len(seen) < MAX_VISITOR_HASHES:
            seen.add(digest)


# ── Flush ─────────────────────────────────────────────────────────────────────

async def _owner_timezone(db: AsyncSession) -> str:
    """The instance owner's timezone: the oldest active admin, i.e. the one
    ``seed_first_admin`` created. UTC when there is none — an instance without an
    active admin must not take the daily rotation, and with it the whole flush, down.

    The answer is resolved through ``resolve_tz`` before it is returned, so the name
    that goes on to ``AT TIME ZONE`` in the query layer is one Python accepted. The
    two would otherwise disagree about a bad value: Python falls back to UTC, while
    Postgres raises and takes the admin page with it.
    """
    global _owner_tz_cache
    now = time.monotonic()
    if _owner_tz_cache and now - _owner_tz_cache[0] < _OWNER_TZ_TTL_SECONDS:
        return _owner_tz_cache[1]

    row = await db.execute(text("""
        SELECT us.timezone
        FROM users u
        JOIN user_settings us ON us.user_id = u.id
        WHERE u.role = 'admin' AND u.is_active
        ORDER BY u.id
        LIMIT 1
    """))
    tz = str(resolve_tz(row.scalar()))
    _owner_tz_cache = (now, tz)
    return tz


async def _load_baseline(db: AsyncSession, day: date) -> dict[str, int]:
    rows = await db.execute(
        text("SELECT path, visitors FROM visitor_daily WHERE day = :day"),
        {"day": day},
    )
    return {r.path: int(r.visitors) for r in rows}


async def _write_views(
    db: AsyncSession,
    views: dict[tuple[datetime, str], list[int]],
    sources: dict[datetime, dict[str, int]],
) -> None:
    for (hour, path), (n, bots) in views.items():
        await db.execute(
            text("""
                INSERT INTO page_view_hourly (hour, path, views, bot_views)
                VALUES (:hour, :path, :views, :bots)
                ON CONFLICT (hour, path) DO UPDATE
                SET views = page_view_hourly.views + EXCLUDED.views,
                    bot_views = page_view_hourly.bot_views + EXCLUDED.bot_views
            """),
            {"hour": hour, "path": path, "views": n, "bots": bots},
        )
    for hour, bucket in sources.items():
        for source, n in bucket.items():
            await db.execute(
                text("""
                    INSERT INTO traffic_source_hourly (hour, source, views)
                    VALUES (:hour, :source, :views)
                    ON CONFLICT (hour, source) DO UPDATE
                    SET views = traffic_source_hourly.views + EXCLUDED.views
                """),
                {"hour": hour, "source": source, "views": n},
            )


def _merge_back(
    views: dict[tuple[datetime, str], list[int]],
    sources: dict[datetime, dict[str, int]],
) -> None:
    """Put counters that failed to persist back into the live dicts."""
    for key, (n, bots) in views.items():
        counts = _views.setdefault(key, [0, 0])
        counts[0] += n
        counts[1] += bots
    for hour, bucket in sources.items():
        target = _sources.setdefault(hour, {})
        for source, n in bucket.items():
            target[source] = target.get(source, 0) + n


async def _write_visitors(
    db: AsyncSession, day: date, baseline: dict[str, int], sets: dict[str, set[bytes]]
) -> None:
    """Write the absolute visitor count for ``day``.

    Absolute, not an increment, because the sets are not emptied — they have to live
    the whole day for a returning visitor to stay one visitor. ``baseline`` is what
    the row already held when this process took over the day, so a restart at 18:00
    adds to the day instead of replacing it with the evening's traffic. It does mean a
    restart overcounts slightly (whoever comes back afterwards is counted twice); the
    admin page says so. Never going backwards is the property that matters.
    """
    # A snapshot of the keys: unlike the view counters these sets are the live ones
    # (they have to survive the whole day), so a first-ever visit to a page during
    # this write would add a key and break the iteration.
    for path, seen in list(sets.items()):
        await db.execute(
            text("""
                INSERT INTO visitor_daily (day, path, visitors)
                VALUES (:day, :path, :visitors)
                ON CONFLICT (day, path) DO UPDATE SET visitors = EXCLUDED.visitors
            """),
            {"day": day, "path": path, "visitors": baseline.get(path, 0) + len(seen)},
        )


async def flush(db: AsyncSession) -> None:
    """Persist the in-memory counters. Runs every minute and again on shutdown."""
    global _views, _sources, _day, _baseline

    if _day is None:
        _day = await _current_day(db)
        _baseline = await _load_baseline(db, _day)

    # Swap before the await, not after. Anything recorded while the write is in
    # flight lands in the fresh dicts and is picked up by the next flush; clearing
    # afterwards would drop it on the floor.
    views, _views = _views, {}
    sources, _sources = _sources, {}
    if views or sources:
        try:
            await _write_views(db, views, sources)
            await db.commit()
        except Exception as exc:
            logger.warning("Traffic stats: view flush failed, retrying next run: %s", exc)
            await db.rollback()
            _merge_back(views, sources)

    await _rotate_day_if_needed(db)

    if _visitors:
        try:
            await _write_visitors(db, _day, _baseline, _visitors)
            await db.commit()
        except Exception as exc:
            logger.warning("Traffic stats: visitor flush failed: %s", exc)
            await db.rollback()


async def _current_day(db: AsyncSession) -> date:
    return datetime.now(resolve_tz(await _owner_timezone(db))).date()


async def _rotate_day_if_needed(db: AsyncSession) -> None:
    """Close out the previous day's visitor sets and start fresh ones.

    Forward only. If the owner moves their timezone from Tokyo to Los Angeles the
    computed day drops, and rotating backwards would write today's set over
    yesterday's finished row.
    """
    global _visitors, _day, _salt, _baseline

    today = await _current_day(db)
    if _day is None or today <= _day:
        return

    try:
        await _write_visitors(db, _day, _baseline, _visitors)
        await db.commit()
    except Exception as exc:
        # Rotate anyway: the previous day keeps the value written a minute ago, which
        # is a minute stale at worst. Holding the sets open until the database comes
        # back would instead pour the new day's visitors into the old day's row.
        logger.warning("Traffic stats: could not close out %s: %s", _day, exc)
        await db.rollback()

    _day = today
    _visitors = {}
    _salt = secrets.token_bytes(32)
    try:
        _baseline = await _load_baseline(db, today)
    except Exception as exc:
        logger.warning("Traffic stats: could not load the baseline for %s: %s", today, exc)
        _baseline = {}


async def purge_old(db: AsyncSession) -> None:
    """Delete counts past the retention horizon."""
    cutoff_hour = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff_day = cutoff_hour.date()
    total = 0
    for stmt, params in (
        ("DELETE FROM page_view_hourly WHERE hour < :cutoff", {"cutoff": cutoff_hour}),
        ("DELETE FROM traffic_source_hourly WHERE hour < :cutoff", {"cutoff": cutoff_hour}),
        ("DELETE FROM visitor_daily WHERE day < :cutoff", {"cutoff": cutoff_day}),
    ):
        result = await db.execute(text(stmt), params)
        total += result.rowcount or 0
    await db.commit()
    if total:
        logger.info("Purged %d traffic stat rows older than %d days", total, RETENTION_DAYS)


# ── Queries for the admin page ────────────────────────────────────────────────

WINDOW_PRESETS = (7, 30, 90, 365)
# Above this many days the daily bars stop being readable, so they are grouped by week.
_WEEKLY_ABOVE_DAYS = 90


def _pct_change(current: float, previous: float) -> float | None:
    """Percent change, or None when there is no previous period to compare against."""
    if not previous:
        return None
    return (current - previous) / previous * 100


async def get_recent_views(db: AsyncSession, days: int = 7) -> int:
    """Page views over the last ``days`` days, for the dashboard tile. A sum, which
    views tolerate — the tile deliberately doesn't show visitors, since an average
    needs the explanation the traffic page has room for."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = await db.scalar(
        text("SELECT SUM(views) FROM page_view_hourly WHERE hour >= :cutoff"),
        {"cutoff": cutoff},
    )
    return int(total or 0)


async def get_traffic_overview(db: AsyncSession, days: int) -> dict:
    """Everything ``/admin/traffic`` renders, in the instance owner's timezone.

    One timezone for the whole page, the owner's, even for another admin looking at
    it: views could be folded into any zone but visitors are fixed to the owner's, and
    two charts on different days would line up against each other for no visible
    reason.

    Views are summed; visitors are never summed over the window (see the model). The
    window is compared against the immediately preceding one of the same length, which
    retention is sized to keep available even for a year.
    """
    tz = await _owner_timezone(db)
    today = datetime.now(resolve_tz(tz)).date()
    start = today - timedelta(days=days - 1)
    prev_start = start - timedelta(days=days)
    # Generous lower bound for the primary-key range scan; the exact day boundaries
    # are applied on the timezone-converted date, so DST can't shave a day off.
    lower = datetime.combine(
        prev_start - timedelta(days=1), datetime.min.time(), tzinfo=resolve_tz(tz)
    ).astimezone(timezone.utc)

    view_rows = (await db.execute(
        text("""
            SELECT (hour AT TIME ZONE :tz)::date AS d,
                   SUM(views) AS views,
                   SUM(bot_views) AS bot_views
            FROM page_view_hourly
            WHERE hour >= :lower
            GROUP BY 1
        """),
        {"tz": tz, "lower": lower},
    )).fetchall()
    views_by_day = {r.d: int(r.views or 0) for r in view_rows}
    bots_by_day = {r.d: int(r.bot_views or 0) for r in view_rows}

    # path = '*' only: the table also holds one row per page and they overlap
    # completely, so an unfiltered sum would roughly double the instance total.
    visitor_rows = (await db.execute(
        text("""
            SELECT day, visitors FROM visitor_daily
            WHERE day >= :prev_start AND path = :total
        """),
        {"prev_start": prev_start, "total": TOTAL_PATH},
    )).fetchall()
    visitors_by_day = {r.day: int(r.visitors or 0) for r in visitor_rows}

    def _window(source: dict, first: date, last: date) -> list[int]:
        span = (last - first).days + 1
        return [source.get(first + timedelta(days=i), 0) for i in range(span)]

    visitors = _window(visitors_by_day, start, today)
    views = _window(views_by_day, start, today)
    bots = _window(bots_by_day, start, today)
    prev_visitors = _window(visitors_by_day, prev_start, start - timedelta(days=1))
    prev_views = _window(views_by_day, prev_start, start - timedelta(days=1))
    prev_bots = _window(bots_by_day, prev_start, start - timedelta(days=1))

    peak = max(visitors) if visitors else 0
    peak_day = (
        start + timedelta(days=visitors.index(peak)) if peak else None
    )

    visitors_avg = sum(visitors) / days
    prev_visitors_avg = sum(prev_visitors) / days

    return {
        "days": days,
        "presets": WINDOW_PRESETS,
        "timezone": tz,
        "start": start,
        "end": today,
        "visitors_avg": visitors_avg,
        "visitors_avg_change": _pct_change(visitors_avg, prev_visitors_avg),
        "visitors_peak": peak,
        "visitors_peak_day": peak_day,
        # How many different people the window saw cannot be recovered from daily
        # counts (the sets are collapsed to a number when they are written), but the
        # counts do bracket it. Not fewer than the busiest single day, since those
        # people were all here at once; not more than every day added up, since that
        # counts a regular once per day. Both bounds are tight at their extreme, so
        # where the truth sits inside says whether the audience returns or arrives.
        "visitors_floor": peak,
        "visitors_ceiling": sum(visitors),
        "views_total": sum(views),
        "views_change": _pct_change(sum(views), sum(prev_views)),
        "bot_total": sum(bots),
        "bot_change": _pct_change(sum(bots), sum(prev_bots)),
        "chart": _build_chart(start, visitors, views, days),
        "chart_weekly": days > _WEEKLY_ABOVE_DAYS,
        "pages": await _top_pages(db, tz, lower, start, days),
        "sources": await _top_sources(db, tz, lower, start),
        "funnel": await _funnel(db, tz, lower, start),
        "has_data": bool(sum(views) or sum(bots)),
    }


def _build_chart(
    start: date, visitors: list[int], views: list[int], days: int
) -> list[dict]:
    """Bars for the daily chart, grouped by week once a window gets long."""
    step = 7 if days > _WEEKLY_ABOVE_DAYS else 1
    bars = []
    for i in range(0, len(visitors), step):
        chunk_visitors = visitors[i:i + step]
        chunk_views = views[i:i + step]
        first = start + timedelta(days=i)
        bars.append({
            "date": first,
            "last_date": first + timedelta(days=len(chunk_visitors) - 1),
            # Visitors still don't add up across days, so a weekly bar shows the
            # average of its days, on the same scale as a daily bar.
            "visitors": round(sum(chunk_visitors) / len(chunk_visitors)),
            "views": sum(chunk_views),
        })
    return bars


async def _top_pages(
    db: AsyncSession, tz: str, lower: datetime, start: date, days: int
) -> list[dict]:
    rows = (await db.execute(
        text("""
            SELECT path, SUM(views) AS views, SUM(bot_views) AS bot_views
            FROM page_view_hourly
            WHERE hour >= :lower AND (hour AT TIME ZONE :tz)::date >= :start
            GROUP BY path
            ORDER BY views DESC
        """),
        {"tz": tz, "lower": lower, "start": start},
    )).fetchall()
    # path <> '*': per-page rows only, or the instance total would show up as a page.
    visitor_rows = (await db.execute(
        text("""
            SELECT path, SUM(visitors) AS total FROM visitor_daily
            WHERE day >= :start AND path <> :total
            GROUP BY path
        """),
        {"start": start, "total": TOTAL_PATH},
    )).fetchall()
    per_day = {r.path: int(r.total or 0) / days for r in visitor_rows}
    return [
        {
            "path": r.path,
            "views": int(r.views or 0),
            "bot_views": int(r.bot_views or 0),
            "visitors_avg": per_day.get(r.path, 0.0),
        }
        for r in rows
    ]


async def _top_sources(
    db: AsyncSession, tz: str, lower: datetime, start: date
) -> list[dict]:
    rows = (await db.execute(
        text("""
            SELECT source, SUM(views) AS views
            FROM traffic_source_hourly
            WHERE hour >= :lower AND (hour AT TIME ZONE :tz)::date >= :start
            GROUP BY source
            ORDER BY views DESC
            LIMIT 20
        """),
        {"tz": tz, "lower": lower, "start": start},
    )).fetchall()
    return [{"source": r.source, "views": int(r.views or 0)} for r in rows]


async def _funnel(db: AsyncSession, tz: str, lower: datetime, start: date) -> dict:
    """Landing → register page → account created. All three are sums, which is fine:
    these are views and rows, not visitors.

    The last step needs no tracking of its own — it is the users table. That is also
    what makes it the one step that can reach further back than the others: views
    begin the hour counting was switched on, while the users table goes back to the
    instance's first day, so a window that starts before that hour would put accounts
    against visits that were never recorded and read as more sign-ups than visitors.
    Sign-ups are therefore counted from the first recorded view, and the page says so
    when that cuts the window short.
    """
    rows = (await db.execute(
        text("""
            SELECT path, SUM(views) AS views
            FROM page_view_hourly
            WHERE hour >= :lower AND (hour AT TIME ZONE :tz)::date >= :start
              AND path IN ('/', '/register')
            GROUP BY path
        """),
        {"tz": tz, "lower": lower, "start": start},
    )).fetchall()
    by_path = {r.path: int(r.views or 0) for r in rows}

    zone = resolve_tz(tz)
    window_start = datetime.combine(start, datetime.min.time(), tzinfo=zone)
    first_hour = await db.scalar(text("SELECT MIN(hour) FROM page_view_hourly"))
    clipped = first_hour is not None and first_hour > window_start
    since = first_hour if clipped else window_start
    signups = await db.scalar(
        text("SELECT COUNT(*) FROM users WHERE created_at >= :since"),
        {"since": since},
    )
    return {
        "landing": by_path.get("/", 0),
        "register": by_path.get("/register", 0),
        "signups": int(signups or 0),
        # None unless counting started inside the window, in which case this is the
        # day the sign-up step actually covers.
        "signups_from": since.astimezone(zone).date() if clipped else None,
    }
