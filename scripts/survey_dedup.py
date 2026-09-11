#!/usr/bin/env python
"""Measure how much cross-source near-duplicate detection would actually catch.

Readfine already dedups by normalised URL (``fetcher.rss._dedup_cross_feed``), which
only ever catches the same article syndicated under the same link. The open question is
the other case: several outlets covering one story under different URLs and different
wording. Before wiring thresholds into the fetcher, this measures them against the real
corpus, because a threshold picked by intuition is either useless or destructive and
there is no way to tell which from a desk.

It is read-only and deliberately NOT a pytest test: the numbers describe one corpus at
one moment, and a change in them is a prompt to go and look rather than a build failure.

The trigram similarity is a faithful reimplementation of PostgreSQL's ``pg_trgm``
``similarity()`` (lowercase, split on non-alphanumerics, each word padded with two
leading and one trailing space, Jaccard over the resulting 3-gram sets), so a threshold
chosen here transfers unchanged to a ``pg_trgm`` GIN index later. Doing it in Python
also keeps the survey runnable against production without installing an extension.

Usage, from the repository root::

    uv run --project backend python scripts/survey_dedup.py
    uv run --project backend python scripts/survey_dedup.py --user 1 --days 60
    uv run --project backend python scripts/survey_dedup.py --pairs /tmp/pairs.tsv

``--user`` restricts the corpus to one account's subscriptions and, because it can then
see read state, additionally estimates how many articles the suppression branch would
have hidden. Without it the survey runs over every feed in the database.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

# Thresholds the report sweeps. COLLAPSE_T and SUPPRESS_T are the pair the first run
# settled on; the rest are there to show the shape of the curve around them, and in
# particular the noise floor below 0.25 and the flat tail above 0.60.
THRESHOLDS = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65,
              0.70, 0.75, 0.80, 0.85]
COLLAPSE_T = 0.30
SUPPRESS_T = 0.45

# Pairs are only considered inside this window, matching the design: a story is a
# burst, and coverage three weeks apart is a different story about the same subject.
WINDOW_HOURS = 72

# Dropped from the title_key token set. Short, deliberately: an aggressive stopword list
# starts merging unrelated headlines, and the trigram score is what does the real work.
STOPWORDS = {
    # en
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "from", "by", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this",
    "that", "these", "those", "will", "would", "can", "could", "has", "have", "had",
    "not", "no", "new", "says", "say", "said", "after", "over", "into", "about",
    # cs
    "a", "i", "v", "ve", "na", "se", "si", "s", "z", "ze", "do", "o", "u", "k", "ke",
    "po", "pro", "od", "za", "je", "jsou", "byl", "byla", "bylo", "byly", "bude",
    "budou", "ale", "nebo", "to", "ten", "ta", "ty", "to", "co", "jak", "uz", "jeste",
    "nove", "novy", "nova", "podle", "pri", "pred", "mezi", "jako", "vice", "mene",
}

# A trailing "| The Verge" / "- Ars Technica" is feed furniture, not part of the
# headline, and leaving it in inflates the similarity of every pair from the same feed
# while deflating every cross-feed pair. Stripped data-driven rather than from a list:
# see strip_feed_suffixes.
SEPARATORS = ("|", " - ", " – ", " — ", " :: ", " » ", "•")


def unaccent(s: str) -> str:
    """Same normalisation the FTS index already applies (immutable_unaccent)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def trigrams(s: str) -> set[str]:
    """pg_trgm-compatible 3-gram set for a string."""
    out: set[str] = set()
    for word in re.split(r"[^a-z0-9]+", unaccent(s).lower()):
        if not word:
            continue
        padded = f"  {word} "
        for i in range(len(padded) - 2):
            out.add(padded[i : i + 3])
    return out


