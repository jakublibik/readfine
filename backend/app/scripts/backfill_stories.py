"""Group the articles already in the database into stories, after migration 0098.

The migration adds the columns and the trigram index but groups nothing, because
everything a migration does is downtime: the container runs `alembic upgrade head`
before it serves. Measured against a production-sized table (135k articles, 81k of
them inside a 30-day window) the pair scan runs between a quarter and half an hour,
against forty seconds for the whole schema change. So it lives here instead and runs
with the app up.

    docker compose exec app python -m app.scripts.backfill_stories --days 7
    uv run --project backend python -m app.scripts.backfill_stories --dry-run

Optional. Skip it and grouping simply starts from the next fetch; what it buys is the
coverage already sitting in people's unread lists. Seven days is the default because
that is roughly what a reader still has in front of them, and because the cost scales
with the articles in the window, which on a busy instance is thousands a day.

Nothing here needs the app stopped. The scan is read-only, and the writes at the end
touch only ``articles.story_id``, which the reading path treats as a hint: a member it
must not show is filtered out by the access gate regardless. Running it twice is safe
and so is stopping it part way, because it only ever lowers a story_id toward the
smallest id in its group, which is the same definition the live path uses.

Deliberately does not suppress anything. ``app.fetcher.stories._link`` also hides an
article from a reader who has already read the same news, which is right for an article
arriving now and wrong for a month of history: it would mark a pile of articles read at
once, on a guess made long after the fact. This groups, and that is all.
"""
from __future__ import annotations

import argparse
import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.fetcher.stories import COLLAPSE_THRESHOLD, MIN_TITLE_CHARS, WINDOW_HOURS

# Label propagation converges in as many rounds as the longest chain in a group; the
# measured largest group is 13, so this is a runaway guard rather than a real limit.
MAX_ROUNDS = 50

# The scan is cut into slices so progress is visible and each slice's pairs are
# committed as they are found. A day at production volume is a few thousand articles,
# which is under a minute of probing: long enough that the per-slice overhead does not
# matter, short enough to watch it move.
SLICE_HOURS = 24

PAIRS = "story_backfill_pairs"


