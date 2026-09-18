"""Tests for public-page traffic counting (``app.services.traffic_service``).

Two halves. The first drives ``record()`` with real Request/Response objects and
checks what does and does not end up in the in-memory counters — that is where the
"public pages only" scope and the "nothing client-supplied is stored" property live.
The second drives ``flush()`` against a fake session that behaves like the three
tables, plus a couple of integration tests against the real database for the queries
whose whole point is a SQL filter.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings as app_settings
from app.services import traffic_service as ts
from app.utils.datetime_format import resolve_tz

CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


@pytest.fixture(autouse=True)
def enabled():
    ts.discard_state()
    ts.set_enabled(True)
    yield
    ts.discard_state()
    ts.set_enabled(False)


def make_request(path="/", *, method="GET", query="", headers=None, ip="203.0.113.9"):
    hdrs = {"user-agent": CHROME_UA}
    hdrs.update(headers or {})
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
        "client": (ip, 51234),
        "server": ("readfine.test", 443),
    })


def html(status=200):
    return Response("<html></html>", status_code=status, media_type="text/html")


def total_views() -> int:
    return sum(v for v, _ in ts._views.values())


def total_bots() -> int:
    return sum(b for _, b in ts._views.values())


def visitors(path=ts.TOTAL_PATH) -> int:
    return len(ts._visitors.get(path, ()))


def sources() -> dict[str, int]:
    merged: dict[str, int] = {}
    for bucket in ts._sources.values():
        for source, n in bucket.items():
            merged[source] = merged.get(source, 0) + n
    return merged


# ── What counts ───────────────────────────────────────────────────────────────

def test_public_page_view_is_counted():
    ts.record(make_request("/features"), html())
    assert total_views() == 1
    assert visitors() == 1
    assert visitors("/features") == 1


def test_disabled_records_nothing():
    ts.set_enabled(False)
    ts.record(make_request("/"), html())
    assert ts._views == {}
    assert ts._visitors == {}


@pytest.mark.parametrize("path", [
    "/app", "/settings", "/settings/stats", "/admin", "/admin/traffic",
    "/api/v1/articles", "/verify-email", "/reset-password/s3cr3t-token",
    "/static/css/tailwind.css", "/healthz",
])
def test_only_the_public_allowlist_is_counted(path):
    ts.record(make_request(path), html())
    assert ts._views == {}
    # The regression this guards: a token in the URL reaching a stored column.
    assert not any("token" in stored for _, stored in ts._views)


def test_non_200_is_not_counted():
    # A signed-in visitor bounced off "/" to /app, and a foreign Host stopped at
    # TrustedHost — both reach this middleware with the status already decided.
    ts.record(make_request("/"), Response(status_code=302, media_type="text/html"))
    ts.record(make_request("/"), Response(status_code=400, media_type="text/html"))
    assert ts._views == {}


def test_non_html_is_not_counted():
    ts.record(make_request("/"), Response("{}", status_code=200, media_type="application/json"))
    assert ts._views == {}


def test_htmx_fragment_is_not_counted():
    ts.record(make_request("/login", headers={"HX-Request": "true"}), html())
    assert ts._views == {}


def test_signed_in_request_is_not_counted():
    """/features and /help answer 200 to anyone, signed in or not, so without the
    cookie check our own users would be inflating "public page traffic"."""
    ts.record(make_request("/features", headers={"cookie": "session=abc.def"}), html())
    assert ts._views == {}
    assert ts._visitors == {}


def test_post_is_not_counted():
    ts.record(make_request("/login", method="POST"), html())
    assert ts._views == {}


# ── Bots ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "python-requests/2.32.0",
    "curl/8.5.0",
    "Mozilla/5.0 (compatible; SemrushBot/7~bl)",
    "facebookexternalhit/1.1",
    "",
    "   ",
])
def test_bots_go_to_their_own_column(ua):
    ts.record(make_request("/", headers={"user-agent": ua}), html())
    assert total_views() == 0
    assert total_bots() == 1
    # Not a visitor and not a source: a bot rarely sends a referrer, and letting
    # them in would pile the whole crawl into "direct".
    assert ts._visitors == {}
    assert ts._sources == {}


def test_a_browser_is_not_a_bot():
    ts.record(make_request("/"), html())
    assert total_views() == 1
    assert total_bots() == 0


# ── Sources ───────────────────────────────────────────────────────────────────

def test_no_referrer_is_direct():
    ts.record(make_request("/"), html())
    assert sources() == {"direct": 1}


def test_referrer_host_is_normalized():
    ts.record(
        make_request("/", headers={"referer": "https://WWW.Example.COM/some/thread?x=1#frag"}),
        html(),
    )
    assert sources() == {"example.com": 1}


def test_our_own_host_is_not_a_source():
    ts.record(make_request("/register", headers={"referer": "https://readfine.test/login"}), html())
    assert sources() == {"direct": 1}


def test_utm_source_wins_over_the_referrer():
    ts.record(
        make_request("/", query="utm_source=Hacker+News", headers={"referer": "https://x.invalid/"}),
        html(),
    )
    assert sources() == {"utm:hackernews": 1}


def test_client_supplied_source_values_are_sanitized_and_capped():
    """Referer and utm_source are both entirely under the client's control, so this
    is the test standing in for "nothing that arrived from outside is stored"."""
    ts.record(make_request("/", query="utm_source=" + "a" * 500), html())
    ts.record(make_request("/", query="utm_source=drop%20table;--%3Cscript%3E"), html())
    ts.record(make_request("/", headers={"referer": "https://" + "b" * 400 + ".invalid/x"}), html())
    ts.record(make_request("/", headers={"referer": "https://ex.invalid/\x00\x07<script>"}), html())
    ts.record(make_request("/", headers={"referer": "not a url at all"}), html())

    for stored in sources():
        assert len(stored) <= 80, stored
        body = stored.removeprefix("utm:")
        assert all(c.isalnum() and c.islower() or c in "._-" for c in body), stored


def test_source_count_per_hour_is_capped():
    for i in range(300):
        ts.record(make_request("/", headers={"referer": f"https://ref{i}.invalid/"}), html())
    bucket = next(iter(ts._sources.values()))
    assert len(bucket) == ts.MAX_SOURCES_PER_HOUR + 1  # the cap, plus "other"
    assert bucket["other"] == 100


# ── Visitors ──────────────────────────────────────────────────────────────────

def test_same_ip_and_browser_is_one_visitor():
    for _ in range(5):
        ts.record(make_request("/", ip="198.51.100.4"), html())
    assert total_views() == 5
    assert visitors() == 1


def test_different_ip_or_browser_is_another_visitor():
    ts.record(make_request("/", ip="198.51.100.4"), html())
    ts.record(make_request("/", ip="198.51.100.5"), html())
    ts.record(make_request("/", ip="198.51.100.4", headers={"user-agent": "Firefox/130.0"}), html())
    assert visitors() == 3


def test_visitor_sets_are_per_page_as_well_as_total():
    ts.record(make_request("/", ip="198.51.100.4"), html())
    ts.record(make_request("/help", ip="198.51.100.4"), html())
    ts.record(make_request("/help", ip="198.51.100.7"), html())
    assert visitors() == 2
    assert visitors("/") == 1
    assert visitors("/help") == 2


# ── Wiring ────────────────────────────────────────────────────────────────────
# Everything above drives record() directly, which says nothing about whether the
# app ever calls it. The whole feature hangs on one line in the security-headers
# middleware, and without these two the line can be deleted with the suite staying
# green.

def test_the_middleware_counts_a_real_public_page(unauth_client):
    resp = unauth_client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert total_views() == 1
    assert visitors("/login") == 1


def test_the_middleware_leaves_everything_else_alone(unauth_client):
    unauth_client.get("/healthz")
    unauth_client.get("/static/js/ai-settings.js")
    assert ts._views == {}


def test_record_never_raises(monkeypatch):
    monkeypatch.setattr(ts, "_record_inner", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    ts.record(make_request("/"), html())  # must not propagate


# ── Fake session ──────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar
        self.rowcount = len(self._rows)

    def scalar(self):
        return self._scalar

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """Enough of an AsyncSession to run flush() against, standing in for the three
    tables with dicts that apply the same ON CONFLICT semantics."""

    def __init__(self, tz="UTC"):
        self.tz = tz
        self.tz_queries = 0
        self.views: dict[tuple[datetime, str], list[int]] = {}
        self.sources: dict[tuple[datetime, str], int] = {}
        self.visitors: dict[tuple[date, str], int] = {}
        self.fail_writes = False
        self.on_write = None          # fires once, before the first write
        self.on_visitor_write = None  # fires once, before the first visitor_daily write
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        params = params or {}
        if "us.timezone" in sql:
            self.tz_queries += 1
            return FakeResult(scalar=self.tz)
        if sql.startswith("SELECT path, visitors FROM visitor_daily"):
            rows = [
                SimpleNamespace(path=p, visitors=v)
                for (d, p), v in self.visitors.items()
                if d == params["day"]
            ]
            return FakeResult(rows=rows)
        if self.on_write:
            hook, self.on_write = self.on_write, None
            hook()
        if self.fail_writes:
            raise RuntimeError("database is gone")
        if "INSERT INTO page_view_hourly" in sql:
            counts = self.views.setdefault((params["hour"], params["path"]), [0, 0])
            counts[0] += params["views"]
            counts[1] += params["bots"]
        elif "INSERT INTO traffic_source_hourly" in sql:
            key = (params["hour"], params["source"])
            self.sources[key] = self.sources.get(key, 0) + params["views"]
        elif "INSERT INTO visitor_daily" in sql:
            if self.on_visitor_write:
                hook, self.on_visitor_write = self.on_visitor_write, None
                hook()
            self.visitors[(params["day"], params["path"])] = params["visitors"]
        else:
            raise AssertionError(f"unexpected statement: {sql}")
        return FakeResult()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def today_in(tz: str) -> date:
    return datetime.now(resolve_tz(tz)).date()


# ── Flush ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_writes_views_sources_and_visitors():
    db = FakeSession()
    ts.record(make_request("/", headers={"referer": "https://news.invalid/"}), html())
    ts.record(make_request("/", headers={"user-agent": "Googlebot/2.1"}), html())
    await ts.flush(db)

    assert list(db.views.values()) == [[1, 1]]
    assert list(db.sources.values()) == [1]
    assert db.visitors[(today_in("UTC"), ts.TOTAL_PATH)] == 1
    assert ts._views == {}  # drained


@pytest.mark.asyncio
async def test_flush_does_not_lose_a_request_that_arrives_mid_write():
    """The counters are swapped for empty ones *before* the await, so anything
    recorded while the write is in flight lands in the next batch instead of being
    cleared away underneath it."""
    db = FakeSession()
    ts.record(make_request("/"), html())
    db.on_write = lambda: ts.record(make_request("/login"), html())

    await ts.flush(db)
    assert sum(v for v, _ in db.views.values()) == 1
    assert total_views() == 1  # the /login hit survived in memory

    await ts.flush(db)
    assert sum(v for v, _ in db.views.values()) == 2


@pytest.mark.asyncio
async def test_a_new_page_visited_mid_write_does_not_break_the_visitor_write():
    """The visitor sets are the live ones (they have to last the whole day), so a
    first-ever visit to a page arriving during the write adds a key to the dict
    being iterated."""
    db = FakeSession()
    ts.record(make_request("/", ip="198.51.100.1"), html())
    db.on_visitor_write = lambda: ts.record(make_request("/terms", ip="198.51.100.2"), html())

    await ts.flush(db)
    day = today_in("UTC")
    # The whole pass has to complete: the added key must not cut the loop short
    # after the first row.
    assert db.visitors[(day, ts.TOTAL_PATH)] == 1
    assert db.visitors[(day, "/")] == 1
    assert not db.rollbacks

    await ts.flush(db)
    assert db.visitors[(day, ts.TOTAL_PATH)] == 2
    assert db.visitors[(day, "/terms")] == 1


@pytest.mark.asyncio
async def test_failed_flush_keeps_the_counters_for_the_next_run():
    db = FakeSession()
    ts.record(make_request("/"), html())
    ts.record(make_request("/"), html())
    db.fail_writes = True

    await ts.flush(db)
    assert db.views == {}
    assert db.rollbacks
    assert total_views() == 2

    db.fail_writes = False
    await ts.flush(db)
    assert sum(v for v, _ in db.views.values()) == 2


@pytest.mark.asyncio
async def test_restart_adds_to_the_day_instead_of_overwriting_it():
    """A deploy at 18:00 must not cut the day down to the evening's traffic: the
    flush reads what the row already holds and writes baseline + set."""
    day = today_in("UTC")
    db = FakeSession()
    db.visitors[(day, ts.TOTAL_PATH)] = 40

    ts.record(make_request("/", ip="198.51.100.1"), html())
    ts.record(make_request("/", ip="198.51.100.2"), html())
    await ts.flush(db)

    assert db.visitors[(day, ts.TOTAL_PATH)] == 42


@pytest.mark.asyncio
async def test_day_rotation_closes_the_old_day_and_starts_a_new_set():
    db = FakeSession()
    ts.record(make_request("/", ip="198.51.100.1"), html())
    await ts.flush(db)

    yesterday = ts._day - timedelta(days=1)
    ts._day = yesterday  # pretend the process has been up since yesterday
    ts.record(make_request("/", ip="198.51.100.2"), html())
    await ts.flush(db)

    assert db.visitors[(yesterday, ts.TOTAL_PATH)] == 2
    assert ts._day == today_in("UTC")
    assert ts._visitors == {}


@pytest.mark.asyncio
async def test_rotation_never_goes_backwards():
    """Moving the owner's timezone from Tokyo to Los Angeles lowers the computed
    day. Rotating on that would write the live set over a finished day."""
    db = FakeSession()
    day = today_in("UTC")
    db.visitors[(day, ts.TOTAL_PATH)] = 99

    await ts.flush(db)
    ts._day = day + timedelta(days=1)  # the process is "ahead" after the move back
    ts._baseline = {}
    ts.record(make_request("/", ip="198.51.100.3"), html())
    await ts.flush(db)

    assert db.visitors[(day, ts.TOTAL_PATH)] == 99
    assert db.visitors[(day + timedelta(days=1), ts.TOTAL_PATH)] == 1


@pytest.mark.asyncio
async def test_flush_survives_an_instance_with_no_active_admin():
    """No owner means no timezone; UTC has to stand in, or the day rotation takes
    the whole flush down with it."""
    db = FakeSession(tz=None)
    ts.record(make_request("/"), html())
    await ts.flush(db)
    assert db.visitors[(today_in("UTC"), ts.TOTAL_PATH)] == 1


@pytest.mark.asyncio
async def test_the_owner_timezone_is_not_looked_up_on_every_flush():
    """The flush runs once a minute and the owner's timezone changes about once in
    an instance's life, so the lookup is cached."""
    db = FakeSession()
    for _ in range(5):
        ts.record(make_request("/"), html())
        await ts.flush(db)
    assert db.tz_queries == 1