def similarity(a: set[str], b: set[str]) -> float:
    """pg_trgm similarity(): Jaccard over the 3-gram sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", unaccent(s).lower()) if t]


def title_key(s: str) -> str:
    """Deterministic key: significant tokens, sorted, hashed.

    Catches reordering, punctuation and casing differences and nothing else. This is the
    cheap exact-match layer that runs before any fuzzy comparison.
    """
    sig = sorted({t for t in tokens(s) if t not in STOPWORDS and len(t) > 1})
    if not sig:
        return ""
    return hashlib.sha256(" ".join(sig).encode()).hexdigest()[:16]


@dataclass
class Art:
    id: int
    feed_id: int | None
    feed_title: str
    title: str
    clean_title: str = ""
    ts: datetime = None  # type: ignore[assignment]
    arrived: datetime = None  # type: ignore[assignment]
    url_norm: str | None = None
    key: str = ""
    tri: set[str] = field(default_factory=set)
    sig_tokens: set[str] = field(default_factory=set)
    is_read: bool = False
    read_at: datetime | None = None
    read_human: bool = False


def load_csv(path: Path) -> list[Art]:
    """Load the production export produced by the \\copy in the survey's usage notes.

    A live database is the wrong corpus for the read-state half of this survey: a
    development copy fetches the same feeds, so its articles are real, but nobody reads
    in it, so every is_read in it came from a bulk sweep. The columns here are exactly
    those needed to reconstruct what the user had actually read at the moment each
    duplicate arrived.
    """
    import csv

    def ts(v: str) -> datetime | None:
        if not v:
            return None
        # psql renders "2026-09-01 20:14:12+00"; fromisoformat wants the colon in the
        # offset on Python < 3.11 and tolerates its absence after, so normalise.
        v = v.strip().replace(" ", "T")
        if re.search(r"[+-]\d{2}$", v):
            v += ":00"
        return datetime.fromisoformat(v)

    arts: list[Art] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            published, fetched = ts(row["published_at"]), ts(row["fetched_at"])
            if not (published or fetched):
                continue
            dwell = int(row["dwell_seconds"] or 0)
            opened = row["link_opened"] == "t"
            starred = row["ever_starred"] == "t"
            arts.append(Art(
                id=int(row["id"]),
                feed_id=int(row["feed_id"]) if row["feed_id"] else None,
                feed_title=row["feed_title"] or "?",
                title=row["title"],
                ts=published or fetched,
                arrived=fetched or published,
                url_norm=row["url_normalized"] or None,
                is_read=row["is_read"] == "t",
                read_at=ts(row["read_at"]),
                read_human=dwell >= 30 or opened or starred,
            ))
    arts.sort(key=lambda a: a.ts)
    return arts


async def load(url: str, days: int, user_id: int | None) -> list[Art]:
    engine = create_async_engine(url)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    where_user = ""
    cols = "false as is_read, false as read_human"
    joins = ""
    if user_id is not None:
        # A state row written by the existing URL dedup is an automatic read, not
        # evidence the user saw anything. The production rule will tell the two apart by
        # suppressed_at; that column does not exist yet, so the closest proxy available
        # here is a read with no engagement of any kind recorded against it AND an
        # identical normalised URL elsewhere. Rather than guess, the survey reports both
        # a strict and a loose reading (see read_human below).
        joins = """
            JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
            LEFT JOIN user_article_states uas
                   ON uas.article_id = a.id AND uas.user_id = :uid
        """
        cols = """
            COALESCE(uas.is_read, false) as is_read, uas.read_at,
            COALESCE(uas.dwell_seconds, 0) >= 30
                OR COALESCE(uas.link_opened, false)
                OR COALESCE(uas.ever_starred, false) as read_human
        """
    else:
        cols = "false as is_read, null::timestamptz as read_at, false as read_human"
    sql = f"""
        SELECT a.id, a.feed_id, COALESCE(f.title, '?') as feed_title, a.title,
               COALESCE(a.published_at, a.fetched_at) as ts, a.fetched_at,
               a.url_normalized,
               {cols}
        FROM articles a
        LEFT JOIN feeds f ON f.id = a.feed_id
        {joins}
        WHERE COALESCE(a.published_at, a.fetched_at) >= :since
          AND a.trimmed_at IS NULL
          AND a.title <> ''
        {where_user}
        ORDER BY ts
    """
    params = {"since": since}
    if user_id is not None:
        params["uid"] = user_id
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).all()
    await engine.dispose()
    return [
        Art(
            id=r.id, feed_id=r.feed_id, feed_title=r.feed_title, title=r.title,
            ts=r.ts, arrived=r.fetched_at or r.ts, url_norm=r.url_normalized,
            is_read=r.is_read, read_at=r.read_at, read_human=r.read_human,
        )
        for r in rows
    ]


def strip_feed_suffixes(arts: list[Art]) -> dict[int, str]:
    """Find and strip each feed's boilerplate title suffix.

    Data-driven instead of a hardcoded list of outlets: for every feed, take the trailing
    segment after a separator and strip it when it repeats across most of that feed's
    titles. A suffix that only shows up occasionally is part of the headline.
    """
    by_feed: dict[int, list[str]] = defaultdict(list)
    for a in arts:
        by_feed[a.feed_id or -1].append(a.title)

    suffixes: dict[int, str] = {}
    for feed_id, titles in by_feed.items():
        if len(titles) < 5:
            continue
        counts: Counter[str] = Counter()
        for t in titles:
            for sep in SEPARATORS:
                idx = t.rfind(sep)
                if idx > 20:  # a separator near the start is not a suffix
                    tail = t[idx:].strip()
                    if 3 < len(tail) < 40:
                        counts[tail] += 1
                    break
        if counts:
            tail, n = counts.most_common(1)[0]
            if n / len(titles) >= 0.5:
                suffixes[feed_id] = tail
    return suffixes


def prepare(arts: list[Art]) -> dict[int, str]:
    suffixes = strip_feed_suffixes(arts)
    for a in arts:
        suffix = suffixes.get(a.feed_id or -1)
        title = a.title
        if suffix and title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        a.clean_title = title or a.title
        a.key = title_key(a.clean_title)
        a.tri = trigrams(a.clean_title)
        a.sig_tokens = {t for t in tokens(a.clean_title) if t not in STOPWORDS and len(t) > 2}
    return suffixes


def build_idf(arts: list[Art]) -> dict[str, float]:
    """Inverse document frequency over the corpus's significant tokens.

    The point of the second score: two headlines about one event almost always share a
    rare token (a surname, a place, a number), while two headlines that merely share a
    subject share only common ones. Raw trigram overlap cannot tell those apart because
    it weights every character the same.
    """
    import math

    df: Counter[str] = Counter()
    for a in arts:
        df.update(a.sig_tokens)
    n = len(arts)
    return {tok: math.log(n / c) for tok, c in df.items()}


def idf_cosine(a: Art, b: Art, idf: dict[str, float]) -> float:
    """Cosine over IDF-weighted token sets."""
    import math

    shared = a.sig_tokens & b.sig_tokens
    if not shared:
        return 0.0
    num = sum(idf.get(t, 0.0) ** 2 for t in shared)
    na = math.sqrt(sum(idf.get(t, 0.0) ** 2 for t in a.sig_tokens))
    nb = math.sqrt(sum(idf.get(t, 0.0) ** 2 for t in b.sig_tokens))
    if not na or not nb:
        return 0.0
    return num / (na * nb)


def find_pairs(arts: list[Art], min_t: float) -> list[tuple[Art, Art, float]]:
    """Cross-feed pairs inside the window scoring at least min_t.

    Blocking mirrors what the real implementation would do with a trigram index: only
    articles sharing at least one significant token are compared at all. Without it this
    is quadratic over the whole corpus; with it the comparison count stays close to
    linear, and --check-blocking measures what the shortcut costs in recall.
    """
    by_token: dict[str, list[int]] = defaultdict(list)
    pairs: list[tuple[Art, Art, float]] = []
    seen: set[tuple[int, int]] = set()
    window = timedelta(hours=WINDOW_HOURS)

    for i, a in enumerate(arts):
        candidates: set[int] = set()
        for tok in a.sig_tokens:
            candidates.update(by_token.get(tok, ()))
        for j in candidates:
            b = arts[j]
            if a.ts - b.ts > window:
                continue
            if a.feed_id == b.feed_id:
                continue  # in-feed repeats are the existing dedup's job
            score = similarity(a.tri, b.tri)
            if score >= min_t:
                pair = (b.id, a.id)
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((b, a, score))
        for tok in a.sig_tokens:
            by_token[tok].append(i)
    return pairs


def check_blocking(arts: list[Art], min_t: float, sample: int = 1500) -> tuple[int, int]:
    """Brute-force a slice of the corpus to see what the token prefilter misses."""
    subset = arts[:sample]
    window = timedelta(hours=WINDOW_HOURS)
    full = 0
    for i, a in enumerate(subset):
        for b in subset[:i]:
            if a.ts - b.ts > window or a.feed_id == b.feed_id:
                continue
            if similarity(a.tri, b.tri) >= min_t:
                full += 1
    blocked = len(find_pairs(subset, min_t))
    return blocked, full


def clusters_from(pairs: list[tuple[Art, Art, float]], t: float) -> dict[int, list[int]]:
    """Union-find over pairs at or above t, returning cluster id -> article ids."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for a, b, score in pairs:
        if score >= t:
            union(a.id, b.id)

    out: dict[int, list[int]] = defaultdict(list)
    for node in parent:
        out[find(node)].append(node)
    return out


