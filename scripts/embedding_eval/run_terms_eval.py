# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Step 2b of the relevance plan: a term-list profile, phrases, truncation.

Measures the basic profile as it is meant to ship (one term per line) against
the AI profile the earlier numbers were taken with, and the matching features
the plan proposes on top of it. Everything is scored the way the app would see
it at fetch time:

- the text is the feed's own description (`corpus.body`), not readable text,
- document frequencies come from the whole instance (`--corpus`), not from one
  reader's articles, since the production table is built across all feeds.

Engagement comes from the user sample (segment P2, which was scored against the
August profile; see the plan for why that profile has to be passed in by hand).

The prototype scorer here reproduces `relevance_service.bm25_raw` exactly when
phrases and truncation are off. That is checked on every run before anything
else is reported, so the deltas below are deltas of the features, not of a port.

    uv run --script run_terms_eval.py --sample sample_clean.jsonl \\
        --corpus corpus.jsonl.gz --august-sample sample.jsonl.gz \\
        --terms-dir terms_v1 --out results.json
"""
import argparse
import gzip
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "backend"))

import run_eval  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402
from app.services.ai_eval_service import compute_auc  # noqa: E402

K1 = rs.BM25_K1
MIN_DF = 3


def sat(f: float) -> float:
    """BM25 term-frequency saturation with b = 0, as shipped."""
    return f * (K1 + 1.0) / (f + K1)


def idf_from(df: int | None, n_docs: int) -> float:
    if not df:
        return 0.0
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


# ── metrics ──────────────────────────────────────────────────────────────────

def auc_fast(scores: np.ndarray, labels: np.ndarray) -> float:
    """Mann-Whitney AUC with average ranks for ties (checked against compute_auc)."""
    order = np.argsort(scores, kind="mergesort")
    s = scores[order]
    ranks = np.empty(len(s))
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        ranks[i:j + 1] = (i + j) / 2.0 + 1.0
        i = j + 1
    r = np.empty(len(s))
    r[order] = ranks
    pos = labels.sum()
    neg = len(labels) - pos
    return float((r[labels].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def top_hit(scores: np.ndarray, labels: np.ndarray, n: int = 100) -> float:
    # Stable on ties: among equal scores the earlier row wins, the same for
    # every variant, so a tie block cannot flatter one of them.
    idx = np.argsort(-scores, kind="mergesort")[:n]
    return float(labels[idx].mean())


def paired_bootstrap(a: np.ndarray, b: np.ndarray, labels: np.ndarray,
                     n_iter: int = 1000, seed: int = 20260923) -> tuple:
    rng = np.random.default_rng(seed)
    deltas = []
    n = len(labels)
    for _ in range(n_iter):
        i = rng.integers(0, n, n)
        lab = labels[i]
        if lab.all() or not lab.any():
            continue
        deltas.append(auc_fast(a[i], lab) - auc_fast(b[i], lab))
    d = np.asarray(deltas)
    return (float(d.mean()), float(np.percentile(d, 2.5)),
            float(np.percentile(d, 97.5)), float((d > 0).mean()))


# ── language of a row, for the slices ───────────────────────────────────────

_CS_CHARS = re.compile(r"[ěščřžýůťďňáíé]")
_CS_WORDS = {"je", "se", "na", "že", "pro", "ve", "do", "jsou", "jak", "po",
             "ale", "by", "od", "za", "si", "to", "už", "jako", "který",
             "která", "které", "bude", "byl", "byla", "nebo", "při", "podle"}


def is_czech(text: str) -> bool:
    low = text.lower()
    if len(_CS_CHARS.findall(low)) >= 2:
        return True
    words = re.findall(r"\w+", low)
    return sum(w in _CS_WORDS for w in words) >= 2


# ── prototype scorer ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    name: str
    phrase: bool = False           # multi-word unit must match as adjacent tokens
    phrase_idf: str = "sum"        # sum | min | df
    backoff: float = 0.0           # phrase: bag score x this when no phrase hit
    trunc_n: int | None = None     # prefix length for graded truncation
    w: float = 0.0                 # weight of a prefix-only match


class Tables:
    """Corpus-wide document frequencies: tokens, token prefixes, phrases."""

    def __init__(self, corpus_tokens: list[list[str]], phrases: set[tuple],
                 trunc_ns: list[int]):
        self.n_docs = len(corpus_tokens)
        df: dict[str, int] = {}
        pdf: dict[int, dict[str, int]] = {n: {} for n in trunc_ns}
        phdf: dict[tuple, int] = {p: 0 for p in phrases}
        needles = {p: " " + " ".join(p) + " " for p in phrases}
        for toks in corpus_tokens:
            uniq = set(toks)
            for t in uniq:
                df[t] = df.get(t, 0) + 1
            for n, table in pdf.items():
                for p in {t[:n] for t in uniq if len(t) >= n}:
                    table[p] = table.get(p, 0) + 1
            if phrases:
                hay = " " + " ".join(toks) + " "
                for p, needle in needles.items():
                    if needle in hay:
                        phdf[p] += 1
        self.df = {t: c for t, c in df.items() if c >= MIN_DF}
        self.pdf = {n: {p: c for p, c in t.items() if c >= MIN_DF}
                    for n, t in pdf.items()}
        self.phdf = phdf

    def idf(self, t: str) -> float:
        return idf_from(self.df.get(t), self.n_docs)

    def pidf(self, p: str, n: int) -> float:
        return idf_from(self.pdf[n].get(p), self.n_docs)


def token_contrib(tok: str, freqs: dict, prefix_freqs: dict | None,
                  tables: Tables, cfg: Config) -> float:
    """One query token against one document: exact match, else graded prefix."""
    f = freqs.get(tok, 0)
    if f:
        idf = tables.idf(tok)
        if idf:
            return idf * sat(f)
    n = cfg.trunc_n
    if n and cfg.w and len(tok) >= n and prefix_freqs is not None:
        p = tok[:n]
        fp = prefix_freqs.get(p, 0) - f  # prefix hits that were not exact ones
        if fp > 0:
            return cfg.w * tables.pidf(p, n) * sat(fp)
    return 0.0


def phrase_hits(doc: list[str], unit: list[str], n: int | None) -> tuple[int, int]:
    """(exact occurrences, prefix-only occurrences) of `unit` as adjacent tokens."""
    m = len(unit)
    exact = pref = 0
    for i in range(len(doc) - m + 1):
        all_exact = True
        ok = True
        for j, u in enumerate(unit):
            d = doc[i + j]
            if d == u:
                continue
            if n and len(u) >= n and len(d) >= n and d[:n] == u[:n]:
                all_exact = False
                continue
            ok = False
            break
        if ok:
            if all_exact:
                exact += 1
            else:
                pref += 1
    return exact, pref


def score_doc(doc: list[str], units: list[list[str]], tables: Tables,
              cfg: Config) -> float:
    # Only tokens the table knows count towards frequencies, as in production.
    freqs: dict[str, int] = {}
    for t in doc:
        if t in tables.df:
            freqs[t] = freqs.get(t, 0) + 1
    prefix_freqs = None
    if cfg.trunc_n and cfg.w:
        n = cfg.trunc_n
        prefix_freqs = {}
        for t in doc:
            if len(t) >= n:
                p = t[:n]
                if p in tables.pdf[n]:
                    # Exact occurrences count here too and are subtracted in
                    # token_contrib. An exact form the table does not know is
                    # left in, so it scores through its prefix class.
                    prefix_freqs[p] = prefix_freqs.get(p, 0) + 1
    best = 0.0
    for unit in units:
        if not unit:
            continue
        uniq = list(dict.fromkeys(unit))
        if cfg.phrase and len(unit) >= 2:
            ex, pr = phrase_hits(doc, unit, cfg.trunc_n if cfg.w else None)
            if cfg.phrase_idf == "df":
                ph_idf = idf_from(tables.phdf.get(tuple(unit)), tables.n_docs)
            else:
                parts = [tables.idf(t) for t in uniq]
                ph_idf = sum(parts) if cfg.phrase_idf == "sum" else min(parts)
            if ex:
                s = ph_idf * sat(ex)
            elif pr:
                s = cfg.w * ph_idf * sat(pr)
            elif cfg.backoff:
                s = cfg.backoff * sum(token_contrib(t, freqs, prefix_freqs,
                                                    tables, cfg) for t in uniq)
            else:
                s = 0.0
        else:
            s = sum(token_contrib(t, freqs, prefix_freqs, tables, cfg)
                    for t in uniq)
        best = max(best, s)
    return best


# ── data ─────────────────────────────────────────────────────────────────────

def load_corpus(path: Path) -> dict[int, dict]:
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            o = json.loads(line)
            out[o["article_id"]] = o
    return out


def read_terms(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path,
                    help="export whose meta.profile.current is the profile P2 "
                         "was scored against")
    ap.add_argument("--terms-dir", required=True, type=Path)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    meta, rows = run_eval.load_sample(args.sample)
    with gzip.open(args.august_sample, "rt", encoding="utf-8") as f:
        meta["profile"]["previous"] = json.loads(f.readline())["profile"]["current"]
    seg = run_eval.assign_segments(meta, rows)["P2"]
    corpus = load_corpus(args.corpus)
    rows = [r for r in seg["rows"] if r["article_id"] in corpus]
    labels = np.array([bool(r["engaged"]) for r in rows])
    print(f"P2: {len(rows)} rows, {labels.sum()} engaged; corpus {len(corpus)}",
          file=sys.stderr)

    texts = [rs.article_text(r["title"], corpus[r["article_id"]]["body"])
             for r in rows]
    czech = np.array([is_czech(t) for t in texts])
    print(f"czech rows {czech.sum()} (engaged {labels[czech].sum()}), "
          f"other {(~czech).sum()} (engaged {labels[~czech].sum()})",
          file=sys.stderr)

    corpus_texts = [rs.article_text(o["title"], o["body"]) for o in corpus.values()]
    corpus_tokens = [rs.tokenize(t) for t in corpus_texts]
    doc_tokens = [rs.tokenize(t) for t in texts]

    en = read_terms(args.terms_dir / "en.txt")
    cs = read_terms(args.terms_dir / "cs_translations.txt")
    cold = read_terms(args.terms_dir / "cold_start.txt")
    ai_profile = rs.parse_profile(seg["profile"])
    profiles = {
        "ai_profile": ai_profile.positive,
        "ai_profile_cold3": ai_profile.positive[:3],
        "terms_en": en,
        "terms_cold3": cold,
        "terms_en_cs": en + cs,
    }
    tok_units = {k: [rs.tokenize(u) for u in v] for k, v in profiles.items()}
    phrases = {tuple(u) for units in tok_units.values() for u in units if len(u) >= 2}
    trunc_ns = [4, 5, 6]
    print("building corpus tables...", file=sys.stderr)
    tables = Tables(corpus_tokens, phrases, trunc_ns)
    print(f"tables: {len(tables.df)} tokens at min_df={MIN_DF}, "
          + ", ".join(f"N={n}: {len(tables.pdf[n])} prefixes" for n in trunc_ns),
          file=sys.stderr)

    # ── fidelity: prototype with features off == production bm25_raw ────────
    prod_stats = rs.CorpusStats(n_docs=tables.n_docs, avg_doc_len=1.0,
                                doc_freq=tables.df, ngram_max=1)
    base = Config("bag")
    worst = 0.0
    for key in ("ai_profile", "terms_en"):
        prod = np.array([rs.bm25_raw(t, profiles[key], prod_stats) for t in texts])
        proto = np.array([score_doc(d, tok_units[key], tables, base)
                          for d in doc_tokens])
        worst = max(worst, float(np.abs(prod - proto).max()))
    if worst > 1e-9:
        raise SystemExit(f"prototype diverges from production: max diff {worst}")
    print(f"fidelity: prototype == production (max diff {worst:.1e})",
          file=sys.stderr)
    chk = compute_auc(list(zip(prod.tolist(), labels.tolist())))
    assert abs(chk - auc_fast(prod, labels)) < 1e-9, "auc_fast disagrees"

    # the old measurement setup, for continuity: DF from the user sample only
    sample_stats = rs.build_corpus_stats(texts, min_df=MIN_DF, ngram_max=1)

    results: dict[str, dict] = {}
    scores: dict[str, np.ndarray] = {}

    def record(name: str, s: np.ndarray, note: str = "") -> None:
        scores[name] = s
        res = {"auc": auc_fast(s, labels), "top100": top_hit(s, labels),
               "zero_share": float((s == 0).mean()), "note": note}
        if labels[czech].any():
            res["auc_cs"] = auc_fast(s[czech], labels[czech])
        res["auc_other"] = auc_fast(s[~czech], labels[~czech])
        results[name] = res

    # E0: baseline
    for key in ("ai_profile", "ai_profile_cold3"):
        record(f"E0 {key} | DF sample",
               np.array([rs.bm25_raw(t, profiles[key], sample_stats) for t in texts]),
               "old setup: DF from the user sample")
        record(f"E0 {key} | DF corpus",
               np.array([score_doc(d, tok_units[key], tables, base)
                         for d in doc_tokens]))

    def run(key: str, cfg: Config) -> None:
        record(f"{cfg.name} {key}",
               np.array([score_doc(d, tok_units[key], tables, cfg)
                         for d in doc_tokens]))

    # E1: term list, multi-word terms as loose words
    for key in ("terms_en", "terms_cold3"):
        run(key, Config("E1 bag"))

    # E2: phrases
    for key in ("terms_en", "terms_cold3"):
        for pidf in ("sum", "min", "df"):
            run(key, Config(f"E2 phrase idf={pidf}", phrase=True, phrase_idf=pidf))
        run(key, Config("E2 phrase idf=sum backoff=0.3", phrase=True,
                        phrase_idf="sum", backoff=0.3))

    # E3: graded truncation, with and without phrases
    for key in ("terms_en", "terms_cold3", "terms_en_cs"):
        run(key, Config("E3 bag no-trunc"))
        run(key, Config("E3 phrase no-trunc", phrase=True))
        for n in trunc_ns:
            for w in (0.3, 0.5, 0.7, 1.0):
                run(key, Config(f"E3 bag N={n} w={w}", trunc_n=n, w=w))
                run(key, Config(f"E3 phrase N={n} w={w}", phrase=True,
                                trunc_n=n, w=w))

    # ── deltas worth a confidence interval ──────────────────────────────────
    pairs = [
        ("E0 ai_profile | DF corpus", "E0 ai_profile | DF sample"),
        ("E1 bag terms_en", "E0 ai_profile | DF corpus"),
        ("E1 bag terms_cold3", "E0 ai_profile_cold3 | DF corpus"),
    ]
    for key in ("terms_en", "terms_cold3"):
        for pidf in ("sum", "min", "df"):
            pairs.append((f"E2 phrase idf={pidf} {key}", f"E1 bag {key}"))
        pairs.append((f"E2 phrase idf=sum backoff=0.3 {key}", f"E1 bag {key}"))
    pairs.append(("E3 bag no-trunc terms_en_cs", "E3 bag no-trunc terms_en"))
    deltas = {}
    for a, b in pairs:
        deltas[f"{a}  -  {b}"] = paired_bootstrap(scores[a], scores[b], labels,
                                                  args.bootstrap)
    # E4: the truncation gate is per language. English must not get worse, so
    # each slice gets its own interval rather than riding on the pooled one.
    for key in ("terms_en", "terms_en_cs"):
        for n in trunc_ns:
            for w in (0.3, 0.5):
                a, b = f"E3 bag N={n} w={w} {key}", f"E3 bag no-trunc {key}"
                for sl, mask in (("all", np.ones_like(czech)), ("cs", czech),
                                 ("other", ~czech)):
                    deltas[f"E4 [{sl}] {a}  -  {b}"] = paired_bootstrap(
                        scores[a][mask], scores[b][mask], labels[mask],
                        args.bootstrap)

    # ── printout ─────────────────────────────────────────────────────────────
    print(f"\n{'variant':52s} {'AUC':>6s} {'top100':>7s} {'zero':>6s} "
          f"{'AUC cs':>7s} {'AUC oth':>7s}")
    for name, r in results.items():
        print(f"{name:52s} {r['auc']:6.3f} {r['top100']:7.0%} "
              f"{r['zero_share']:6.0%} {r.get('auc_cs', float('nan')):7.3f} "
              f"{r['auc_other']:7.3f}")
    print("\npaired bootstrap (mean, 95% CI, share > 0):")
    for k, (m, lo, hi, p) in deltas.items():
        print(f"  {m:+.3f} [{lo:+.3f}, {hi:+.3f}] {p:4.0%}  {k}")

    if args.out:
        args.out.write_text(json.dumps({
            "rows": len(rows), "engaged": int(labels.sum()),
            "czech_rows": int(czech.sum()),
            "czech_engaged": int(labels[czech].sum()),
            "results": results, "deltas": deltas,
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