# ── Switching it off ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switching_off_writes_the_last_minute_and_empties_the_process():
    """The flush job stops the moment the flag drops, so anything still in memory
    would never be written, and the day's hashes would stay there for the life of
    the process with nothing left to do with them."""
    db = FakeSession()
    ts.record(make_request("/", ip="198.51.100.1"), html())
    ts.record(make_request("/", ip="198.51.100.2"), html())

    await ts.apply_enabled(db, False)

    assert db.visitors[(today_in("UTC"), ts.TOTAL_PATH)] == 2
    assert sum(v for v, _ in db.views.values()) == 2
    assert not ts.get_enabled()
    assert ts._visitors == {}
    assert ts._views == {}


@pytest.mark.asyncio
async def test_switching_off_empties_the_process_even_if_the_write_fails():
    db = FakeSession()
    ts.record(make_request("/", ip="198.51.100.1"), html())
    db.fail_writes = True

    await ts.apply_enabled(db, False)

    assert ts._visitors == {}
    assert ts._views == {}


@pytest.mark.asyncio
async def test_switching_on_touches_nothing():
    db = FakeSession()
    ts.set_enabled(False)
    await ts.apply_enabled(db, True)
    assert ts.get_enabled()
    assert db.commits == 0


# ── Against the real database ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _clear_window(pg, day: date):
    await pg.execute(text("DELETE FROM visitor_daily WHERE day >= :d"), {"d": day - timedelta(days=400)})
    await pg.execute(
        text("DELETE FROM page_view_hourly WHERE hour >= :h"),
        {"h": datetime.now(timezone.utc) - timedelta(days=400)},
    )