async def _scan(conn, days: int, slice_hours: int, quiet: bool) -> int:
    """Find every near-duplicate pair in the window, oldest slice first.

    One statement per slice, each in its own transaction, writing into a real table
    rather than a temp one because the pairs have to outlive those transactions. The
    table is dropped and rebuilt on every run: resuming a half-finished one would mean
    matching up slice boundaries between two runs whose windows are both measured from
    their own ``now()``, and getting that subtly wrong would silently skip a day.

    LATERAL, not a plain self-join: it pins the plan to "walk the slice, probe the
    trigram index once per row", which is what the live path does too. Left as a join
    the planner rates a sequential scan computing similarity() over the whole table
    about as cheaply as the index scan, and at this size that coin toss is the
    difference between minutes and an afternoon.
    """
    await conn.execute(text(f"DROP TABLE IF EXISTS {PAIRS}"))
    await conn.execute(text(
        f"CREATE TABLE {PAIRS} (a_id BIGINT NOT NULL, b_id BIGINT NOT NULL)"
    ))
    await conn.commit()

    slices = max(1, (days * 24 + slice_hours - 1) // slice_hours)
    total = 0
    for i in range(slices):
        # Oldest first, so a run that is stopped part way has done the far end of the
        # window and left the near end, which is the part the live path is already
        # keeping up with by itself.
        newest = days * 24 - i * slice_hours
        oldest = max(0, newest - slice_hours)

        started = time.monotonic()
        # SET LOCAL, not SET: `%` reads pg_trgm.similarity_threshold, and a plain SET
        # would leak the value into whatever reuses this pooled connection next. Its
        # default (0.3) matching ours is a coincidence, not something to lean on. This
        # is also what opens the slice's transaction, which the INSERT then joins.
        await conn.execute(text(
            f"SET LOCAL pg_trgm.similarity_threshold = {COLLAPSE_THRESHOLD}"
        ))
        result = await conn.execute(text(f"""
            INSERT INTO {PAIRS} (a_id, b_id)
            SELECT a.id, b.id
            FROM articles a
            CROSS JOIN LATERAL (
                SELECT b.id
                FROM articles b
                WHERE b.id > a.id
                  AND b.feed_id IS DISTINCT FROM a.feed_id
                  AND b.title_norm % a.title_norm
                  AND similarity(a.title_norm, b.title_norm) >= {COLLAPSE_THRESHOLD}
                  AND length(b.title_norm) >= {MIN_TITLE_CHARS}
                  AND COALESCE(b.published_at, b.fetched_at)
                        BETWEEN COALESCE(a.published_at, a.fetched_at)
                                  - INTERVAL '{WINDOW_HOURS} hours'
                            AND COALESCE(a.published_at, a.fetched_at)
                                  + INTERVAL '{WINDOW_HOURS} hours'
            ) b
            WHERE COALESCE(a.published_at, a.fetched_at)
                    >= now() - make_interval(hours => :newest)
              AND COALESCE(a.published_at, a.fetched_at)
                    < now() - make_interval(hours => :oldest)
              -- Same guard as app.fetcher.stories.MIN_TITLE_CHARS: a score over a
              -- handful of trigrams is noise, and the fetcher's "Untitled" placeholder
              -- would herd every title-less item into one story.
              AND length(a.title_norm) >= {MIN_TITLE_CHARS}
        """), {"newest": newest, "oldest": oldest})
        await conn.commit()

        found = result.rowcount or 0
        total += found
        if not quiet:
            print(f"  slice {i + 1}/{slices} ({newest}h..{oldest}h ago): "
                  f"{found} pairs, {time.monotonic() - started:.1f}s", flush=True)
    return total


async def _assign(conn) -> tuple[int, int]:
    """Turn the pairs into story ids: the smallest id in a component wins.

    Three steps, and the third is the one that is easy to miss.

    Seed every article that matched something with a group of its own, unless it
    already has one. COALESCE, not a plain assignment: an article the live path has
    grouped since the migration ran keeps its group, and resetting it to its own id
    would tear apart everything the app has done in the meantime.

    Then walk the minimum along the pair edges until nothing moves. Every edge is
    present at once, which is what makes this land on the true smallest id of each
    connected component rather than on whatever a slice happened to see.

    Then repair the groups whose root moved underneath them. An article whose story_id
    points at an article that has itself since joined a lower group has to follow it,
    and no pair edge says so, because its own counterpart never changed. This is the
    same case ``stories._link`` handles by moving a whole story_id at once, and it
    comes up here for a group that straddles the edge of the window: the half inside
    gets re-examined, the half outside does not.
    """
    await conn.execute(text(f"""
        UPDATE articles SET story_id = COALESCE(story_id, id)
        WHERE id IN (SELECT a_id FROM {PAIRS} UNION SELECT b_id FROM {PAIRS})
    """))

    for _ in range(MAX_ROUNDS):
        result = await conn.execute(text(f"""
            UPDATE articles a SET story_id = m.sid
            FROM (
                SELECT id, MIN(sid) AS sid FROM (
                    SELECT p.a_id AS id, other.story_id AS sid
                      FROM {PAIRS} p
                      JOIN articles other ON other.id = p.b_id
                    UNION ALL
                    SELECT p.b_id AS id, other.story_id AS sid
                      FROM {PAIRS} p
                      JOIN articles other ON other.id = p.a_id
                ) edges GROUP BY id
            ) m
            WHERE a.id = m.id AND a.story_id > m.sid
        """))
        if not result.rowcount:
            break

    for _ in range(MAX_ROUNDS):
        result = await conn.execute(text("""
            UPDATE articles a SET story_id = root.story_id
            FROM articles root
            WHERE a.story_id = root.id AND root.story_id < a.story_id
        """))
        if not result.rowcount:
            break

    grouped = (await conn.execute(text(f"""
        SELECT count(*) FROM articles
        WHERE story_id IS NOT NULL
          AND id IN (SELECT a_id FROM {PAIRS} UNION SELECT b_id FROM {PAIRS})
    """))).scalar() or 0
    stories = (await conn.execute(text(f"""
        SELECT count(DISTINCT story_id) FROM articles
        WHERE story_id IS NOT NULL
          AND id IN (SELECT a_id FROM {PAIRS} UNION SELECT b_id FROM {PAIRS})
    """))).scalar() or 0
    await conn.commit()
    return grouped, stories


async def run(url: str, days: int, slice_hours: int, dry_run: bool, quiet: bool) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            probes = (await conn.execute(text(f"""
                SELECT count(*) FROM articles
                WHERE COALESCE(published_at, fetched_at)
                        >= now() - make_interval(days => :days)
                  AND length(title_norm) >= {MIN_TITLE_CHARS}
            """), {"days": days})).scalar() or 0
            print(f"{probes} articles to probe over {days} days", flush=True)
            if dry_run:
                print("dry run, nothing written")
                return
            if not probes:
                return

            started = time.monotonic()
            pairs = await _scan(conn, days, slice_hours, quiet)
            print(f"{pairs} pairs in {time.monotonic() - started:.0f}s", flush=True)
            if pairs:
                grouped, stories = await _assign(conn)
                print(f"{grouped} articles grouped into {stories} stories")
            await conn.execute(text(f"DROP TABLE IF EXISTS {PAIRS}"))
            await conn.commit()
            print(f"done in {time.monotonic() - started:.0f}s")
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m app.scripts.backfill_stories",
        description="Group articles already in the database into stories.",
    )
    ap.add_argument("--days", type=int, default=7,
                    help="how far back to group (default 7). The live path keeps up "
                         "from here on, so this only has to cover what readers still "
                         "have unread")
    ap.add_argument("--slice-hours", type=int, default=SLICE_HOURS,
                    help=f"scan granularity (default {SLICE_HOURS})")
    ap.add_argument("--dry-run", action="store_true",
                    help="say how much there is to do, write nothing")
    ap.add_argument("--quiet", action="store_true", help="no per-slice progress")
    ap.add_argument("--database-url", help="override the configured DATABASE_URL")
    args = ap.parse_args()

    asyncio.run(run(
        args.database_url or settings.database_url,
        args.days, args.slice_hours, args.dry_run, args.quiet,
    ))


if __name__ == "__main__":
    main()
