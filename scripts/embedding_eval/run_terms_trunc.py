# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Step 2b, E3b: truncation relative to the word, against the fixed N=4.

E3 picked a fixed prefix of four characters. It helps Czech, but reading where
it fires (E5) shows English conflations a fixed prefix cannot avoid:
longevity ~ long, meditation ~ media, stress ~ street. The textbook alternative
is light stemming: cut the last k characters of the query word (never below
four) and match the words that start with what is left. `skleróza` -> `sklero`
still meets `sklerózou`, `meditation` -> `meditat` no longer meets `media`.

Same data and setup as `run_terms_eval.py` (fetch-time text, instance-wide
document frequencies, P2 with the August profile), same bootstrap, per-language
slices.
"""
import argparse
import gzip
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
import run_terms_eval as te  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

MIN_PREFIX = 4


def prefix_of(tok: str, k: int | None, fixed: int | None) -> str | None:
    if len(tok) < MIN_PREFIX:
        return None
    if fixed:
        return tok[:fixed]
    return tok[:max(MIN_PREFIX, len(tok) - k)]


def prefix_df(corpus_tokens, prefixes: set[str]) -> dict[str, int]:
    lengths = sorted({len(p) for p in prefixes})
    df = {p: 0 for p in prefixes}
    for toks in corpus_tokens:
        seen = set()
        for t in set(toks):
            for L in lengths:
                if len(t) >= L:
                    p = t[:L]
                    if p in df:
                        seen.add(p)
        for p in seen:
            df[p] += 1
    return {p: c for p, c in df.items() if c >= te.MIN_DF}


def score(doc: list[str], units: list[list[str]], tables: te.Tables,
          pdf: dict[str, int], k: int | None, fixed: int | None, w: float,
          cap: int | None, fired: list | None = None) -> float:
    freqs: dict[str, int] = {}
    for t in doc:
        if t in tables.df:
            freqs[t] = freqs.get(t, 0) + 1
    best = 0.0
    for unit in units:
        s = 0.0
        for q in dict.fromkeys(unit):
            f = freqs.get(q, 0)
            if f and tables.idf(q):
                s += tables.idf(q) * te.sat(f)
                continue
            if not w:
                continue
            p = prefix_of(q, k, fixed)
            if not p or p not in pdf:
                continue
            hits = [t for t in doc if t != q and t.startswith(p)
                    and (cap is None or len(t) <= len(q) + cap)]
            if hits:
                s += w * te.idf_from(pdf[p], tables.n_docs) * te.sat(len(hits))
                if fired is not None:
                    fired.extend((q, t) for t in set(hits))
        best = max(best, s)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path)
    ap.add_argument("--terms-dir", required=True, type=Path)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--min-prefix", type=int, default=4,
                    help="floor under a relative prefix; 4 lets short words like "
                         "stress/model reach street/modern")
    args = ap.parse_args()
    global MIN_PREFIX
    MIN_PREFIX = args.min_prefix

    meta, rows = run_eval.load_sample(args.sample)
    with gzip.open(args.august_sample, "rt", encoding="utf-8") as f:
        meta["profile"]["previous"] = json.loads(f.readline())["profile"]["current"]
    corpus = te.load_corpus(args.corpus)
    rows = [r for r in run_eval.assign_segments(meta, rows)["P2"]["rows"]
            if r["article_id"] in corpus]
    labels = np.array([bool(r["engaged"]) for r in rows])
    texts = [rs.article_text(r["title"], corpus[r["article_id"]]["body"])
             for r in rows]
    czech = np.array([te.is_czech(t) for t in texts])
    docs = [rs.tokenize(t) for t in texts]
    corpus_tokens = [rs.tokenize(rs.article_text(o["title"], o["body"]))
                     for o in corpus.values()]
    tables = te.Tables(corpus_tokens, set(), [])

    en = te.read_terms(args.terms_dir / "en.txt")
    cs = te.read_terms(args.terms_dir / "cs_translations.txt")
    profiles = {"en": [rs.tokenize(u) for u in en],
                "en_cs": [rs.tokenize(u) for u in en + cs]}

    variants = [("no-trunc", None, None, 0.0, None),
                ("fixed N=4 w=0.3", None, 4, 0.3, None),
                ("fixed N=4 w=0.3 cap+3", None, 4, 0.3, 3)]
    for k in (1, 2, 3):
        for w in (0.3, 0.5, 1.0):
            variants.append((f"rel k={k} w={w}", k, None, w, None))

    needed = {prefix_of(q, k, fx) for _, k, fx, w, _ in variants if w
              for units in profiles.values() for u in units for q in u}
    needed.discard(None)
    pdf = prefix_df(corpus_tokens, needed)

    scores = {}
    fired_by = {}
    for pname, units in profiles.items():
        for name, k, fx, w, cap in variants:
            fired: list = []
            s = np.array([score(d, units, tables, pdf, k, fx, w, cap, fired)
                          for d in docs])
            scores[(pname, name)] = s
            fired_by[(pname, name)] = fired

    print(f"{'profile':6s} {'variant':24s} {'AUC':>6s} {'top100':>7s} "
          f"{'zero':>5s} {'cs':>6s} {'other':>6s}")
    res = {}
    for (pname, name), s in scores.items():
        r = {"auc": te.auc_fast(s, labels), "top100": te.top_hit(s, labels),
             "zero": float((s == 0).mean()),
             "cs": te.auc_fast(s[czech], labels[czech]),
             "other": te.auc_fast(s[~czech], labels[~czech])}
        res[f"{pname} {name}"] = r
        print(f"{pname:6s} {name:24s} {r['auc']:6.3f} {r['top100']:7.0%} "
              f"{r['zero']:5.0%} {r['cs']:6.3f} {r['other']:6.3f}")

    print("\nvs no-trunc, per slice (mean [95% CI] share>0):")
    deltas = {}
    for pname in profiles:
        base = scores[(pname, "no-trunc")]
        for name, *_ in variants[1:]:
            s = scores[(pname, name)]
            line = []
            for sl, m in (("all", np.ones_like(czech)), ("cs", czech),
                          ("other", ~czech)):
                d = te.paired_bootstrap(s[m], base[m], labels[m], args.bootstrap)
                deltas[f"{pname} {name} [{sl}]"] = d
                line.append(f"{sl} {d[0]:+.3f} [{d[1]:+.3f},{d[2]:+.3f}]")
            print(f"  {pname:6s} {name:24s} " + "  ".join(line))

    print("\nprefix pairs that fire, en_cs:")
    for name in ("fixed N=4 w=0.3", "rel k=2 w=0.5", "rel k=3 w=0.5"):
        counts: dict = {}
        for q, t in fired_by[("en_cs", name)]:
            counts[f"{q}~{t}"] = counts.get(f"{q}~{t}", 0) + 1
        top = sorted(counts.items(), key=lambda x: -x[1])[:30]
        print(f"  {name}: " + ", ".join(f"{p} {c}" for p, c in top))

    if args.out:
        args.out.write_text(json.dumps({"results": res, "deltas": deltas},
                                       indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