@pytest.mark.asyncio
async def test_overview_does_not_add_the_total_to_the_pages(pg):
    """visitor_daily holds both '*' (the instance) and one row per page, and they
    overlap completely — an unfiltered SUM roughly doubles the number and looks
    entirely believable."""
    tz = await ts._owner_timezone(pg)
    day = today_in(tz)
    await _clear_window(pg, day)
    for path, n in ((ts.TOTAL_PATH, 10), ("/", 7), ("/login", 5)):
        await pg.execute(
            text("INSERT INTO visitor_daily (day, path, visitors) VALUES (:d, :p, :v)"),
            {"d": day, "p": path, "v": n},
        )

    data = await ts.get_traffic_overview(pg, days=7)

    assert data["visitors_peak"] == 10        # not 22
    assert data["visitors_avg"] == 10 / 7
    assert [p["path"] for p in data["pages"]] == []  # no views recorded, so no page rows


@pytest.mark.asyncio
async def test_window_visitors_are_bracketed_rather_than_summed(pg):
    """The daily counts cannot say how many different people a window saw, but they
    bound it: not fewer than the busiest day, not more than every day added up."""
    tz = await ts._owner_timezone(pg)
    day = today_in(tz)
    await _clear_window(pg, day)
    for offset, n in ((0, 10), (1, 4), (2, 7)):
        await pg.execute(
            text("INSERT INTO visitor_daily (day, path, visitors) VALUES (:d, '*', :v)"),
            {"d": day - timedelta(days=offset), "v": n},
        )

    data = await ts.get_traffic_overview(pg, days=7)

    assert data["visitors_floor"] == 10
    assert data["visitors_ceiling"] == 21
    assert data["visitors_avg"] == 21 / 7


