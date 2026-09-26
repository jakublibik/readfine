# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "nh3>=0.2",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Step 2b, E5: Cyrillic and CJK, which one reader's sample does not contain.

There is no engagement for these languages, so nothing here is an AUC. The
questions are mechanical ones, answered over the whole-instance corpus:

1. Does a term in the language match at all? With today's tokenizer a CJK
   headline is one token, so a Chinese term never matches anything.
2. Does it match the right articles? Top articles per language are dumped for
   reading, next to a random draw from the same language as a control.
3. What does script-aware tokenization cost: table growth, and does it leave
   the English/Czech numbers of E3 untouched (it must, as it only changes CJK)?
4. Where does the prefix match of the chosen truncation (N=4, w=0.3) fire in
   the user sample, so the conflations can be read rather than assumed.

Script-aware tokenization: text is split into runs by Unicode script. Han, kana
and Hangul runs become overlapping character bigrams (the Lucene/Elasticsearch
`cjk` approach); everything else is `\\w\\w+` words as today. Accents are
stripped outside CJK runs only: NFKD on Hangul decomposes a syllable into jamo,
and on kana it drops the voicing mark (が -> か).

    uv run --script run_terms_multilang.py --corpus corpus.jsonl.gz \\
        --sample sample_clean.jsonl --august-sample sample.jsonl.gz \\
        --terms-dir terms_v1 --out-dir e5/
