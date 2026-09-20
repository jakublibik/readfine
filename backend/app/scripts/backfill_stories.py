"""Group the articles already in the database into stories, after migration 0098.

The migration adds the columns and the trigram index but groups nothing, because
everything a migration does is downtime: the container runs `alembic upgrade head`
before it serves. Measured against a production-sized table (135k articles, 81k of
them inside a 30-day window) the pair scan runs between a quarter and half an hour,
against forty seconds for the whole schema change. So it lives here instead and runs
with the app up.

    docker compose exec app python -m app.scripts.backfill_stories --days 7
    uv run --project backend python -m app.scripts.backfill_stories --dry-run
    docker compose exec app python -m app.scripts.backfill_stories --days 10 --reset

Optional. Skip it and grouping simply starts from the next fetch; what it buys is the
coverage already sitting in people's unread lists. Seven days is the default because
that is roughly what a reader still has in front of them, and because the cost scales
with the articles in the window, which on a busy instance is thousands a day.

``--reset`` is the other reason to run it: when the matching rules themselves change,
the groups already in the database were built by rules that no longer apply, and
rebuilding on top of them would preserve exactly what the change was meant to undo.

Nothing here needs the app stopped. The scan is read-only, and the writes at the end
touch only ``articles.story_id``, which the reading path treats as a hint: a member it
must not show is filtered out by the access gate regardless. Running it twice is safe
and so is stopping it part way: articles already grouped are left as they are, which
is also what makes a second run agree with the first rather than reshuffle it.

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
from app.fetcher.story_params import (
    COLLAPSE_THRESHOLD,
    MAX_GROUP_SIZE,
    MEMBERSHIP_MEAN,
    MEMBERSHIP_SHARE,
    MIN_TITLE_CHARS,
    WINDOW_HOURS,
)

# Rows per UPDATE when writing the grouping back. The writes are one statement per
# article because each one gets a different story_id; batching them keeps the round
# trips down without building a CASE expression over a hundred thousand ids.
WRITE_BATCH = 1000

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
        f"CREATE TABLE {PAIRS} (a_id BIGINT NOT NULL, b_id BIGINT NOT NULL, "
        "sim REAL NOT NULL)"
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
            INSERT INTO {PAIRS} (a_id, b_id, sim)
            SELECT a.id, b.id, b.sim
            FROM articles a
            CROSS JOIN LATERAL (
                SELECT b.id, similarity(a.title_norm, b.title_norm) AS sim
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


async def _meta(conn, ids) -> dict[int, tuple]:
    """(timestamp, story_id) for each article id, the two things a decision needs."""
    if not ids:
        return {}
    return {
        row.id: (row.ts, row.story_id)
        for row in await conn.execute(text("""
            SELECT id, COALESCE(published_at, fetched_at) AS ts, story_id
            FROM articles WHERE id = ANY(:ids)
        """), {"ids": list(ids)})
    }


async def _similarity_to(conn, article_id: int, member_ids: list[int]) -> dict[int, float]:
    """Trigram similarity of one article's title against a set of others.

    Scored by Postgres for the same reason as in ``app.fetcher.stories``: a Python
    reimplementation of pg_trgm would be a second definition of the same number, and the
    two would have to agree forever. One round trip per article that has a candidate
    group left standing after the cheap tests, which on a 30-day production window is a
    few thousand lookups by primary key. Measured at 0.86 ms each, so a few seconds
    against a scan that takes a quarter to half an hour.
    """
    rows = await conn.execute(text("""
        SELECT m.id, similarity(a.title_norm, m.title_norm)
        FROM articles a, articles m
        WHERE a.id = :aid AND m.id = ANY(:mids)
    """), {"aid": article_id, "mids": list(member_ids)})
    return {row[0]: float(row[1] or 0.0) for row in rows}


async def _assign(conn) -> tuple[int, int]:
    """Replay the pairs in arrival order under the live membership rule.

    Not label propagation any more, and it cannot be. Propagating the minimum along the
    edges is the transitive closure by definition: it is exactly what ``_link`` used to
    do and exactly what produced 514-member groups. The rule that replaced it asks how
    much of a group an article matches, so the answer depends on what the group already
    holds, and that depends on the order articles arrived in.

    So this walks the articles oldest first and hands each one to the same decision the
    fetcher makes, which also means a backfill and a week of live fetching land on the
    same grouping rather than two different ones.

    In Python rather than SQL because the state being carried forward is a group's
    membership and root, which changes with every row. The edges are the expensive part
    and they are already computed; what is left is a scan over a few hundred thousand
    pairs, plus one similarity lookup per article that still has a candidate group after
    the cheap tests.
    """
    edges: dict[int, list[tuple[int, float]]] = {}
    rows = await conn.execute(text(f"SELECT a_id, b_id, sim FROM {PAIRS}"))
    article_ids: set[int] = set()
    for a_id, b_id, sim in rows:
        # a_id < b_id by construction (the scan only looks forward), so every edge is
        # filed under the later article: that is the one making the decision.
        edges.setdefault(b_id, []).append((a_id, sim))
        article_ids.update((a_id, b_id))
    if not article_ids:
        return 0, 0

    meta = await _meta(conn, article_ids)

    # story_id of each article as this run decides it, plus the state of every group it
    # builds. Seeded from what is already in the database so that a second run, or a run
    # over a window the live path has already grouped, agrees with it instead of
    # fighting it.
    story_of = {
        article_id: story_id
        for article_id, (_, story_id) in meta.items()
        if story_id is not None
    }
    # Membership comes from the database, not from what the scan saw: a group can
    # perfectly well have members whose only counterpart sits outside this window, and
    # counting only the ones in front of us would understate the group and let articles
    # in that the live path would turn away. Ids rather than a count, because the mean
    # test has to score the newcomer against each member.
    members: dict[int, list[int]] = {}
    roots = set(story_of.values())
    if roots:
        for row in await conn.execute(text("""
            SELECT story_id, id FROM articles WHERE story_id = ANY(:ids)
        """), {"ids": list(roots)}):
            members.setdefault(row.story_id, []).append(row.id)
        # A root can be older than anything the scan touched, and its timestamp is what
        # the window is measured against.
        meta.update(await _meta(conn, roots - set(meta)))

    window = WINDOW_HOURS * 3600
    for article_id in sorted(article_ids):
        if article_id in story_of:
            continue
        matches: dict[int, list[float]] = {}
        for other_id, sim in edges.get(article_id, ()):
            group_id = story_of.get(other_id, other_id)
            matches.setdefault(group_id, []).append(sim)

        # Same order as app.fetcher.stories._pick_group, and for the same reason: the
        # mean is the only test that costs a query, so it runs on what is left.
        shortlist: list[tuple[int, float]] = []
        wanted: set[int] = set()
        for group_id, hits in matches.items():
            member_ids = members.get(group_id) or [group_id]
            if len(member_ids) >= MAX_GROUP_SIZE:
                continue
            if len(hits) < len(member_ids) * MEMBERSHIP_SHARE:
                continue
            root_ts = meta.get(group_id, (None, None))[0]
            if root_ts is None:
                continue
            if abs((meta[article_id][0] - root_ts).total_seconds()) > window:
                continue
            shortlist.append((group_id, sum(hits) / len(hits)))
            wanted.update(member_ids)

        best, best_score = None, 0.0
        if shortlist:
            sims = await _similarity_to(conn, article_id, sorted(wanted))
            for group_id, score in shortlist:
                member_ids = members.get(group_id) or [group_id]
                mean = sum(sims.get(m, 0.0) for m in member_ids) / len(member_ids)
                if mean < MEMBERSHIP_MEAN:
                    continue
                if score > best_score:
                    best, best_score = group_id, score
        if best is None:
            continue

        if best not in story_of:  # the root naming a group for the first time
            story_of[best] = best
        story_of[article_id] = best
        members.setdefault(best, [best]).append(article_id)

    writes = [
        {"id": article_id, "sid": story_id}
        for article_id, story_id in story_of.items()
        if meta.get(article_id, (None, None))[1] != story_id
    ]
    for start in range(0, len(writes), WRITE_BATCH):
        await conn.execute(
            text("UPDATE articles SET story_id = :sid WHERE id = :id"),
            writes[start:start + WRITE_BATCH],
        )
    await conn.commit()
    grouped = len(story_of)
    return grouped, len(set(story_of.values()))


async def _reset(conn) -> int:
    """Undo every grouping decision, including the ones already acted on.

    Three separate things, and only the first is obvious. The groups themselves go, and
    so do the two kinds of read this feature writes: an article folded away under a
    story the reader marked read, and one hidden because they had read something too
    similar. Both are machine-written reads carrying suppressed_at, so clearing them
    gives back articles nobody actually read.

    ``hidden_at`` is deliberately left alone. It is the record that something was once
    taken away, the reader's own reading already clears the columns beside it, and
    nothing reads it back into a decision (see migration 0101). URL dedup is left alone
    too: it keys on an identical address, it is not part of this, and it is right.

    Does not commit; the caller owns the transaction.
    """
    await conn.execute(text("UPDATE articles SET story_id = NULL WHERE story_id IS NOT NULL"))
    result = await conn.execute(text("""
        UPDATE user_article_states
        SET is_read = false, read_at = NULL, suppressed_at = NULL, suppressed_by = NULL
        WHERE suppressed_by IN ('story', 'similar')
    """))
    return result.rowcount or 0


async def run(url: str, days: int, slice_hours: int, dry_run: bool, quiet: bool,
              reset: bool = False) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            if reset and not dry_run:
                given_back = await _reset(conn)
                await conn.commit()
                print(f"reset: groups cleared, {given_back} hidden articles given back",
                      flush=True)
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
    ap.add_argument("--reset", action="store_true",
                    help="drop every existing group and give back every article this "
                         "feature has hidden, then group again from scratch. For a "
                         "change to the matching rules, where the old grouping is not "
                         "something to build on")
    ap.add_argument("--dry-run", action="store_true",
                    help="say how much there is to do, write nothing")
    ap.add_argument("--quiet", action="store_true", help="no per-slice progress")
    ap.add_argument("--database-url", help="override the configured DATABASE_URL")
    args = ap.parse_args()

    asyncio.run(run(
        args.database_url or settings.database_url,
        args.days, args.slice_hours, args.dry_run, args.quiet, args.reset,
    ))


if __name__ == "__main__":
    main()
