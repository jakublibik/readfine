#!/usr/bin/env python
"""Replay story grouping over a corpus and compare membership rules side by side.

``survey_dedup.py`` measures thresholds: how many pairs a score catches and whether they
are the same story. This measures the other half, which is what the pairs get built
into. A threshold can be perfect pair by pair and still produce a group nobody would
call a story, because membership is decided by a rule walking the pairs in order, and
that rule is what this file sweeps.

Read-only, and deliberately not a pytest test for the same reason as its sibling: the
numbers describe one corpus at one moment.

    uv run --project backend python scripts/survey_story_groups.py --from-csv corpus.csv
    uv run --project backend python scripts/survey_story_groups.py --from-csv corpus.csv \
        --mean 0.28 0.30 0.32 --order id
    uv run --project backend python scripts/survey_story_groups.py --from-csv corpus.csv \
        --dump-diff diff.tsv

The corpus is the whole-instance export from ``survey_dedup.py``'s usage notes, not one
account's: group shape depends on how many feeds could have joined, which is exactly
what made the first version look fine on 23 feeds and build 514-member groups on 351.

Rules compared
--------------
``share only`` is what the feature ran until 2026-09-20: an article joins a group when
it matches at least ``MEMBERSHIP_SHARE`` of the members at the collapse threshold. It is
the baseline everything is reported against. ``share AND mean`` is what ships now, and
adds the second condition: mean similarity to *every* member has to clear
``MEMBERSHIP_MEAN``. ``--mean-only`` drops the share test and keeps just the mean, which
is the variant this survey exists to argue against; see the template families below.

What to read
------------
``incoherent`` is the metric that caught the bug this rule was written for: a group
holding two members that score near zero against each other was built by a chain, and
that is the defect itself rather than a proxy for it. It needs no labels, but it shares
its similarity function with the rule being measured, so it cannot be the only evidence
-- ``--dump-diff`` writes every decision the rules disagree on, for reading by hand.

``template`` counts groups whose members are one headline with a slot filled in
differently ("X voted in the Duma elections", "The Washington Post - September 11").
Those are the failure mode of the mean on its own: it counts sub-threshold similarity as
evidence, and a template family is nothing but sub-threshold similarity.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import survey_dedup as sd  # noqa: E402

# Titles with a spelled-out date in them. Magazine issues ("Cycling Weekly - September
# 10, 2026") are the clearest template family in the corpus and the one that blows up
# first if the share test is ever dropped, so they get counted separately.
MONTHS = ("january february march april may june july august september october "
          "november december").split()
DATEY = re.compile(r"(" + "|".join(MONTHS) + r")\s*\d{0,4}|\d{1,2}\.\d{1,2}\.", re.I)

COLLAPSE = sd.COLLAPSE_T
WINDOW_HOURS = sd.WINDOW_HOURS
MEMBERSHIP_SHARE = sd.MEMBERSHIP_SHARE
MAX_GROUP_SIZE = sd.MAX_GROUP_SIZE
MEMBERSHIP_MEAN = sd.MEMBERSHIP_MEAN


def replay(arts, pairs, order: str, mean: float | None, share: bool):
    """Walk the corpus one article at a time and join each to at most one group.

    The third copy of the membership rule, after ``app.fetcher.stories._pick_group`` and
    ``app.scripts.backfill_stories._assign``. It has to agree with them or the survey
    measures something that does not run anywhere; see the note in ``survey_dedup.py``.

    ``order`` decides which arrival order is simulated. Production walks new ids in id
    order inside a fetch round (``_link``), while a corpus replayed by timestamp puts
    articles in publication order, and the two differ whenever a feed backfills. Both
    are here because the answer should not depend on which one is picked.
    """
    by_id = {a.id: a for a in arts}
    edges: defaultdict[int, list[tuple[int, float]]] = defaultdict(list)
    for a, b, score in pairs:  # a is the earlier article, b the later one
        edges[b.id].append((a.id, score))

    walk = sorted(arts, key=(lambda a: a.id) if order == "id" else (lambda a: a.ts))
    story_of: dict[int, int] = {}
    members: dict[int, list[int]] = {}

    for art in walk:
        candidates = edges.get(art.id)
        if not candidates:
            continue
        matches: defaultdict[int, list[float]] = defaultdict(list)
        for other_id, score in candidates:
            matches[story_of.get(other_id, other_id)].append(score)

        best, best_score = None, 0.0
        for group_id, hits in matches.items():
            member_ids = members.get(group_id) or [group_id]
            if len(member_ids) >= MAX_GROUP_SIZE:
                continue
            if share and len(hits) < len(member_ids) * MEMBERSHIP_SHARE:
                continue
            root = by_id.get(group_id)
            if root is None:
                continue
            if abs((art.ts - root.ts).total_seconds()) > WINDOW_HOURS * 3600:
                continue
            if mean is not None:
                sims = [sd.similarity(art.tri, by_id[m].tri) for m in member_ids]
                if sum(sims) / len(sims) < mean:
                    continue
            score = sum(hits) / len(hits)
            if score > best_score:
                best, best_score = group_id, score
        if best is None:
            continue
        if best not in members:
            members[best] = [best]
            story_of[best] = best
        members[best].append(art.id)
        story_of[art.id] = best

    return {g: m for g, m in members.items() if len(m) > 1}, by_id


def incoherent(groups, by_id, floor: float = 0.15):
    """Groups holding a pair of members that are not the same story at all.

    Returns (groups, articles in them, the worst few). A group whose weakest internal
    pair scores under the floor was built by a chain: some member matched one other
    member well enough to get in, and nothing asked what it had to do with the rest.
    """
    bad, bad_articles, worst = 0, 0, []
    for group_id, mem in groups.items():
        low, pair = 1.0, None
        for i, a in enumerate(mem):
            for b in mem[i + 1:]:
                s = sd.similarity(by_id[a].tri, by_id[b].tri)
                if s < low:
                    low, pair = s, (a, b)
        if low < floor:
            bad += 1
            bad_articles += len(mem)
            worst.append((low, group_id, len(mem), pair))
    return bad, bad_articles, sorted(worst)


def template_groups(groups, by_id, min_size: int = 10):
    """Big groups that are one headline template rather than one story."""
    out = {}
    for group_id, mem in groups.items():
        if len(mem) < min_size:
            continue
        if sum(bool(DATEY.search(by_id[a].clean_title)) for a in mem) > len(mem) / 2:
            out[group_id] = mem
    return out


def histogram(groups) -> str:
    counts = Counter(len(m) for m in groups.values())
    return "  ".join(f"{size}:{n}" for size, n in sorted(counts.items()))


def describe(name, groups, by_id, baseline=None) -> dict:
    sizes = sorted((len(m) for m in groups.values()), reverse=True)
    bad, bad_articles, worst = incoherent(groups, by_id)
    templates = template_groups(groups, by_id)
    print(f"\n{name}: {len(groups)} groups, {sum(sizes)} articles grouped, "
          f"largest {sizes[0] if sizes else 0}")
    print(f"  sizes: {histogram(groups)}")
    if baseline is not None:
        here = {a for m in groups.values() for a in m}
        there = {a for m in baseline.values() for a in m}
        print(f"  vs share only: {len(there - here)} articles lost, "
              f"{len(here - there)} gained")
    print(f"  incoherent groups (a member pair under 0.15): {bad} ({bad_articles} articles)")
    for low, group_id, size, pair in worst[:4]:
        print(f"    {low:.3f} group {group_id} ({size}): "
              f"{by_id[pair[0]].clean_title[:34]} || {by_id[pair[1]].clean_title[:34]}")
    print(f"  groups of 10+: {sum(1 for s in sizes if s >= 10)}, "
          f"of those date-template: {len(templates)} "
          f"({sum(len(m) for m in templates.values())} articles)")
    return {"groups": len(groups), "grouped": sum(sizes), "incoherent": bad,
            "incoherent_articles": bad_articles, "templates": len(templates)}


def dump_diff(path: Path, shipped, candidate, by_id) -> None:
    """Every article the two rules place differently, for reading by hand.

    The incoherence metric is computed from the same similarity the rule thresholds on,
    so it cannot say on its own whether a lost article was lost rightly. This is what
    that question gets answered with: the article, the group it was in, and what else
    was in that group.
    """
    ship_of = {a: g for g, m in shipped.items() for a in m}
    cand_of = {a: g for g, m in candidate.items() for a in m}
    rows = []
    for article_id, group_id in ship_of.items():
        if article_id not in cand_of:
            rows.append(("lost", article_id, group_id, shipped[group_id]))
    for article_id, group_id in cand_of.items():
        if article_id not in ship_of:
            rows.append(("gained", article_id, group_id, candidate[group_id]))

    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("verdict\tkind\tarticle_id\tgroup_id\ttitle\tgroup_titles\n")
        for kind, article_id, group_id, mem in rows:
            others = " ~ ".join(
                by_id[m].clean_title for m in mem if m != article_id
            )
            fh.write(f"\t{kind}\t{article_id}\t{group_id}\t"
                     f"{by_id[article_id].clean_title}\t{others}\n")
    print(f"\n{len(rows)} differing decisions written to {path}; "
          f"fill in the verdict column by hand")


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare story membership rules.")
    ap.add_argument("--from-csv", required=True,
                    help="whole-instance export, format as in survey_dedup.py")
    ap.add_argument("--mean", type=float, nargs="+", default=[MEMBERSHIP_MEAN],
                    help=f"mean-similarity thresholds to sweep (default {MEMBERSHIP_MEAN})")
    ap.add_argument("--order", choices=("ts", "id"), default="ts",
                    help="arrival order to simulate (default ts; production uses id)")
    ap.add_argument("--mean-only", action="store_true",
                    help="also replay with the share test dropped, which is what the "
                         "template-family numbers in BENCHMARKS.md come from")
    ap.add_argument("--dump-diff", help="write the differing decisions to this TSV")
    ap.add_argument("--show", type=int, default=0,
                    help="print the N largest groups under the first --mean value")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    arts = sd.load_csv(Path(args.from_csv))
    sd.prepare(arts)
    print(f"corpus: {len(arts)} articles, {arts[0].ts.date()} .. {arts[-1].ts.date()}, "
          f"{len({a.feed_id for a in arts})} feeds, walking in {args.order} order",
          flush=True)

    pairs = sd.find_pairs(arts, COLLAPSE)
    print(f"candidate pairs at >= {COLLAPSE} inside {WINDOW_HOURS}h, cross-feed: "
          f"{len(pairs)}", flush=True)

    shipped, by_id = replay(arts, pairs, args.order, mean=None, share=True)
    describe("share only (the rule before 2026-09-20)", shipped, by_id)

    if args.mean_only:
        for tau in args.mean:
            only, _ = replay(arts, pairs, args.order, mean=tau, share=False)
            describe(f"mean {tau:.2f} only (share dropped)", only, by_id, shipped)

    first = None
    for tau in args.mean:
        both, _ = replay(arts, pairs, args.order, mean=tau, share=True)
        describe(f"share AND mean {tau:.2f}", both, by_id, shipped)
        if first is None:
            first = both

    if first is None:
        return
    if args.dump_diff:
        dump_diff(Path(args.dump_diff), shipped, first, by_id)
    for group_id, mem in sorted(first.items(), key=lambda kv: -len(kv[1]))[:args.show]:
        print(f"\ngroup {group_id}: {len(mem)} members")
        for a in mem[:16]:
            print(f"  {by_id[a].clean_title[:78]}")
        if len(mem) > 16:
            print(f"  ... {len(mem) - 16} more")


if __name__ == "__main__":
    main()