"""
import argparse
import csv
import gzip
import json
import random
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
import run_terms_eval as te  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

CJK = ("぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿"
       "가-힯ᄀ-ᇿ㄰-㆏")
_SEG_RE = re.compile(rf"([{CJK}]+)")
_CJK_RUN_RE = re.compile(rf"^[{CJK}]+$")
_WORD_RE = re.compile(r"\b\w\w+\b", re.UNICODE)

TERMS = {
    "ru": ["искусственный интеллект", "безопасность ИИ", "нейросеть", "Украина",
           "биткоин", "криптовалюта", "фондовый рынок", "космос", "деменция",
           "сон"],
    "zh": ["人工智能", "AI安全", "大模型", "乌克兰", "比特币", "加密货币", "股市",
           "航天", "痴呆", "睡眠"],
    "ja": ["人工知能", "生成AI", "ウクライナ", "ビットコイン", "暗号資産", "株式市場",
           "宇宙", "認知症", "睡眠"],
    "ko": ["인공지능", "우크라이나", "비트코인", "암호화폐", "주식시장", "우주",
           "치매", "수면"],
}


def tokenize_script(text: str, unigrams: bool = False) -> list[str]:
    text = unicodedata.normalize("NFKC", text.lower())
    out: list[str] = []
    for seg in _SEG_RE.split(text):
        if not seg:
            continue
        if _CJK_RUN_RE.match(seg):
            if len(seg) == 1:
                out.append(seg)
                continue
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
            if unigrams:
                out.extend(seg)
        else:
            out.extend(_WORD_RE.findall(rs.strip_accents(seg)))
    return out


_CYR = re.compile(r"[Ѐ-ӿ]")
_HAN = re.compile(r"[一-鿿]")
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힯]")


def lang_of(text: str) -> str:
    if _HANGUL.search(text):
        return "ko"
    if _KANA.search(text):
        return "ja"
    if _HAN.search(text):
        return "zh"
    if _CYR.search(text):
        return "ru"
    return "latin"


def is_cjk_unit(unit: list[str]) -> bool:
    return bool(unit) and all(_CJK_RUN_RE.match(t) for t in unit)


def score_mixed(doc: list[str], units: list[list[str]], tables, cfg) -> float:
    """A CJK term is one word cut into bigrams, so its bigrams must be adjacent.

    A Latin multi-word term is a list of concepts and scores as loose words
    (E2 measured strict phrases there at -0.035 AUC); a CJK term has no such
    reading, and scoring its bigrams loosely lets a common katakana pair like
    `ット` stand in for the whole word.
    """
    phrase_cfg = te.Config("cjk-phrase", phrase=True)
    best = 0.0
    for u in units:
        c = phrase_cfg if (len(u) >= 2 and is_cjk_unit(u)) else cfg
        best = max(best, te.score_doc(doc, [u], tables, c))
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path)
    ap.add_argument("--terms-dir", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    corpus = te.load_corpus(args.corpus)
    items = list(corpus.values())
    texts = [rs.article_text(o["title"], o["body"]) for o in items]
    langs = [lang_of(o["title"] or "") for o in items]
    print("languages by title:",
          {l: langs.count(l) for l in sorted(set(langs))}, file=sys.stderr)

    report: dict = {}

    # ── 3a. table growth ────────────────────────────────────────────────────
    old_tok = [rs.tokenize(t) for t in texts]
    new_tok = [tokenize_script(t) for t in texts]
    uni_tok = [tokenize_script(t, unigrams=True) for t in texts]
    phr: set = set()
    t_old = te.Tables(old_tok, phr, [4])
    t_new = te.Tables(new_tok, phr, [4])
    t_uni = te.Tables(uni_tok, phr, [4])
    report["table"] = {"today": len(t_old.df), "script_bigrams": len(t_new.df),
                       "script_bigrams_unigrams": len(t_uni.df)}
    print("table sizes:", report["table"], file=sys.stderr)

    # ── 1 + 2. matching per language ────────────────────────────────────────
    cfg = te.Config("n4w03", trunc_n=4, w=0.3)
    rng = random.Random(20260923)
    review_rows = []
    report["match"] = {}
    for lang, terms in TERMS.items():
        idx = [i for i, l in enumerate(langs) if l == lang]
        for label, tok, tables in (("today", rs.tokenize, t_old),
                                   ("script", tokenize_script, t_new),
                                   ("script+uni",
                                    lambda s: tokenize_script(s, True), t_uni),
                                   ("script+cjkphrase", tokenize_script, t_new)):
            units = [tok(u) for u in terms]
            docs = old_tok if label == "today" else (
                uni_tok if label == "script+uni" else new_tok)
            if label == "script+cjkphrase":
                sc = np.array([score_mixed(docs[i], units, tables, cfg)
                               for i in idx])
            else:
                sc = np.array([te.score_doc(docs[i], units, tables, cfg)
                               for i in idx])
            report["match"].setdefault(lang, {})[label] = {
                "articles": len(idx), "matched": int((sc > 0).sum()),
                "matched_share": round(float((sc > 0).mean()), 4)}
            if label == "script+cjkphrase":
                order = np.argsort(-sc, kind="mergesort")[:args.top]
                for rank, j in enumerate(order):
                    i = idx[j]
                    best = max(terms, key=lambda u: score_mixed(
                        docs[i], [tok(u)], tables, cfg))
                    review_rows.append({"lang": lang, "pick": "top",
                                        "rank": rank + 1,
                                        "score": round(float(sc[j]), 3),
                                        "term": best,
                                        "feed": items[i]["feed_title"],
                                        "title": items[i]["title"]})
                for i in rng.sample(idx, min(args.top, len(idx))):
                    review_rows.append({"lang": lang, "pick": "random",
                                        "rank": "", "score": "", "term": "",
                                        "feed": items[i]["feed_title"],
                                        "title": items[i]["title"]})
    for lang, m in report["match"].items():
        print(lang, m, file=sys.stderr)
    with open(args.out_dir / "e5_review.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(review_rows[0]))
        w.writeheader()
        w.writerows(review_rows)

    # ── 3b. English/Czech numbers unchanged under the new tokenizer ─────────
    meta, rows = run_eval.load_sample(args.sample)
    with gzip.open(args.august_sample, "rt", encoding="utf-8") as f:
        meta["profile"]["previous"] = json.loads(f.readline())["profile"]["current"]
    rows = [r for r in run_eval.assign_segments(meta, rows)["P2"]["rows"]
            if r["article_id"] in corpus]
    labels = np.array([bool(r["engaged"]) for r in rows])
    stexts = [rs.article_text(r["title"], corpus[r["article_id"]]["body"])
              for r in rows]
    en = te.read_terms(args.terms_dir / "en.txt")
    cs = te.read_terms(args.terms_dir / "cs_translations.txt")
    t4_old = te.Tables(old_tok, phr, [4])
    t4_new = t_new
    a_old = np.array([te.score_doc(rs.tokenize(t), [rs.tokenize(u) for u in en + cs],
                                   t4_old, cfg) for t in stexts])
    a_new = np.array([te.score_doc(tokenize_script(t),
                                   [tokenize_script(u) for u in en + cs],
                                   t4_new, cfg) for t in stexts])
    report["sample_auc"] = {"today_tokenizer": te.auc_fast(a_old, labels),
                            "script_tokenizer": te.auc_fast(a_new, labels),
                            "max_abs_diff": float(np.abs(a_old - a_new).max())}
    print("sample AUC:", report["sample_auc"], file=sys.stderr)

    # ── 4. where the prefix match fires in the user sample ──────────────────
    stoks = [tokenize_script(t) for t in stexts]
    exact_cfg = te.Config("exact")
    hits = []
    for r, toks, s_tr in zip(rows, stoks, a_new):
        s_ex = te.score_doc(toks, [tokenize_script(u) for u in en + cs],
                            t4_new, exact_cfg)
        if s_tr > s_ex:
            fired = sorted({(u, t) for u in en + cs for q in tokenize_script(u)
                            for t in toks
                            if len(q) >= 4 and len(t) >= 4 and t != q
                            and t[:4] == q[:4]})
            hits.append({"gain": round(float(s_tr - s_ex), 3),
                         "engaged": bool(r["engaged"]),
                         "matches": "; ".join(f"{u} ~ {t}" for u, t in fired),
                         "title": r["title"]})
    hits.sort(key=lambda h: -h["gain"])
    report["prefix"] = {"articles_gaining": len(hits),
                        "engaged_among_them": sum(h["engaged"] for h in hits)}
    pair_counts: dict[str, int] = {}
    for h in hits:
        for m in h["matches"].split("; "):
            if m:
                pair_counts[m] = pair_counts.get(m, 0) + 1
    report["prefix"]["top_pairs"] = sorted(pair_counts.items(),
                                           key=lambda x: -x[1])[:60]
    with open(args.out_dir / "e5_prefix_hits.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(hits[0]))
        w.writeheader()
        w.writerows(hits)

    (args.out_dir / "e5_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["prefix"], ensure_ascii=False, indent=1),
          file=sys.stderr)


if __name__ == "__main__":
    main()
