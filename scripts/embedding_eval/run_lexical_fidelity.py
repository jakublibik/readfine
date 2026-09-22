# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Phase 2: check the shipped lexical scorer against the measured baseline.

`run_eval.py` scored BM25 with scikit-learn over the whole rated batch at once.
The app has neither scikit-learn nor a batch: it scores one article at a time
against a stored term→document-frequency table. This script runs the *shipped*
function (`app.services.relevance_service`) over the same sample and answers the
two questions that separates:

1. **Exactness.** Given the same corpus statistics the reference was fitted on,
   does the pure-Python BM25 reproduce the scikit-learn one? Anything but a
   rounding difference is a bug in the port.
2. **Cost of the production shape.** Swapping the per-batch fit for a table built
   over a rolling window at `min_df >= 3`, and dropping bigrams, are decisions
   the app has to make. Each is run as its own variant, so the cost shows up as a
   number instead of an assumption.

    uv run --script run_lexical_fidelity.py --sample sample.jsonl
    uv run --script run_lexical_fidelity.py --sample sample.jsonl --calibrate

The published baseline this has to land on: cold start (3 topics, no avoid list)
AUC ~0.563, generated profile ~0.62, LLM 0.73 on the same rows.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "backend"))

import run_eval  # noqa: E402  (the reference implementation, imported not copied)
from app.services import relevance_service as rs  # noqa: E402
from app.services.ai_eval_service import compute_auc  # noqa: E402


def reference_scores(texts: list[str], positive: list[str]) -> list[float]:
    """The scikit-learn BM25 from `run_eval`, positives only, plain max."""
    return run_eval.bm25_scores(texts, positive, [], [False] * len(positive),
                                0.0, "max", 3).tolist()


def shipped_scores(texts: list[str], positive: list[str],
                   stats: rs.CorpusStats) -> list[float]:
    return [rs.bm25_raw(t, positive, stats) for t in texts]


def with_reference_params(fn):
    """Run `fn` with the constants the scikit-learn baseline was measured with.

    The shipped scorer turns length normalization off; the reference does not.
    The exactness check has to compare like with like, so it borrows the
    reference's b, and the cost of the deviation is then measured separately as
    its own variant.
    """
    original = rs.BM25_B
    rs.BM25_B = run_eval.BM25_B
    try:
        return fn()
    finally:
        rs.BM25_B = original


def auc(scores: list[float], engaged: list[bool]) -> float | None:
    return compute_auc(list(zip(scores, engaged)))


def fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def table_growth(texts: list[str], min_df: int) -> list[dict]:
    """How the term table grows with the corpus, for sizing the stored table.

    The window the app builds the table over is an order of magnitude larger than
    this sample, so the question is not how many terms the sample has but whether
    the count grows linearly or flattens. Heaps' law says it flattens; this
    checks it on the actual text rather than on the law.
    """
    out = []
    for share in (0.25, 0.5, 1.0):
        subset = texts[:int(len(texts) * share)]
        shipped = rs.build_corpus_stats(subset, min_df=min_df, ngram_max=1)
        with_bigrams = rs.build_corpus_stats(subset, min_df=min_df, ngram_max=2)
        out.append({"docs": len(subset),
                    "terms": len(shipped.doc_freq),
                    "terms_with_bigrams": len(with_bigrams.doc_freq)})
    return out


def threshold_shares(raw: list[float], k: float) -> dict:
    """Share of articles a filter at each threshold would let through."""
    squashed = np.asarray(raw) / (np.asarray(raw) + k)
    return {str(t): round(float((squashed >= t).mean()), 4)
            for t in (0.3, 0.5, 0.6, 0.7)}


