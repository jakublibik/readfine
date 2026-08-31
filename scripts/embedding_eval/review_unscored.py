# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "sentence-transformers>=3.0",
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   # not used here: importing run_eval pulls in the app's compute_auc, which
#   # imports sqlalchemy at module level
#   "sqlalchemy>=2.0",
# ]
# ///
"""Scenario C: is a semantic score worth anything on the articles nobody scored?

Scoring only runs on articles a keyword or feed rule labeled
(`filter_service.py:581`), so roughly two thirds of what arrives never gets a
score at all. The question is whether a score would surface anything worth
reading in there. It cannot be answered with an AUC: the user is only ever shown
labeled articles, so engagement on the rest is zero by construction no matter how
good they are. It has to be answered by reading titles.

Which means the test has to be built so that reading titles proves something:

- **Blind.** The candidate list does not say which scorer picked which article,
  and mixes them, so "the embedding's picks look good" cannot be wishful
  reading. The mapping goes to a separate key file.
- **With a control.** Random articles from the same pool are mixed in. Without
  them, "9 of 20 look interesting" means nothing, because nobody knows what 20
  random ones would have scored. The control is the whole experiment.
- **Against BM25, not just against nothing.** If word matching surfaces the same
  articles, the semantic model is not what found them.

Two steps:

    uv run --script review_unscored.py prepare --sample unscored.jsonl \\
        --profile-from sample.jsonl --out review.csv --key review_key.json

    (fill in the `want` column with y/n, without opening the key file)

    uv run --script review_unscored.py score --review review.csv --key review_key.json
"""
import argparse
import csv
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval import (  # noqa: E402
    BM25_K1,
    BM25_B,
    _bm25_weighted_matrix,
    aggregate_positive,
    article_text,
    default_prefix_family,
    parse_profile,
    PREFIXES,
)


def load_jsonl(path: Path) -> tuple[dict, list[dict]]:
    meta, rows = None, []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "meta":
                meta = obj
            else:
                rows.append(obj)
    return meta, rows


