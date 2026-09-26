# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Step 2b: confirm the chosen scorer settings on a window they were not picked on.

E3 and E3b chose the truncation settings from a grid over the September window
(`sample_clean`, from 24 August). The earlier export (`sample.jsonl.gz`, June to
22 August) never took part in that choice, so it is the out-of-sample check:
only rows fetched before 24 August are used, so the two windows share nothing.

Two differences from the September runs, both unavoidable: the text is the
exported body (readable where there was one, see step 0f of the plan for why
that barely matters), and the document frequencies still come from the
September corpus, which is also what production would have.

    uv run --script run_terms_confirm.py --old-sample sample.jsonl.gz \\
        --corpus corpus.jsonl.gz --terms-dir terms_v1
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
import run_terms_eval as te  # noqa: E402
import run_terms_trunc as tt  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

CUTOFF = datetime(2026, 8, 24, tzinfo=timezone.utc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--old-sample", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--terms-dir", required=True, type=Path)
    ap.add_argument("--bootstrap", type=int, default=1000)
    args = ap.parse_args()

    meta, rows = run_eval.load_sample(args.old_sample)
    rows, retention = run_eval.drop_post_retention(meta, rows, 5)
    rows = [r for r in rows if run_eval.parse_dt(r["fetched_at"]) < CUTOFF]
    labels = np.array([bool(r["engaged"]) for r in rows])
    texts = [run_eval.article_text(r, "title300") for r in rows]
    czech = np.array([te.is_czech(t) for t in texts])
    docs = [rs.tokenize(t) for t in texts]
    print(f"old window: {len(rows)} rows ({labels.sum()} engaged), czech "
          f"{czech.sum()} ({labels[czech].sum()} engaged); retention cut "
          f"{retention}", file=sys.stderr)

    corpus = te.load_corpus(args.corpus)
    corpus_tokens = [rs.tokenize(rs.article_text(o["title"], o["body"]))
                     for o in corpus.values()]
    tables = te.Tables(corpus_tokens, set(), [])

    en = te.read_terms(args.terms_dir / "en.txt")
    cs = te.read_terms(args.terms_dir / "cs_translations.txt")
    cold = te.read_terms(args.terms_dir / "cold_start.txt")
    profiles = {"en": en, "en_cs": en + cs, "cold3": cold}
    variants = [("no-trunc", None, None, 0.0),
                ("fixed N=4 w=0.3", None, 4, 0.3),
                ("rel k=2 w=0.5 (chosen)", 2, None, 0.5),
                ("rel k=2 w=0.3", 2, None, 0.3),
                ("rel k=2 w=1.0", 2, None, 1.0)]
    needed = {tt.prefix_of(q, k, fx) for _, k, fx, w in variants if w
              for terms in profiles.values() for u in terms
              for q in rs.tokenize(u)}
    needed.discard(None)
    pdf = tt.prefix_df(corpus_tokens, needed)

    scores = {}
    print(f"\n{'profile':6s} {'variant':24s} {'AUC':>6s} {'top100':>7s} "
          f"{'cs':>6s} {'other':>6s}")
    for pname, terms in profiles.items():
        units = [rs.tokenize(u) for u in terms]
        for name, k, fx, w in variants:
            s = np.array([tt.score(d, units, tables, pdf, k, fx, w, None)
                          for d in docs])
            scores[(pname, name)] = s
            print(f"{pname:6s} {name:24s} {te.auc_fast(s, labels):6.3f} "
                  f"{te.top_hit(s, labels):7.0%} "
                  f"{te.auc_fast(s[czech], labels[czech]):6.3f} "
                  f"{te.auc_fast(s[~czech], labels[~czech]):6.3f}")

    print("\nvs no-trunc (mean [95% CI]):")
    for pname in profiles:
        base = scores[(pname, "no-trunc")]
        for name, *_ in variants[1:]:
            s = scores[(pname, name)]
            parts = []
            for sl, m in (("all", np.ones_like(czech)), ("cs", czech),
                          ("other", ~czech)):
                d = te.paired_bootstrap(s[m], base[m], labels[m], args.bootstrap)
                parts.append(f"{sl} {d[0]:+.3f} [{d[1]:+.3f},{d[2]:+.3f}]")
            print(f"  {pname:6s} {name:24s} " + "  ".join(parts))
    d = te.paired_bootstrap(scores[("en_cs", "no-trunc")], scores[("en", "no-trunc")],
                            labels, args.bootstrap)
    print(f"\nczech translations (en_cs - en, no-trunc): {d[0]:+.3f} "
          f"[{d[1]:+.3f}, {d[2]:+.3f}]")


if __name__ == "__main__":
    main()
