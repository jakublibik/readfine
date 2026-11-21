# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "regex>=2026.2.28",
#   "sqlalchemy[asyncio]>=2.0",
# ]
# ///
"""Translation plan, Phase 2 step 0: how much does the snippet add to basic relevance?

A translated article has an English title and a body in the original language.
The body matches none of the reader's terms, and the scorer has no length
normalization (BM25 with b = 0), so such an article scores exactly as its title
alone would. The question is whether translating the first 300 characters of the
description as well would be worth it, which is the gap between "title only"
and "title + 300 characters" on articles where both are in the reader's language.

Same rows, corpus and terms as `run_terms_fidelity.py`, scored through the
shipped `relevance_service`. The title300 variant must reproduce the recorded
AUCs (0.659 / 0.676), otherwise the setup drifted and the comparison means
nothing.

    uv run --script run_translation_snippet.py --sample sample_clean.jsonl \\
        --august-sample sample.jsonl --corpus corpus.jsonl.gz \\
        --terms-dir terms_v1
"""
import argparse
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
import run_terms_confirm as tc  # noqa: E402
import run_terms_eval as te  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

EXPECTED = {"september": 0.659, "summer": 0.676}


def windows(args):
    """Per window: (titles, title + 300 texts, engaged labels, is-Czech mask)."""
    corpus = te.load_corpus(args.corpus)

    august_meta, august_rows = run_eval.load_sample(args.august_sample)
    meta, rows = run_eval.load_sample(args.sample)
    meta["profile"]["previous"] = august_meta["profile"]["current"]
    rows = [r for r in run_eval.assign_segments(meta, rows)["P2"]["rows"]
            if r["article_id"] in corpus]
    sept = ([r["title"] or "" for r in rows],
            [rs.article_text(r["title"], corpus[r["article_id"]]["body"]) for r in rows],
            np.array([bool(r["engaged"]) for r in rows]))

    rows, _ = run_eval.drop_post_retention(august_meta, august_rows, 5)
    rows = [r for r in rows if run_eval.parse_dt(r["fetched_at"]) < tc.CUTOFF]
    summer = ([r["title"] or "" for r in rows],
              [run_eval.article_text(r, "title300") for r in rows],
              np.array([bool(r["engaged"]) for r in rows]))

    corpus_texts = [rs.article_text(o["title"], o["body"]) for o in corpus.values()]
    return {"september": sept, "summer": summer}, corpus_texts


def report(label: str, title: np.ndarray, full: np.ndarray, labels: np.ndarray) -> None:
    a_t, a_f = te.auc_fast(title, labels), te.auc_fast(full, labels)
    mean, lo, hi, p = te.paired_bootstrap(full, title, labels)
    print(f"  {label:8s} rows {len(labels):5d} engaged {labels.sum():4d}  "
          f"AUC title {a_t:.3f}  title300 {a_f:.3f}  "
          f"gain {mean:+.3f} [{lo:+.3f}, {hi:+.3f}] P(gain>0) {p:.2f}")
    print(f"  {'':8s} matched (score > 0): title {np.mean(title > 0):.1%}, "
          f"title300 {np.mean(full > 0):.1%}   "
          f"top100 hit: title {te.top_hit(title, labels):.2f}, "
          f"title300 {te.top_hit(full, labels):.2f}   "
          f"engaged matched: title {np.mean(title[labels] > 0):.1%}, "
          f"title300 {np.mean(full[labels] > 0):.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--terms-dir", required=True, type=Path)
    args = ap.parse_args()

    data, corpus_texts = windows(args)
    terms = (te.read_terms(args.terms_dir / "en.txt")
             + te.read_terms(args.terms_dir / "cs_translations.txt"))
    stats = rs.build_corpus_stats(corpus_texts, min_df=te.MIN_DF)

    ok = True
    for name, (titles, fulls, labels) in data.items():
        title = np.array([rs.bm25_raw(t, terms, stats).score for t in titles])
        full = np.array([rs.bm25_raw(t, terms, stats).score for t in fulls])
        auc = te.auc_fast(full, labels)
        match = round(auc, 3) == EXPECTED[name]
        ok &= match
        print(f"{name}: title300 AUC {auc:.4f}, expected {EXPECTED[name]} "
              f"{'OK' if match else 'MISMATCH'}")
        report("all", title, full, labels)
        en = np.array([not te.is_czech(t) for t in fulls])
        report("non-cs", title[en], full[en], labels[en])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