def estimate_suppression_timed(
    pairs: list[tuple[Art, Art, float]], t: float, strict: bool
) -> tuple[int, int, int]:
    """Suppression as it would actually have fired, in arrival order.

    The naive estimate below asks "is the older article read *now*", which counts a
    story read last week as if it had been read before its duplicate landed. This one
    asks the question the fetcher would have asked: at the moment the newer article
    arrived (its fetched_at), had the user already read the older one (read_at)?

    Returns (hidden, regret, unknown): articles that would never have reached the unread
    list, how many of those the user went on to engage with anyway, and pairs skipped
    because the older article is read but carries no read_at to order it by.
    """
    hidden: set[int] = set()
    regret = 0
    unknown = 0
    for older, newer, score in pairs:
        if score < t:
            continue
        if strict and not older.read_human:
            continue
        if not older.is_read:
            continue
        if older.read_at is None:
            unknown += 1
            continue
        if older.read_at <= newer.arrived and newer.id not in hidden:
            hidden.add(newer.id)
            if newer.read_human:
                regret += 1
    return len(hidden), regret, unknown


def estimate_suppression(
    pairs: list[tuple[Art, Art, float]], t: float, strict: bool
) -> tuple[int, int]:
    """How many articles the suppression branch would have hidden, and of those, how
    many the user went on to engage with anyway (i.e. probable false positives).

    ``strict`` uses the engagement signals (dwell/link/star) as the "you saw it" test;
    otherwise any read counts, which is the rule the design settled on.

    Approximate in both directions: it tests *current* read state, not the state at the
    moment the newer article arrived. An article read last week counts here even though
    at arrival time it was still unread (overestimate), while one read only after the
    duplicate showed up is missed (underestimate).
    """
    hidden: set[int] = set()
    regret = 0
    for older, newer, score in pairs:
        if score < t:
            continue
        seen = older.read_human if strict else older.is_read
        if seen and newer.id not in hidden:
            hidden.add(newer.id)
            if newer.read_human:
                regret += 1
    return len(hidden), regret


