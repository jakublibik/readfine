# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Step 2b, E7 and E8: add/remove suggestions, and the squash calibration.

E7. The window is split in time. Suggestions are computed from the first half
(what the reader engaged with there), applied to the profile, and the profile
is scored on the second half. A suggestion that only fits the half it was
learned on shows up as no gain.

- add, log-odds: terms over-represented in engaged articles, log-odds ratio
  with an informative Dirichlet prior from instance-wide document frequency
  (Monroe, Colaresi & Quinn 2008), so rare terms do not win on one article.
- add, Rocchio: the top terms of the TF-IDF centroid of engaged articles.
- remove: profile terms that match often and lead to engagement at under half
  the base rate.
- silent expansion: the same added terms, but at half weight and without
  asking, as the plan's opt-in would do.

Accepted suggestions are simulated as "all accepted". A real reader drops the
nonsense ones, so this is the pessimistic side.

E8. The squash constant `k` refitted for the chosen scorer, and the share of
articles above each threshold, so the UI text can say what a threshold means.

Scorer throughout: loose words, relative prefix (drop 2, floor 4), w = 0.5.
"""
import argparse
import gzip
import json
import math
import random
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
import run_lexical_fidelity as fid  # noqa: E402
import run_terms_eval as te  # noqa: E402
import run_terms_trunc as tt  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

K, W = 2, 0.5

# Function words for the languages this reader reads, accents stripped the way
# the tokenizer strips them. English is scikit-learn's list (loaded in main, the
# eval has it); Czech is written out here. A product version needs one per
# language the instance carries.
CS_STOP = set("""
a aby ac ale ani ano asi az bez bude budou budu by byl byla byli bylo byly byt
ci co coz do ho i jak jake jaky jako je jeho jej jeji jejich jen jeste ji jiz
jsem jsi jsme jsou jste k kam kde kdo kdy kdyz ke ktera ktere kteri kterou
ktery kterym kterych ma maji mame mate me mezi mi mit mne mnou muj muze my na
nad nam nami nas nase nasi ne nebo neni nez nic nich nim o od ode on ona oni
ono pak po pod podle pokud pouze prave pred pres pri pro proc proto protoze
prvni s se si sve svych svym svou ta tak take takze tam te tedy ten tento teto
tim timto to tohle toho tohoto tom tomto tomu tu tuto ty tyto u uz v ve vice
vsak vse vsechny vsech z za zde ze rok roku let dnes jiz cela cely celou
""".split())


def unit_scores(doc, units, weights, tables, pdf) -> float:
    best = 0.0
    for u, wt in zip(units, weights):
        s = tt.score(doc, [u], tables, pdf, K, None, W, None)
        best = max(best, wt * s)
    return best


def covered(tok: str, profile_tokens: set[str]) -> bool:
    for q in profile_tokens:
        if tok == q:
            return True
        p = tt.prefix_of(q, K, None)
        if p and tok.startswith(p):
            return True
    return False


def log_odds(train_docs, train_lab, corpus_df, n_corpus, alpha0=500.0):
    eng = [set(d) for d, l in zip(train_docs, train_lab) if l]
    non = [set(d) for d, l in zip(train_docs, train_lab) if not l]
    yi: dict[str, int] = {}
    yj: dict[str, int] = {}
    for s in eng:
        for t in s:
            yi[t] = yi.get(t, 0) + 1
    for s in non:
        for t in s:
            yj[t] = yj.get(t, 0) + 1
    ni, nj = len(eng), len(non)
    out = {}
    for t, a in yi.items():
        df = corpus_df.get(t)
        if not df:
            continue
        aw = alpha0 * df / n_corpus
        b = yj.get(t, 0)
        d = (math.log((a + aw) / (ni + alpha0 - a - aw))
             - math.log((b + aw) / (nj + alpha0 - b - aw)))
        var = 1.0 / (a + aw) + 1.0 / (b + aw)
        out[t] = (d / math.sqrt(var), a)
    return out


def rocchio(train_docs, train_lab, tables):
    acc: dict[str, float] = {}
    n = 0
    support: dict[str, int] = {}
    for d, l in zip(train_docs, train_lab):
        if not l or not d:
            continue
        n += 1
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            idf = tables.idf(t)
            if idf > 0:
                acc[t] = acc.get(t, 0.0) + (c / len(d)) * idf
                support[t] = support.get(t, 0) + 1
    return {t: (v / n, support[t]) for t, v in acc.items()}


def pick_add(ranked: dict, profile_tokens, inflow_df: dict, n_inflow: int, m: int,
             min_support: int = 3, max_df_share: float = 0.01,
             stop: set | None = None) -> list[str]:
    """Top candidates that are specific enough to be a topic.

    Commonness is measured over the reader's own inflow, not the instance: half
    the instance is Cyrillic and CJK, which makes English function words look
    rare there. The first run without this suggested `may`, `could`, `they`.
    """
    out = []
    for t, (score, sup) in sorted(ranked.items(), key=lambda x: -x[1][0]):
        if sup < min_support or len(t) < 4 or t.isdigit():
            continue
        if stop and t in stop:
            continue
        df = inflow_df.get(t, 0)
        if not df or df / n_inflow > max_df_share:
            continue
        if covered(t, profile_tokens) or any(covered(t, {o}) or covered(o, {t})
                                             for o in out):
            continue
        out.append(t)
        if len(out) == m:
            break
    return out


def pick_remove(units, texts_units, train_docs, train_lab, tables, pdf,
                min_matches: int = 15, ratio: float = 0.5) -> list[tuple]:
    base = float(np.mean(train_lab))
    out = []
    for raw, u in zip(texts_units, units):
        hits = [l for d, l in zip(train_docs, train_lab)
                if tt.score(d, [u], tables, pdf, K, None, W, None) > 0]
        if len(hits) >= min_matches and np.mean(hits) < ratio * base:
            out.append((raw, len(hits), round(float(np.mean(hits)), 3)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path)
    ap.add_argument("--terms-dir", required=True, type=Path)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--filters", default="0.01:0",
                    help="comma list of max_df_share:stoplist(0/1) settings")
    ap.add_argument("--skip-calibration", action="store_true")
    args = ap.parse_args()
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
    stop_all = set(ENGLISH_STOP_WORDS) | CS_STOP
    filters = [(float(a), b == "1") for a, b in
               (f.split(":") for f in args.filters.split(","))]

    meta, rows = run_eval.load_sample(args.sample)
    with gzip.open(args.august_sample, "rt", encoding="utf-8") as f:
        meta["profile"]["previous"] = json.loads(f.readline())["profile"]["current"]
    corpus = te.load_corpus(args.corpus)
    rows = [r for r in run_eval.assign_segments(meta, rows)["P2"]["rows"]
            if r["article_id"] in corpus]
    rows.sort(key=lambda r: r["fetched_at"])
    labels = np.array([bool(r["engaged"]) for r in rows])
    docs = [rs.tokenize(rs.article_text(r["title"], corpus[r["article_id"]]["body"]))
            for r in rows]
    half = len(rows) // 2
    tr_d, te_d = docs[:half], docs[half:]
    tr_l, te_l = labels[:half], labels[half:]
    print(f"train {half} ({tr_l.sum()} engaged, until {rows[half]['fetched_at'][:10]}), "
          f"test {len(rows) - half} ({te_l.sum()} engaged)", file=sys.stderr)

    corpus_tokens = [rs.tokenize(rs.article_text(o["title"], o["body"]))
                     for o in corpus.values()]
    tables = te.Tables(corpus_tokens, set(), [])
    n_corpus = tables.n_docs

    en = te.read_terms(args.terms_dir / "en.txt")
    cs = te.read_terms(args.terms_dir / "cs_translations.txt")
    cold = te.read_terms(args.terms_dir / "cold_start.txt")
    base_profiles = {"en_cs": en + cs, "cold3": cold}

    feeds = {r["feed_id"] for r in rows}
    inflow = [rs.tokenize(rs.article_text(o["title"], o["body"]))
              for o in corpus.values() if o["feed_id"] in feeds]
    inflow_df: dict[str, int] = {}
    for d in inflow:
        for t in set(d):
            inflow_df[t] = inflow_df.get(t, 0) + 1
    n_inflow = len(inflow)
    print(f"reader inflow: {n_inflow} articles from {len(feeds)} feeds",
          file=sys.stderr)
    lo = log_odds(tr_d, tr_l, tables.df, n_corpus)
    ro = rocchio(tr_d, tr_l, tables)

    plans = {}
    for pname, terms in base_profiles.items():
        ptoks = {t for u in terms for t in rs.tokenize(u)}
        adds = {}
        for share, use_stop in filters:
            tag = f"df{share:g}{'+stop' if use_stop else ''}"
            st = stop_all if use_stop else None
            adds[f"logodds {tag}"] = pick_add(lo, ptoks, inflow_df, n_inflow, 20,
                                              max_df_share=share, stop=st)
            adds[f"rocchio {tag}"] = pick_add(ro, ptoks, inflow_df, n_inflow, 20,
                                              max_df_share=share, stop=st)
        plans[pname] = {"terms": terms, "adds": adds}

    needed = set()
    for p in plans.values():
        for u in p["terms"] + [t for a in p["adds"].values() for t in a]:
            for q in rs.tokenize(u):
                needed.add(tt.prefix_of(q, K, None))
    needed.discard(None)
    tt.MIN_PREFIX = 4
    pdf = tt.prefix_df(corpus_tokens, needed)

    results, scores = {}, {}

    def evaluate(name, units_raw, weights):
        units = [rs.tokenize(u) for u in units_raw]
        s = np.array([unit_scores(d, units, weights, tables, pdf) for d in te_d])
        scores[name] = s
        results[name] = {"auc": te.auc_fast(s, te_l), "top50": te.top_hit(s, te_l, 50),
                         "zero": float((s == 0).mean()), "units": len(units_raw)}

    for pname, p in plans.items():
        terms = p["terms"]
        removes = pick_remove([rs.tokenize(u) for u in terms], terms, tr_d, tr_l,
                              tables, pdf)
        p["removes"] = removes
        rm = {r[0] for r in removes}
        evaluate(f"{pname} base", terms, [1.0] * len(terms))
        kept = [t for t in terms if t not in rm]
        if rm:
            evaluate(f"{pname} remove", kept, [1.0] * len(kept))
        for meth, add in p["adds"].items():
            for m in (5, 10, 20):
                a = add[:m]
                evaluate(f"{pname} +{meth} {m}", terms + a, [1.0] * (len(terms) + len(a)))
            if rm:
                a = add[:10]
                evaluate(f"{pname} +{meth} 10 & remove", kept + a,
                         [1.0] * (len(kept) + len(a)))

    print(f"\n{'variant':34s} {'AUC':>6s} {'top50':>6s} {'zero':>5s}")
    for n, r in results.items():
        print(f"{n:34s} {r['auc']:6.3f} {r['top50']:6.0%} {r['zero']:5.0%}")
    print("\nvs base, test half (mean [95% CI] share>0):")
    deltas = {}
    for n in results:
        pname = n.split()[0]
        if n == f"{pname} base":
            continue
        d = te.paired_bootstrap(scores[n], scores[f"{pname} base"], te_l,
                                args.bootstrap)
        deltas[n] = d
        print(f"  {d[0]:+.3f} [{d[1]:+.3f}, {d[2]:+.3f}] {d[3]:4.0%}  {n}")
    for pname, p in plans.items():
        print(f"\n{pname} suggestions:")
        for meth, add in p["adds"].items():
            print(f"  add {meth}: {', '.join(add)}")
        print(f"  remove: {p['removes']}")

    if args.skip_calibration:
        if args.out:
            args.out.write_text(json.dumps({"results": results, "deltas": deltas,
                                            "plans": plans}, indent=2,
                                           ensure_ascii=False))
        return

    # ── E8: calibration on the whole P2 window, chosen scorer, en_cs ────────
    units = [rs.tokenize(u) for u in en + cs]
    raw = [unit_scores(d, units, [1.0] * len(units), tables, pdf) for d in docs]
    llm = [r["ai_score"] for r in rows]
    cal = fid.calibrate_k(raw, llm)
    cal["threshold_shares"] = fid.threshold_shares(raw, cal["k"])
    cal["threshold_shares_llm"] = {str(t): round(float((np.asarray(llm) >= t).mean()), 4)
                                   for t in (0.3, 0.5, 0.6, 0.7)}
    pool = [o for o in corpus.values() if o["feed_id"] in feeds]
    random.Random(20260923).shuffle(pool)
    pool = pool[:5000]
    raw_pool = [unit_scores(rs.tokenize(rs.article_text(o["title"], o["body"])),
                            units, [1.0] * len(units), tables, pdf) for o in pool]
    cal["threshold_shares_all_inflow"] = fid.threshold_shares(raw_pool, cal["k"])
    cal["threshold_shares_all_inflow_old_k"] = fid.threshold_shares(raw_pool, rs.SQUASH_K)
    print("\nE8 calibration:", json.dumps(cal, indent=1))

    if args.out:
        args.out.write_text(json.dumps({"results": results, "deltas": deltas,
                                        "plans": plans, "calibration": cal},
                                       indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