@pytest.mark.asyncio
async def test_overview_sums_views_and_compares_with_the_previous_window(pg):
    tz = await ts._owner_timezone(pg)
    day = today_in(tz)
    await _clear_window(pg, day)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for offset, views, bots in ((0, 10, 3), (1, 5, 0), (9, 4, 0)):
        await pg.execute(
            text("""INSERT INTO page_view_hourly (hour, path, views, bot_views)
                    VALUES (:h, '/', :v, :b)"""),
            {"h": now - timedelta(days=offset), "v": views, "b": bots},
        )

    data = await ts.get_traffic_overview(pg, days=7)

    assert data["views_total"] == 15
    assert data["bot_total"] == 3
    assert data["views_change"] == pytest.approx((15 - 4) / 4 * 100)
    assert data["pages"][0]["path"] == "/"
    assert data["pages"][0]["views"] == 15


@pytest.mark.asyncio
async def test_signups_are_counted_from_the_first_recorded_view(pg):
    """Counting was switched on long after the instance opened, so the users table
    reaches back further than the views do. Accounts created before the first
    recorded view are left out, or the last funnel step ends up above the first."""
    tz = await ts._owner_timezone(pg)
    day = today_in(tz)
    await _clear_window(pg, day)
    await pg.execute(text("DELETE FROM page_view_hourly"))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await pg.execute(
        text("""INSERT INTO page_view_hourly (hour, path, views, bot_views)
                VALUES (:h, '/register', 20, 0)"""),
        {"h": now - timedelta(days=2)},
    )

    async def signups() -> dict:
        return (await ts.get_traffic_overview(pg, days=7))["funnel"]

    async def add_user(days_ago: int) -> None:
        await pg.execute(
            text("""INSERT INTO users (email, password_hash, display_name, created_at,
                                       updated_at)
                    VALUES (:e, 'x', 'x', :t, :t)"""),
            {"e": f"{uuid.uuid4().hex}@example.test", "t": now - timedelta(days=days_ago)},
        )

    before = (await signups())["signups"]
    await add_user(5)          # predates counting
    assert (await signups())["signups"] == before
    await add_user(1)          # inside the counted period
    funnel = await signups()
    assert funnel["signups"] == before + 1
    assert funnel["signups_from"] == (now - timedelta(days=2)).astimezone(
        resolve_tz(tz)
    ).date()