def report(arts: list[Art], args, suffixes: dict[int, str]) -> dict:
    days = args.days
    print(f"\n{'=' * 78}")
    print(f"Corpus: {len(arts)} articles, {days} days, "
          f"{len({a.feed_id for a in arts})} feeds"
          + (f", user {args.user}" if args.user else ", all feeds"))
    if arts:
        print(f"Span:   {arts[0].ts:%Y-%m-%d} .. {arts[-1].ts:%Y-%m-%d} "
              f"({len(arts) / max(days, 1):.0f} articles/day)")
    print(f"Feed title suffixes stripped: {len(suffixes)} feeds")
    print("=" * 78)

    # Layer 0: what the existing URL dedup already covers, so the rest of the report
    # measures the *incremental* catch rather than re-counting solved cases.
    by_url: dict[str, set[int]] = defaultdict(set)
    for a in arts:
        if a.url_norm:
            by_url[a.url_norm].add(a.feed_id or -1)
    url_dupes = sum(1 for feeds in by_url.values() if len(feeds) > 1)
    print(f"\nAlready caught by url_normalized: {url_dupes} cross-feed URL groups")

    # Layer 1: exact normalised-title match.
    by_key: dict[str, list[Art]] = defaultdict(list)
    for a in arts:
        if a.key:
            by_key[a.key].append(a)
    key_groups = [
        g for g in by_key.values()
        if len(g) > 1 and len({x.feed_id for x in g}) > 1
        and (max(x.ts for x in g) - min(x.ts for x in g)) <= timedelta(hours=WINDOW_HOURS)
    ]
    key_articles = sum(len(g) - 1 for g in key_groups)
    print(f"Exact title_key match:           {len(key_groups)} groups, "
          f"{key_articles} redundant articles ({key_articles / max(len(arts), 1) * 100:.1f}%)")

    # Layer 2: the trigram sweep.
    lowest = min(THRESHOLDS)
    pairs = find_pairs(arts, lowest)
    print(f"\nTrigram pairs at >= {lowest}: {len(pairs)}")
    # "new" is the column that decides whether the fuzzy layer earns its place: pairs
    # whose two titles do not already share a title_key, i.e. what the cheap exact layer
    # would have missed.
    print(f"\n{'thr':>5} {'pairs':>7} {'new':>6} {'clusters':>9} {'collapsed':>10} "
          f"{'%corpus':>8} {'/day':>6}")
    print("-" * 78)
    sweep = {}
    for t in THRESHOLDS:
        cl = clusters_from(pairs, t)
        above = [p for p in pairs if p[2] >= t]
        new = sum(1 for a, b, _ in above if a.key != b.key)
        in_clusters = sum(len(v) for v in cl.values())
        collapsed = in_clusters - len(cl)  # rows the list would stop showing
        sweep[t] = {
            "pairs": len(above), "new_over_title_key": new, "clusters": len(cl),
            "in_clusters": in_clusters, "collapsed": collapsed,
        }
        print(f"{t:>5.2f} {len(above):>7} {new:>6} {len(cl):>9} {collapsed:>10} "
              f"{collapsed / max(len(arts), 1) * 100:>7.1f}% "
              f"{collapsed / max(days, 1):>6.1f}")

    if any(a.is_read for a in arts):
        n_read = sum(1 for a in arts if a.is_read)
        n_read_at = sum(1 for a in arts if a.read_at)
        n_human = sum(1 for a in arts if a.read_human)
        print(f"\nRead state: {n_read} read ({n_read / len(arts) * 100:.0f}% of corpus), "
              f"{n_read_at} with read_at, {n_human} with engagement recorded")
        if n_human < n_read * 0.05:
            print("  WARNING: almost no engagement against these reads. In a development\n"
                  "  database every is_read came from a bulk sweep and the numbers below\n"
                  "  describe nothing.")

        print(f"\nSuppression estimate (window {WINDOW_HOURS}h, ordered by arrival):")
        print(f"{'thr':>5} {'hidden':>8} {'/day':>6} {'regret':>7} {'unknown':>8}   rule")
        print("-" * 78)
        for t in (0.30, 0.35, 0.40, SUPPRESS_T, 0.50, 0.60, 0.75):
            for strict, label in ((False, "any read"), (True, "dwell/link/star")):
                hidden, regret, unknown = estimate_suppression_timed(pairs, t, strict)
                print(f"{t:>5.2f} {hidden:>8} {hidden / max(days, 1):>6.1f} "
                      f"{regret:>7} {unknown:>8}   {label}")
        print("\n  hidden  = articles that would never have reached the unread list")
        print("  regret  = of those, ones the user engaged with anyway "
              "(dwell>=30 / opened / starred)")
        print("            i.e. a lower bound on false positives that actually cost "
              "something")
        print("  unknown = pairs skipped: older article is read but has no read_at")

    # Samples for eyeballing precision. The bands matter more than the totals: the
    # threshold gets chosen by reading these, not by the counts above.
    print(f"\n{'=' * 78}\nSample pairs by score band (read these, the numbers can't "
          f"tell you if a match is right)\n{'=' * 78}")
    bands = [(0.15, 0.22), (0.22, 0.30), (0.30, 0.38), (0.38, 0.45), (0.45, 0.55),
             (0.55, 0.70), (0.70, 1.01)]
    rng = random.Random(args.seed)
    samples = {}
    for lo, hi in bands:
        band = [p for p in pairs if lo <= p[2] < hi]
        picked = rng.sample(band, min(args.samples, len(band)))
        samples[f"{lo}-{hi}"] = [
            {"score": s, "a": a.clean_title, "b": b.clean_title,
             "feed_a": a.feed_title, "feed_b": b.feed_title}
            for a, b, s in picked
        ]
        print(f"\n── {lo:.2f} – {hi:.2f}  ({len(band)} pairs) " + "─" * 40)
        for a, b, s in picked:
            gap = (b.ts - a.ts).total_seconds() / 3600
            print(f"  {s:.3f}  +{gap:4.1f}h")
            print(f"    A [{a.feed_title[:22]:22}] {a.clean_title[:90]}")
            print(f"    B [{b.feed_title[:22]:22}] {b.clean_title[:90]}")

    if args.check_blocking:
        blocked, full = check_blocking(arts, COLLAPSE_T)
        miss = full - blocked
        print(f"\n{'=' * 78}")
        print(f"Blocking check on first 1500 articles at {COLLAPSE_T}: "
              f"prefilter {blocked}, brute force {full}, missed {miss} "
              f"({miss / max(full, 1) * 100:.1f}%)")

    if args.pairs:
        out = Path(args.pairs)
        with out.open("w") as fh:
            fh.write("score\tgap_h\tfeed_a\ttitle_a\tfeed_b\ttitle_b\n")
            for a, b, s in sorted(pairs, key=lambda p: -p[2]):
                gap = (b.ts - a.ts).total_seconds() / 3600
                fh.write(f"{s:.4f}\t{gap:.1f}\t{a.feed_title}\t{a.clean_title}\t"
                         f"{b.feed_title}\t{b.clean_title}\n")
        print(f"\nAll {len(pairs)} pairs written to {out}")

    return {
        "corpus": len(arts), "days": days, "url_groups": url_dupes,
        "title_key_groups": len(key_groups), "title_key_articles": key_articles,
        "sweep": sweep, "samples": samples,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60, help="corpus window (default 60)")
    ap.add_argument("--user", type=int, help="restrict to one user's subscriptions")
    ap.add_argument("--samples", type=int, default=6, help="sample pairs per band")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pairs", help="write every pair to this TSV")
    ap.add_argument("--json", dest="json_out", help="write the summary as JSON")
    ap.add_argument("--check-blocking", action="store_true",
                    help="brute-force a slice to measure prefilter recall loss")
    ap.add_argument("--database-url", help="override DATABASE_URL")
    ap.add_argument("--from-csv", help="read the corpus from a production export "
                                       "instead of a live database")
    args = ap.parse_args()

    if args.from_csv:
        arts = load_csv(Path(args.from_csv))
        if arts:
            span = (arts[-1].ts - arts[0].ts).days
            args.days = span or args.days
    else:
        url = args.database_url
        if not url:
            env = (REPO_ROOT / ".env").read_text()
            url = env.split("DATABASE_URL=")[1].split("\n")[0].strip()
        arts = asyncio.run(load(url, args.days, args.user))
    if not arts:
        print("No articles in range.")
        return
    suffixes = prepare(arts)
    summary = report(arts, args, suffixes)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"Summary written to {args.json_out}")


if __name__ == "__main__":
    main()