def prepare(args) -> None:
    meta, rows = load_jsonl(args.sample)
    if not rows:
        raise SystemExit("no articles in the sample")
    # `kind` only exists on exports made after this script did; the rows
    # themselves are the reliable tell, and handing over the scored sample by
    # mistake would silently review the wrong population.
    if (meta or {}).get("kind") == "scored" or "ai_score" in rows[0]:
        raise SystemExit("that sample holds scored articles; scenario C is about "
                         "the ones that never got a score, so export it with "
                         "--unscored")

    profile_meta = meta
    if args.profile_from:
        profile_meta, _ = load_jsonl(args.profile_from)
    profile = (profile_meta or {}).get("profile", {}).get("current")
    if not profile:
        raise SystemExit("no profile text in the sample meta; pass --profile-from")

    positive, moderate_mask, negative = parse_profile(profile, "b", None, None)
    texts = [article_text(r, args.input) for r in rows]

    from sentence_transformers import SentenceTransformer
    from sklearn.feature_extraction.text import CountVectorizer

    family = args.prefix_family or default_prefix_family(args.model)
    query_prefix, passage_prefix = PREFIXES[family]
    model = SentenceTransformer(args.model)
    art_vecs = model.encode([passage_prefix + t for t in texts],
                            normalize_embeddings=True, batch_size=args.batch_size,
                            show_progress_bar=True)
    pos_vecs = model.encode([query_prefix + t for t in positive],
                            normalize_embeddings=True)
    neg_vecs = (model.encode([query_prefix + t for t in negative],
                             normalize_embeddings=True) if negative else None)
    # Same aggregation the eval run uses, so this is the configuration that was
    # actually measured rather than a third variant invented for the review.
    agg = (args.moderate_penalty, "max", 3)
    emb = aggregate_positive(art_vecs @ pos_vecs.T, moderate_mask, *agg)
    if neg_vecs is not None and len(neg_vecs):
        emb = emb - (art_vecs @ neg_vecs.T).max(axis=1)

    vec = CountVectorizer(lowercase=True, strip_accents="unicode",
                          ngram_range=(1, 2), min_df=2, max_features=200_000)
    weighted = _bm25_weighted_matrix(vec.fit_transform(texts), BM25_K1, BM25_B)

    def bm25_against(units):
        q = (vec.transform(units) > 0).astype(np.float64)
        return np.asarray((weighted @ q.T).todense())

    bm25 = aggregate_positive(bm25_against(positive), moderate_mask, *agg)
    if negative:
        bm25 = bm25 - bm25_against(negative).max(axis=1)

    rng = random.Random(args.seed)
    picks: dict[int, list[str]] = defaultdict(list)
    for name, scores in (("embedding", emb), ("bm25", bm25)):
        for i in np.argsort(scores)[::-1][:args.top]:
            picks[int(i)].append(name)
    # The control decides the whole test, so it is drawn from what neither
    # scorer picked: articles that merely exist in the pool.
    remaining = [i for i in range(len(rows)) if i not in picks]
    for i in rng.sample(remaining, min(args.control, len(remaining))):
        picks[i].append("random")

    # Feed-matched control. A scorer whose picks come mostly from one feed may be
    # doing nothing more than preferring that feed, which the app can already do
    # per feed without any model. Drawing one random article from the same feed
    # for each pick holds the feed mix fixed, so what is left is the choice made
    # inside a feed.
    if args.feed_matched:
        by_feed = defaultdict(list)
        for i, row in enumerate(rows):
            if i not in picks:
                by_feed[row.get("feed_id")].append(i)
        wanted = [rows[i].get("feed_id") for i, s in picks.items()
                  if "embedding" in s]
        rng.shuffle(wanted)
        for feed in wanted[:args.feed_matched]:
            candidates = by_feed.get(feed) or []
            if candidates:
                j = candidates.pop(rng.randrange(len(candidates)))
                picks[j].append("same-feed")

    order = list(picks)
    rng.shuffle(order)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["n", "want", "title", "url"])
        for n, i in enumerate(order, 1):
            w.writerow([n, "", rows[i]["title"], rows[i].get("url", "")])
    args.key.write_text(json.dumps({
        "seed": args.seed, "model": args.model, "input": args.input,
        "top": args.top, "control": args.control,
        "moderate_penalty": args.moderate_penalty,
        "pool_size": len(rows),
        "selectivity": args.top / len(rows),
        "profile_positive_units": positive,
        "rows": [{"n": n, "article_id": rows[i]["article_id"],
                  "sources": picks[i], "feed_id": rows[i].get("feed_id"),
                  "embedding": float(emb[i]), "bm25": float(bm25[i])}
                 for n, i in enumerate(order, 1)],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    overlap = sum(1 for s in picks.values() if "embedding" in s and "bm25" in s)
    print(f"pool: {len(rows)} unscored articles", file=sys.stderr)
    print(f"candidates: {len(order)} rows in {args.out} "
          f"({args.top} per scorer = top {args.top / len(rows):.1%} of the pool, "
          f"{overlap} picked by both, {args.control} random controls)",
          file=sys.stderr)
    # A pick list drawn from one feed would say the feed is on topic, not that
    # the scorer can choose; the pool itself is concentrated, so report both.
    pool_feeds = Counter(r.get("feed_id") for r in rows)
    for name in ("embedding", "bm25"):
        chosen = Counter(rows[i].get("feed_id") for i, s in picks.items()
                         if name in s)
        top_feed, count = chosen.most_common(1)[0]
        print(f"{name}: picks span {len(chosen)} feeds, biggest is "
              f"{count}/{sum(chosen.values())} from feed {top_feed} "
              f"(that feed is {pool_feeds[top_feed] / len(rows):.0%} of the pool)",
              file=sys.stderr)
    print(f"key written to {args.key} - do not open it before judging",
          file=sys.stderr)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Interval for a rate measured on a handful of articles.

    Printed next to every group because a review of this size cannot separate
    20% from 15%, and a bare pair of percentages invites reading a difference
    that is not there.
    """
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """p for the 2x2 table [[a, b], [c, d]]; exact, the counts are tiny."""
    total = a + b + c + d
    if not total or not (a + b) or not (c + d):
        return 1.0

    def prob(x: int) -> float:
        return (math.comb(a + b, x) * math.comb(c + d, a + c - x)
                / math.comb(total, a + c))

    observed = prob(a)
    lo, hi = max(0, a + c - (c + d)), min(a + b, a + c)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1)
                        if prob(x) <= observed * (1 + 1e-9)))


def needed_to_clear(control_k: int, control_n: int, n: int) -> int | None:
    """Hits out of n that would make the difference from the control significant.

    Reported so a null result is readable as either "the scorer does nothing" or
    "this review was too small to tell", which are very different conclusions.
    """
    for k in range(control_k, n + 1):
        if fisher_two_sided(k, n - k, control_k, control_n - control_k) < 0.05:
            return k
    return None


YES = {"y", "yes", "1", "ano", "a"}
# Blank is deliberately not a "no". A half-finished review would otherwise come
# out as "the scorers found nothing", which is the one wrong answer this test
# could produce without anybody noticing.
NO = {"n", "no", "0", "ne"}


def score(args) -> None:
    key = json.loads(args.key.read_text(encoding="utf-8"))
    by_n = {r["n"]: r for r in key["rows"]}

    verdicts: dict[int, bool] = {}
    with args.review.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("want") or "").strip().lower()
            if raw in YES:
                verdicts[int(row["n"])] = True
            elif raw in NO:
                verdicts[int(row["n"])] = False
            elif not raw:
                raise SystemExit(f"row {row['n']} has no verdict; every row needs "
                                 f"an explicit y or n, blanks are not counted as no")
            else:
                raise SystemExit(f"row {row['n']}: cannot read want={raw!r}")

    missing = set(by_n) - set(verdicts)
    if missing:
        raise SystemExit(f"{len(missing)} rows have no verdict: "
                         f"{sorted(missing)[:10]}")

    groups = defaultdict(lambda: {"n": 0, "want": 0})
    for n, want in verdicts.items():
        for source in by_n[n]["sources"]:
            groups[source]["n"] += 1
            groups[source]["want"] += want

    control = groups.get("random", {"n": 0, "want": 0})
    base = (control["want"] / control["n"]) if control["n"] else None
    labels = {"embedding": "embedding", "bm25": "BM25", "random": "random",
              "same-feed": "same feed"}
    ck, cn = control["want"], control["n"]
    print(f"\n{'group':<12}{'wanted':>10}{'rate':>7}{'95% CI':>16}"
          f"{'vs control':>12}")
    for name in ("embedding", "bm25", "same-feed", "random"):
        g = groups.get(name)
        if not g or not g["n"]:
            continue
        k, gn = g["want"], g["n"]
        lo, hi = wilson(k, gn)
        p = ("" if name == "random" or not cn
             else f"p={fisher_two_sided(k, gn - k, ck, cn - ck):.2f}")
        print(f"{labels[name]:<12}{f'{k}/{gn}':>10}{k/gn:>6.0%}"
              f"{f'[{lo:.0%}, {hi:.0%}]':>16}{p:>12}")

    if base is None or base == 0:
        print("\nNo control articles were wanted, so any hit rate above zero is "
              "something, but the size of it cannot be read off this sample.")
        return

    print(f"\nControl rate is {base:.0%}: that is what picking at random from the "
          f"unscored pool gets you.")

    # Say plainly when the review was too small to answer, instead of letting a
    # point estimate read as a result. A group that beats the control by one
    # article has shown nothing.
    emb = groups.get("embedding") or {"n": 0, "want": 0}
    if emb["n"]:
        need = needed_to_clear(ck, cn, emb["n"])
        significant = [labels[n] for n in ("embedding", "bm25")
                       if groups.get(n) and groups[n]["n"]
                       and fisher_two_sided(groups[n]["want"],
                                            groups[n]["n"] - groups[n]["want"],
                                            ck, cn - ck) < 0.05]
        if significant:
            print(f"Clears the control at p<0.05: {', '.join(significant)}.")
        elif need is not None:
            print(f"NOTHING here clears the control. A scorer would have needed "
                  f"{need}/{emb['n']} ({need / emb['n']:.0%}) to do so, so this "
                  f"review cannot tell a useless scorer from a decent one - only "
                  f"rule out a spectacular one.")
        else:
            print("NOTHING here clears the control, and no result at this sample "
                  "size could have; the review needs to be far larger.")

    same_feed = groups.get("same-feed")
    if same_feed and same_feed["n"] and emb["n"]:
        sk, sn = same_feed["want"], same_feed["n"]
        p = fisher_two_sided(emb["want"], emb["n"] - emb["want"], sk, sn - sk)
        print(f"\nSame-feed control {sk}/{sn} ({sk/sn:.0%}) against the "
              f"embedding's {emb['want']}/{emb['n']} ({emb['want']/emb['n']:.0%}), "
              f"p={p:.2f}.")
        print("  This is the comparison that separates choosing an article from "
              "choosing a feed" + (", and it is decided." if p < 0.05 else
                                   ", and at this size it decides nothing."))

    both = [n for n in verdicts if set(by_n[n]["sources"]) >= {"embedding", "bm25"}]
    only_emb = [n for n in verdicts if by_n[n]["sources"] == ["embedding"]]
    if both:
        print(f"\n{len(both)} articles were picked by both scorers "
              f"({sum(verdicts[n] for n in both)} wanted). The semantic model "
              f"only earns its keep on what word matching missed:")
    if only_emb:
        want = sum(verdicts[n] for n in only_emb)
        lo, hi = wilson(want, len(only_emb))
        p = fisher_two_sided(want, len(only_emb) - want, ck, cn - ck)
        print(f"  embedding alone: {want}/{len(only_emb)} wanted "
              f"({want / len(only_emb):.0%}, [{lo:.0%}, {hi:.0%}], p={p:.2f} "
              f"against the control)")
        print("  This is the number the sidecar rests on: what the model finds "
              "that word matching does not.")
    else:
        print("  the embedding picked nothing BM25 did not; on this pool the "
              "semantic model adds no selection of its own")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="score the pool and write a blind review list")
    p.add_argument("--sample", required=True, type=Path,
                   help="jsonl from `export_sample.py --unscored`")
    p.add_argument("--profile-from", type=Path, default=None,
                   help="take the profile text from another export's meta")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--key", required=True, type=Path)
    p.add_argument("--model", default="intfloat/multilingual-e5-small")
    p.add_argument("--prefix-family", default=None, choices=sorted(PREFIXES))
    p.add_argument("--input", default="title300",
                   choices=["title", "title300", "full2000"])
    p.add_argument("--top", type=int, default=20, help="picks per scorer")
    p.add_argument("--control", type=int, default=20,
                   help="random articles mixed in; without these the result is "
                        "unreadable, so this is not a knob to turn to zero")
    p.add_argument("--feed-matched", type=int, default=18,
                   help="random articles drawn from the same feeds the embedding "
                        "picked from, to tell choosing an article apart from "
                        "choosing a feed")
    p.add_argument("--moderate-penalty", type=float, default=1.0,
                   help="discount for topics from the 'Moderate relevance' line, "
                        "in standard deviations of the scorer's own similarities. "
                        "Defaults to the value that measured best on the scored "
                        "sample, so the review sees the embedding at its best")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--seed", type=int, default=20260824)
    p.set_defaults(func=prepare)

    s = sub.add_parser("score", help="read the filled-in verdicts")
    s.add_argument("--review", required=True, type=Path)
    s.add_argument("--key", required=True, type=Path)
    s.set_defaults(func=score)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