@pytest.mark.asyncio
async def test_a_window_inside_the_counted_period_is_not_clipped(pg):
    """The note only shows when counting started mid-window; once the whole window
    is covered, sign-ups run from the window's own start."""
    tz = await ts._owner_timezone(pg)
    day = today_in(tz)
    await _clear_window(pg, day)
    await pg.execute(text("DELETE FROM page_view_hourly"))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await pg.execute(
        text("""INSERT INTO page_view_hourly (hour, path, views, bot_views)
                VALUES (:h, '/register', 3, 0)"""),
        {"h": now - timedelta(days=20)},
    )

    data = await ts.get_traffic_overview(pg, days=7)

    assert data["funnel"]["signups_from"] is None


@pytest.mark.asyncio
async def test_purge_drops_rows_past_retention_and_keeps_the_rest(pg):
    old = datetime.now(timezone.utc) - timedelta(days=ts.RETENTION_DAYS + 1)
    recent = datetime.now(timezone.utc) - timedelta(days=ts.RETENTION_DAYS - 1)
    tag = uuid.uuid4().hex[:8]
    for hour in (old, recent):
        await pg.execute(
            text("""INSERT INTO page_view_hourly (hour, path, views, bot_views)
                    VALUES (:h, '/', 1, 0)"""),
            {"h": hour},
        )
        await pg.execute(
            text("""INSERT INTO traffic_source_hourly (hour, source, views)
                    VALUES (:h, :s, 1)"""),
            {"h": hour, "s": tag},
        )
        await pg.execute(
            text("INSERT INTO visitor_daily (day, path, visitors) VALUES (:d, '*', 1)"),
            {"d": hour.date()},
        )

    await ts.purge_old(pg)

    for table, column, value in (
        ("page_view_hourly", "hour", old),
        ("traffic_source_hourly", "hour", old),
        ("visitor_daily", "day", old.date()),
    ):
        left = await pg.scalar(
            text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :v"), {"v": value}
        )
        assert left == 0, table
    kept = await pg.scalar(
        text("SELECT COUNT(*) FROM page_view_hourly WHERE hour = :v"), {"v": recent}
    )
    assert kept == 1
