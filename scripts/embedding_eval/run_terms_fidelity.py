# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Step 2 of the relevance plan: the shipped scorer against the eval prototype.

The configuration chosen in step 2b (term list as loose words, relative prefix
k=2 at weight 0.5, script-aware tokenization, instance-wide document
frequencies) was measured through `run_terms_trunc.score`. This runs the ported
`relevance_service.bm25_raw` over the same rows and must reproduce it: every
score equal, and the AUCs the plan records (0.659 on the September window,
0.676 on the summer one, see `EXPECTED`). A difference here is a bug in the
port.

    uv run --script run_terms_fidelity.py --sample sample_clean.jsonl \\
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
import run_terms_trunc as tt  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

# The plan records 0.677 for the summer window, measured with the tokenizer of
# the time. Script-aware tokenization moves it to 0.676: a Latin word glued to
# CJK text ("OpenAI发布") used to be one token and now counts towards the
# document frequency of "openai". With the old tokenizer patched back in, both
# windows reproduce the recorded numbers exactly.
EXPECTED = {"september": 0.659, "summer": 0.676}


def windows(args) -> tuple[dict[str, tuple[list[str], np.ndarray]], list[str]]:
    corpus = te.load_corpus(args.corpus)

    august_meta, august_rows = run_eval.load_sample(args.august_sample)
    meta, rows = run_eval.load_sample(args.sample)
    # P2 was scored against the August profile, see step 0 of the plan.
    meta["profile"]["previous"] = august_meta["profile"]["current"]
    rows = [r for r in run_eval.assign_segments(meta, rows)["P2"]["rows"]
            if r["article_id"] in corpus]
    sept = ([rs.article_text(r["title"], corpus[r["article_id"]]["body"])
             for r in rows], np.array([bool(r["engaged"]) for r in rows]))

    rows, _ = run_eval.drop_post_retention(august_meta, august_rows, 5)
    rows = [r for r in rows if run_eval.parse_dt(r["fetched_at"]) < tc.CUTOFF]
    summer = ([run_eval.article_text(r, "title300") for r in rows],
              np.array([bool(r["engaged"]) for r in rows]))

    corpus_texts = [rs.article_text(o["title"], o["body"]) for o in corpus.values()]
    return {"september": sept, "summer": summer}, corpus_texts


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
    assert rs.parse_terms("\n".join(terms)) == terms, "parser changed the terms"

    stats = rs.build_corpus_stats(corpus_texts, min_df=te.MIN_DF)
    corpus_tokens = [rs.tokenize(t) for t in corpus_texts]
    tables = te.Tables(corpus_tokens, set(), [])
    units = [rs.tokenize(u) for u in terms]
    pdf = tt.prefix_df(corpus_tokens, {tt.prefix_of(q, rs.PREFIX_CUT, None)
                                       for u in units for q in u} - {None})
    print(f"corpus {stats.n_docs} docs, {len(stats.doc_freq)} tokens, "
          f"{len(stats.prefix_freq)} prefixes", file=sys.stderr)

    ok = True
    for name, (texts, labels) in data.items():
        shipped = np.array([rs.bm25_raw(t, terms, stats).score for t in texts])
        proto = np.array([tt.score(rs.tokenize(t), units, tables, pdf,
                                   rs.PREFIX_CUT, None, rs.PREFIX_WEIGHT, None)
                          for t in texts])
        diff = float(np.abs(shipped - proto).max())
        auc = te.auc_fast(shipped, labels)
        squashed = te.auc_fast(np.array([rs.squash(s) for s in shipped]), labels)
        match = diff < 1e-9 and round(auc, 3) == EXPECTED[name]
        ok &= match
        print(f"{name:10s} rows {len(texts):5d} engaged {labels.sum():4d}  "
              f"AUC {auc:.4f} (expected {EXPECTED[name]}, squashed {squashed:.4f})  "
              f"max |shipped - prototype| {diff:.2e}  {'OK' if match else 'MISMATCH'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
