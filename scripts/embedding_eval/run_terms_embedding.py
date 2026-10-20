# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "sentence-transformers>=3.0",
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "regex>=2026.2.28",
#   "sqlalchemy[asyncio]>=2.0",
# ]
# ///
"""Embedding over the term list, against the shipped BM25 over the same list.

The earlier embedding runs (`run_eval.py`) read the AI profile, a prose
description, while the basic scorer that shipped reads a term list. Comparing
the two mixes up the scorer with its input. This holds the input fixed: same
rows, same text (`relevance_service.article_text`), same frozen terms
(`terms_v1`), and only the scorer changes.

Each term is one query unit, the article score is its best cosine (or the mean
of the k best). The term list has no avoid side, so nothing is subtracted. The
prose profile is scored too, on the September window where its text is known,
so the effect of the input can be read on its own.

    uv run --script run_terms_embedding.py --sample sample_clean.jsonl \\
        --august-sample sample.jsonl --corpus corpus.jsonl.gz \\
        --terms-dir terms_v1 --model intfloat/multilingual-e5-small
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
import run_terms_eval as te  # noqa: E402
import run_terms_fidelity as tf  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402


def embed_scores(model, prefixes, texts, units, aggregate, top_k, batch_size,
                 negative=None):
    q, p = prefixes
    art = run_eval.embed(model, texts, p, batch_size)
    pos = run_eval.embed(model, units, q, batch_size)
    neg = run_eval.embed(model, negative, q, batch_size) if negative else None
    return run_eval.profile_score(art, pos, neg, [False] * len(units), 0.0,
                                  aggregate, top_k)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--terms-dir", required=True, type=Path)
    ap.add_argument("--model", action="append", default=None)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    models = args.model or ["intfloat/multilingual-e5-small"]

    data, corpus_texts = tf.windows(args)
    en = te.read_terms(args.terms_dir / "en.txt")
    cs = te.read_terms(args.terms_dir / "cs_translations.txt")
    cold = te.read_terms(args.terms_dir / "cold_start.txt")
    term_lists = {"terms_en_cs": en + cs, "terms_en": en, "terms_cold3": cold}

    august_meta, _ = run_eval.load_sample(args.august_sample)
    prose_pos, _, prose_neg = run_eval.parse_profile(
        august_meta["profile"]["current"], "b", None)

    stats = rs.build_corpus_stats(corpus_texts, min_df=te.MIN_DF)

    from sentence_transformers import SentenceTransformer

    results, deltas = {}, {}
    for window, (texts, labels) in data.items():
        print(f"\n== {window}: {len(texts)} rows, {labels.sum()} engaged", flush=True)
        bm25 = {k: np.array([rs.bm25_raw(t, v, stats).score for t in texts])
                for k, v in term_lists.items()}
        for k, s in bm25.items():
            results[f"{window} | BM25 {k}"] = {
                "auc": te.auc_fast(s, labels), "top100": te.top_hit(s, labels)}

        for name in models:
            model = SentenceTransformer(name)
            prefixes = run_eval.PREFIXES[run_eval.default_prefix_family(name)]
            short = name.split("/")[-1]
            emb = {}
            for k, v in term_lists.items():
                for agg in ("max", "topk"):
                    emb[(k, agg)] = embed_scores(model, prefixes, texts, v, agg,
                                                 args.top_k, args.batch_size)
            if window == "september":
                for agg in ("max", "topk"):
                    emb[("prose_profile", agg)] = embed_scores(
                        model, prefixes, texts, prose_pos, agg, args.top_k,
                        args.batch_size, prose_neg)
                    emb[("prose_cold3", agg)] = embed_scores(
                        model, prefixes, texts, prose_pos[:3], agg, args.top_k,
                        args.batch_size, prose_neg)
            for (k, agg), s in emb.items():
                key = f"{window} | {short} {k} {agg}"
                results[key] = {"auc": te.auc_fast(s, labels),
                                "top100": te.top_hit(s, labels)}
                line = f"{key:60s} {results[key]['auc']:6.3f}"
                if k in bm25:
                    d = te.paired_bootstrap(s, bm25[k], labels, args.bootstrap)
                    deltas[f"{key} - BM25"] = d
                    line += (f"  vs BM25 {results[f'{window} | BM25 {k}']['auc']:.3f}"
                             f"  {d[0]:+.3f} [{d[1]:+.3f}, {d[2]:+.3f}]")
                print(line, flush=True)
            del model

    print(f"\n{'variant':60s} {'AUC':>6s} {'top100':>7s}")
    for name, r in results.items():
        print(f"{name:60s} {r['auc']:6.3f} {r['top100']:7.0%}")
    print("\npaired bootstrap, embedding - BM25 on the same terms "
          "(mean, 95% CI, share > 0):")
    for k, (m, lo, hi, p) in deltas.items():
        print(f"  {m:+.3f} [{lo:+.3f}, {hi:+.3f}] {p:4.0%}  {k}")

    if args.out:
        args.out.write_text(json.dumps({"results": results, "deltas": deltas},
                                       indent=2))


if __name__ == "__main__":
    main()