def calibrate_k(raw: list[float], llm: list[float]) -> dict:
    """Pick the squash constant that puts the two score distributions on one scale.

    Matched on deciles rather than on the mean: the LLM quantizes to a handful of
    values and piles articles on them, so a mean would be dragged around by where
    the heaps sit. Deciles only ask that the same share of articles sits below
    the same number, which is what a threshold in a filter actually reads.

    Nothing here changes the ranking — the squash is monotonic — so a badly
    chosen k costs meaning in the UI and in filter thresholds, not accuracy.
    """
    quantiles = np.arange(0.1, 1.0, 0.1)
    target = np.quantile(llm, quantiles)
    raw_arr = np.asarray(raw)
    best = None
    for k in np.arange(0.1, 60.0, 0.05):
        got = np.quantile(raw_arr / (raw_arr + k), quantiles)
        err = float(np.abs(got - target).mean())
        if best is None or err < best[1]:
            best = (float(k), err, got)
    k, err, got = best
    return {"k": round(k, 2), "mean_decile_error": round(err, 4),
            "llm_deciles": [round(float(v), 3) for v in target],
            "squashed_deciles": [round(float(v), 3) for v in got]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--min-df", type=int, default=3,
                    help="document-frequency cutoff for the production-shaped "
                         "term table (the reference fit uses 2)")
    ap.add_argument("--retention-margin-days", type=int, default=5)
    ap.add_argument("--calibrate", action="store_true",
                    help="also fit the 0..1 squash constant against the LLM "
                         "score distribution")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    meta, rows = run_eval.load_sample(args.sample)
    rows, retention = run_eval.drop_post_retention(meta, rows,
                                                   args.retention_margin_days)
    if not rows:
        raise SystemExit("no rows left after the retention cut")
    for i, row in enumerate(rows):
        row["_index"] = i
    texts = [run_eval.article_text(r, "title300") for r in rows]

    # The production table is built over a window of articles, not over the rated
    # segment: every article in the sample counts, including the ones a segment
    # does not contain.
    global_stats = rs.build_corpus_stats(texts, min_df=args.min_df, ngram_max=1)
    global_bigram = rs.build_corpus_stats(texts, min_df=args.min_df, ngram_max=2)
    print(f"sample: {len(rows)} articles after the retention cut "
          f"({retention.get('dropped', 0)} dropped)", file=sys.stderr)
    print(f"term table: {len(global_stats.doc_freq)} terms at min_df={args.min_df} "
          f"(with bigrams: {len(global_bigram.doc_freq)}), "
          f"avg doc len {global_stats.avg_doc_len:.1f}", file=sys.stderr)

    segments = run_eval.assign_segments(meta, rows)
    segments.pop("_older")
    collected: dict[str, list[dict]] = {"generated": [], "cold_start_3": []}

    report = {
        "sample": {"file": str(args.sample), "rows": len(rows),
                   "exported_at": meta.get("exported_at"),
                   "retention_cut": retention},
        "term_table": {
            "min_df": args.min_df,
            "terms": len(global_stats.doc_freq),
            "terms_with_bigrams": len(global_bigram.doc_freq),
            "avg_doc_len": round(global_stats.avg_doc_len, 2),
        },
        "segments": {},
    }

    for name in ("P2", "P3"):
        seg = segments[name]
        seg_rows = seg["rows"]
        if not seg_rows:
            continue
        idx = [r["_index"] for r in seg_rows]
        seg_texts = [texts[i] for i in idx]
        engaged = [bool(r["engaged"]) for r in seg_rows]
        llm = [r["ai_score"] for r in seg_rows]

        # The reference fits its vocabulary on the segment it scores at min_df=2
        # with bigrams, so the exactness check has to hand the shipped scorer the
        # same corpus and the same n-gram range. The shipped default is unigrams;
        # that deviation is measured separately, below.
        seg_stats = rs.build_corpus_stats(seg_texts, min_df=2, ngram_max=2)

        seg_report = {"n": len(seg_rows), "engaged": sum(engaged),
                      "auc_llm": auc(llm, engaged), "variants": {}}

        for variant, topics in (("generated", None), ("cold_start_3", 3)):
            ref_pos, mask, _neg = run_eval.parse_profile(
                seg["profile"], "b", None, topics)
            profile = rs.parse_profile(seg["profile"])
            pos = profile.positive[:topics] if topics else profile.positive

            reference = reference_scores(seg_texts, ref_pos)
            ours_same_corpus = with_reference_params(
                lambda: shipped_scores(seg_texts, pos, seg_stats))
            ours_production = shipped_scores(seg_texts, pos, global_stats)
            ours_bigram = shipped_scores(seg_texts, pos, global_bigram)

            ref_arr = np.asarray(reference)
            ours_arr = np.asarray(ours_same_corpus)
            scale = float(np.abs(ref_arr).max()) or 1.0
            seg_report["variants"][variant] = {
                "profile_units_match": ref_pos == pos,
                "profile_units": pos,
                "auc": {
                    "reference_sklearn": auc(reference, engaged),
                    "shipped_same_corpus": auc(ours_same_corpus, engaged),
                    "shipped_production_table": auc(ours_production, engaged),
                    "shipped_with_bigrams": auc(ours_bigram, engaged),
                    "shipped_with_length_norm": auc(with_reference_params(
                        lambda: shipped_scores(seg_texts, pos, global_stats)), engaged),
                },
                "port_check": {
                    "max_abs_diff_rel": float(np.abs(ref_arr - ours_arr).max() / scale),
                    "spearman_like_rank_diff": int(
                        (np.argsort(np.argsort(-ref_arr))
                         != np.argsort(np.argsort(-ours_arr))).sum()),
                },
            }
            collected[variant].append({
                "scores": {"bigram": ours_bigram, "unigram": ours_production},
                "engaged": engaged,
                "llm": llm,
            })
            if args.calibrate and variant == "generated":
                seg_report["variants"][variant]["calibration"] = calibrate_k(
                    ours_production, llm)

        report["segments"][name] = seg_report

    # ── decisions that need the whole sample, not one segment ────────────────
    report["table_growth"] = table_growth(texts, args.min_df)
    report["bigram_delta"] = {
        variant: run_eval.paired_bootstrap(segs, "bigram", "unigram", 1000, 20260922)
        for variant, segs in collected.items() if segs
    }
    if args.calibrate:
        pooled_raw = [s for seg in collected["generated"]
                      for s in seg["scores"]["unigram"]]
        pooled_llm = [s for seg in collected["generated"] for s in seg["llm"]]
        pooled = calibrate_k(pooled_raw, pooled_llm)
        pooled["threshold_shares"] = threshold_shares(pooled_raw, pooled["k"])
        pooled["threshold_shares_llm"] = {
            str(t): round(float((np.asarray(pooled_llm) >= t).mean()), 4)
            for t in (0.3, 0.5, 0.6, 0.7)}
        pooled["cold_start_threshold_shares"] = threshold_shares(
            [s for seg in collected["cold_start_3"]
             for s in seg["scores"]["unigram"]], pooled["k"])
        report["calibration_pooled"] = pooled

    # ── printout ──────────────────────────────────────────────────────────────
    for name, seg in report["segments"].items():
        print(f"\n{name}: n={seg['n']}, engaged={seg['engaged']}, "
              f"LLM AUC {fmt(seg['auc_llm'])}")
        for variant, v in seg["variants"].items():
            a = v["auc"]
            print(f"  {variant:14s} units={len(v['profile_units'])} "
                  f"units_match={v['profile_units_match']}")
            print(f"    sklearn ref      {fmt(a['reference_sklearn'])}")
            print(f"    shipped (same)   {fmt(a['shipped_same_corpus'])}  "
                  f"max rel diff {v['port_check']['max_abs_diff_rel']:.2e}, "
                  f"rank moves {v['port_check']['spearman_like_rank_diff']}")
            print(f"    shipped (table)  {fmt(a['shipped_production_table'])}")
            print(f"    shipped (+bigram){fmt(a['shipped_with_bigrams'])}")
            print(f"    shipped (+len norm) {fmt(a['shipped_with_length_norm'])}")
            if "calibration" in v:
                c = v["calibration"]
                print(f"    squash k={c['k']} (mean decile error "
                      f"{c['mean_decile_error']})")
                print(f"      llm      {c['llm_deciles']}")
                print(f"      squashed {c['squashed_deciles']}")

    print("\nterm table growth (min_df=%d):" % args.min_df)
    for row in report["table_growth"]:
        print(f"  {row['docs']:6d} docs -> {row['terms']:7d} terms "
              f"({row['terms_with_bigrams']} with bigrams)")

    print("\nbigrams minus unigrams (pooled AUC, paired bootstrap):")
    for variant, d in report["bigram_delta"].items():
        if d.get("n_iter"):
            print(f"  {variant:14s} {d['mean']:+.3f} "
                  f"[{d['ci_low']:+.3f}, {d['ci_high']:+.3f}]")

    if "calibration_pooled" in report:
        c = report["calibration_pooled"]
        print(f"\npooled squash k={c['k']} (mean decile error "
              f"{c['mean_decile_error']})")
        print(f"  llm deciles      {c['llm_deciles']}")
        print(f"  squashed deciles {c['squashed_deciles']}")
        print(f"  share >= threshold, lexical {c['threshold_shares']}")
        print(f"                     llm      {c['threshold_shares_llm']}")
        print(f"  cold start 3               {c['cold_start_threshold_shares']}")

    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
