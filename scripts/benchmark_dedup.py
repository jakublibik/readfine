#!/usr/bin/env python
"""Score the story matcher against a corpus with a human-written ground truth.

``survey_dedup.py`` is the other half of this pair and answers a different question.
It runs over our own articles and reports how much a threshold would catch, which is
what the shipped values were chosen from -- but nobody ever said, article by article,
which of those pairs really are the same story. It measures volume, not accuracy.
This script removes the guessing: it runs the matcher over headline pairs a person has
labelled as covering the same event or not, and scores the answers against them.

The corpus is HLGD (Headline Grouping Dataset, Laban et al., NAACL 2021): 20 056
headline pairs drawn from 10 large news events, each labelled by five annotators for
whether the two headlines describe the same underlying event. Downloaded on first run
to ``.benchmark/hlgd/`` in the repository root, which is gitignored.

HLGD is the rare benchmark that matches what we actually do. Its first two challenges
are headlines only, and headlines plus publication date -- which is our matcher exactly,
trigrams over the title inside a 72 h window. The published reference points are worth
keeping in view while reading any number this prints:

    humans (annotator agreement)       ~0.90 F1
    best models in the paper            0.75 F1
    the released Electra + time model   0.74 F1

So a lexical matcher is not being held to an unreachable standard here. Even a
fine-tuned transformer leaves a quarter of the pairs wrong.

**What it does and does not cover.** Both headlines in an HLGD pair come from the same
news event, so a negative is "two different moments of the Equifax breach", not "an
article about something else entirely". That is the hard half of our problem and only
the hard half: the easy negatives that make up almost all of our real traffic are not
represented. Precision here is therefore a floor, and the absolute numbers do not
transfer to our corpus. What does transfer is the comparison between approaches on the
same pairs, and the shape of the curve around our thresholds. Use both scripts: this one
to compare methods and see where a threshold starts breaking, the survey to see how much
of our own corpus a threshold touches.

Usage, from the repository root::

    uv run --project backend python scripts/benchmark_dedup.py
    uv run --project backend python scripts/benchmark_dedup.py --split dev --window 3
    uv run --with sentence-transformers --project backend \\
        python scripts/benchmark_dedup.py --embeddings intfloat/multilingual-e5-small
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BENCH_DIR = REPO_ROOT / ".benchmark" / "hlgd"
ARCHIVE_URL = (
    "https://github.com/tingofurro/headline_grouping/releases/download/0.1/"
    "hlgd_classification_0.1.zip"
)

from app.services.ai_eval_service import compute_auc  # noqa: E402

# The similarity function is imported, never reimplemented: it is the one the shipped
# thresholds were measured with, and a second copy would drift from it silently.
from survey_dedup import (  # noqa: E402
    SEPARATORS,
    STOPWORDS,
    similarity,
    tokens,
    trigrams,
)

# What the app ships today (app/fetcher/stories.py). Printed as a marked row in every
# table so the sweep is read against the real operating point rather than in the
# abstract.
COLLAPSE_THRESHOLD = 0.30
SUPPRESS_THRESHOLD = 0.40
WINDOW_HOURS = 72
MIN_TITLE_CHARS = 12

SWEEP = [round(0.05 * i, 2) for i in range(1, 19)]


def ensure_corpus() -> Path:
    """Download and unpack the pair corpus once. ~1.9 MB."""
    if (BENCH_DIR / "test.json").exists():
        return BENCH_DIR
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"downloading HLGD to {BENCH_DIR} ...", file=sys.stderr)
    with urllib.request.urlopen(ARCHIVE_URL, timeout=300) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(BENCH_DIR)
    return BENCH_DIR


# ── the pairs ────────────────────────────────────────────────────────────────


class Pair:
    __slots__ = ("a", "b", "date_a", "date_b", "host_a", "host_b", "label",
                 "tri_a", "tri_b", "tok_a", "tok_b")

    def __init__(self, row: dict, strip_suffixes: bool):
        self.a = _clean(row["headline_a"]) if strip_suffixes else row["headline_a"]
        self.b = _clean(row["headline_b"]) if strip_suffixes else row["headline_b"]
        self.date_a = _parse_date(row.get("date_a"))
        self.date_b = _parse_date(row.get("date_b"))
        self.host_a = _host(row.get("url_a"))
        self.host_b = _host(row.get("url_b"))
        self.label = bool(row["label"])
        self.tri_a, self.tri_b = trigrams(self.a), trigrams(self.b)
        self.tok_a, self.tok_b = _sig(self.a), _sig(self.b)

    @property
    def days_apart(self) -> int | None:
        if self.date_a is None or self.date_b is None:
            return None
        return abs((self.date_a - self.date_b).days)

    @property
    def cross_source(self) -> bool:
        return bool(self.host_a) and bool(self.host_b) and self.host_a != self.host_b

    @property
    def long_enough(self) -> bool:
        return len(self.a) >= MIN_TITLE_CHARS and len(self.b) >= MIN_TITLE_CHARS


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _host(url: str | None) -> str:
    if not url:
        return ""
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _sig(title: str) -> set[str]:
    return {t for t in tokens(title) if t not in STOPWORDS and len(t) > 2}


def _clean(title: str) -> str:
    """Drop a trailing " - The New York Times" the way survey_dedup does per feed.

    Not what the app does. ``title_norm`` is generated from the raw title, so the shipped
    path compares the furniture along with the headline, while the survey that chose the
    thresholds compared titles with it stripped. ``--strip-suffixes`` exists to put a
    number on that gap; here the suffix has to be guessed per headline rather than
    learned per feed, since one pair is all there is to look at.
    """
    for sep in SEPARATORS:
        idx = title.rfind(sep)
        if idx > 20 and 3 < len(title) - idx < 40:
            return title[:idx].strip()
    return title


def load_pairs(split: str, strip_suffixes: bool) -> list[Pair]:
    rows = json.loads((ensure_corpus() / f"{split}.json").read_text(encoding="utf8"))
    return [Pair(row, strip_suffixes) for row in rows]


# ── the scorers ──────────────────────────────────────────────────────────────


def score_trigram(pair: Pair) -> float:
    """What the app runs: pg_trgm similarity over the normalised title."""
    return similarity(pair.tri_a, pair.tri_b)


def score_token_jaccard(pair: Pair) -> float:
    """Jaccard over significant words. The cheap baseline trigrams have to beat."""
    if not pair.tok_a or not pair.tok_b:
        return 0.0
    inter = len(pair.tok_a & pair.tok_b)
    return inter / (len(pair.tok_a | pair.tok_b))


def make_idf_cosine(pairs: list[Pair]):
    """Cosine over IDF-weighted words, with IDF built from the split's headlines.

    The idea survey_dedup already carries: two headlines about one event nearly always
    share a rare word (a surname, a place, a number), while two headlines that merely
    share a subject share only common ones. Trigram overlap cannot tell those apart,
    because it weights every character the same, and on this corpus every negative pair
    shares the subject. If anything lexical is going to separate them, it is this.
    """
    import math

    seen: dict[str, int] = {}
    docs = 0
    for pair in pairs:
        for bag in (pair.tok_a, pair.tok_b):
            docs += 1
            for token in bag:
                seen[token] = seen.get(token, 0) + 1
    idf = {t: math.log(docs / n) for t, n in seen.items()}

    def score(pair: Pair) -> float:
        shared = pair.tok_a & pair.tok_b
        if not shared:
            return 0.0
        num = sum(idf[t] ** 2 for t in shared)
        na = math.sqrt(sum(idf[t] ** 2 for t in pair.tok_a))
        nb = math.sqrt(sum(idf[t] ** 2 for t in pair.tok_b))
        return num / (na * nb) if na and nb else 0.0

    return score


def make_embedding_cosine(pairs: list[Pair], model_name: str, prefix: str = ""):
    """Cosine between sentence embeddings. Optional, needs sentence-transformers.

    Here to answer one question and no other: how much of the gap between a lexical
    score and the published transformer number is bought by meaning alone, without
    fine-tuning anything on this task.

    ``prefix`` is for the models that were trained with one. E5 in particular expects
    "query: " in front of every short text, and leaving it off costs a little accuracy,
    so a number measured without it is a floor for that model rather than its score.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        sys.exit(
            "--embeddings needs sentence-transformers. Re-run with:\n"
            "  uv run --with sentence-transformers --project backend "
            "python scripts/benchmark_dedup.py --embeddings " + model_name
        )
    model = SentenceTransformer(model_name)
    texts = sorted({p.a for p in pairs} | {p.b for p in pairs})
    print(f"  embedding {len(texts)} headlines with {model_name} ...", file=sys.stderr)
    vectors = model.encode([prefix + t for t in texts], normalize_embeddings=True,
                           batch_size=64, show_progress_bar=False)
    index = {text: vector for text, vector in zip(texts, vectors)}

    def score(pair: Pair) -> float:
        return float(index[pair.a] @ index[pair.b])

    return score


def windowed(score, window_days: int):
    """Our 72 h rule: outside the window the pair is not a pair at all."""
    def scored(pair: Pair) -> float:
        days = pair.days_apart
        if days is not None and days > window_days:
            return 0.0
        if not pair.long_enough:
            return 0.0
        return score(pair)
    return scored


# ── scoring ──────────────────────────────────────────────────────────────────


def metrics(scored: list[tuple[float, bool]], threshold: float) -> dict:
    tp = fp = fn = 0
    for value, label in scored:
        hit = value >= threshold
        if hit and label:
            tp += 1
        elif hit:
            fp += 1
        elif label:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"t": threshold, "precision": precision, "recall": recall, "f1": f1,
            "tp": tp, "fp": fp, "fn": fn}


def candidates(scored: list[tuple[float, bool]], steps: int = 200) -> list[float]:
    """Thresholds to try when looking for the best F1, taken from the scores themselves.

    A fixed 0.05 grid is fine for trigram similarity, which spreads across the range, and
    badly unfair to an embedding cosine, which piles up above 0.8: its whole decision
    happens between two grid points and the best F1 lands on the edge of the sweep.
    Sampling the actual distribution asks every scorer the same question.
    """
    values = sorted({value for value, _ in scored})
    if len(values) <= steps:
        return values
    stride = len(values) / steps
    return [values[min(int(i * stride), len(values) - 1)] for i in range(steps)]


def report(name: str, scored: list[tuple[float, bool]], marks: dict[float, str]) -> None:
    auc = compute_auc(scored)
    best = max((metrics(scored, t) for t in candidates(scored)), key=lambda m: m["f1"])
    print(f"\n  {name}")
    print(f"    AUC {auc:.3f}   best F1 {best['f1']:.3f} at threshold {best['t']:.2f} "
          f"(P {best['precision']:.3f} R {best['recall']:.3f})")
    for threshold, label in sorted(marks.items()):
        m = metrics(scored, threshold)
        print(f"    {label:<22} t={threshold:.2f}  F1 {m['f1']:.3f}  "
              f"P {m['precision']:.3f}  R {m['recall']:.3f}  "
              f"(tp {m['tp']}, fp {m['fp']}, fn {m['fn']})")


def sweep_table(name: str, scored: list[tuple[float, bool]]) -> None:
    print(f"\n  {name}: full sweep")
    print(f"    {'t':>5}  {'F1':>6}  {'prec':>6}  {'rec':>6}  {'tp':>6} {'fp':>6} {'fn':>6}")
    for threshold in SWEEP:
        m = metrics(scored, threshold)
        print(f"    {threshold:5.2f}  {m['f1']:6.3f}  {m['precision']:6.3f}  "
              f"{m['recall']:6.3f}  {m['tp']:6d} {m['fp']:6d} {m['fn']:6d}")


def at_recall(scored: list[tuple[float, bool]], target: float) -> dict:
    """The operating point that matches a given recall, for comparing at equal catch.

    Comparing two scores at their own best F1 says nothing useful when the question is
    "would this hide fewer wrong articles": a score can buy F1 with recall we do not
    want. Held at the same recall, the only thing left to compare is precision.
    """
    best = None
    for threshold in candidates(scored):
        m = metrics(scored, threshold)
        if m["recall"] < target:
            continue
        if best is None or m["recall"] < best["recall"]:
            best = m
    return best or metrics(scored, 0.0)


def stacked(name: str, gate: list[tuple[float, bool]], second: list[tuple[float, bool]],
            gate_threshold: float, hide_threshold: float) -> None:
    """What a second opinion on top of the trigram candidates would be worth.

    This is the only shape either alternative could realistically ship in. Candidate
    generation has to stay on the GIN index, because that is what makes the lookup one
    indexed probe instead of a scan; but the hide decision runs on a handful of pairs a
    day, which is few enough to score again with something more expensive. So the
    question is not "is IDF cosine better than trigrams", it is "does a second score,
    applied only to pairs the trigram already accepted, hide fewer wrong articles at the
    same catch".
    """
    baseline = metrics(gate, hide_threshold)
    gated = [
        (value if gate_value >= gate_threshold else 0.0, label)
        for (gate_value, _), (value, label) in zip(gate, second)
    ]
    matched = at_recall(gated, baseline["recall"])
    print(f"\n  {name}, applied only to pairs the trigram put at >= {gate_threshold:.2f}")
    print(f"    trigram alone at {hide_threshold:.2f}:  "
          f"P {baseline['precision']:.3f}  R {baseline['recall']:.3f}  "
          f"(fp {baseline['fp']})")
    print(f"    second score at the same recall: "
          f"P {matched['precision']:.3f}  R {matched['recall']:.3f}  "
          f"(fp {matched['fp']}, t={matched['t']:.3f})")
    ceiling = max((metrics(gated, t) for t in candidates(gated)), key=lambda m: m["f1"])
    print(f"    its own best F1:                 F1 {ceiling['f1']:.3f} at {ceiling['t']:.3f}  "
          f"(P {ceiling['precision']:.3f} R {ceiling['recall']:.3f})")


def examples(pairs: list[Pair], score, threshold: float, limit: int) -> None:
    """The pairs the matcher gets wrong, worst first.

    A table of rates says how often it is wrong; this says what being wrong looks like,
    which is the only way to judge whether a false positive here would have cost a reader
    anything. False positives are listed by descending score because those are the ones
    that would be hidden most confidently.
    """
    scored = [(score(p), p) for p in pairs]
    by_score = lambda item: item[0]  # noqa: E731 - Pair itself is not orderable
    fps = sorted(((s, p) for s, p in scored if s >= threshold and not p.label), key=by_score)
    fns = sorted(((s, p) for s, p in scored if s < threshold and p.label), key=by_score)
    print(f"\n  hidden but not the same story (false positives at {threshold:.2f}), "
          f"most confident first")
    for value, pair in reversed(fps[-limit:]):
        print(f"    {value:.3f}  {pair.a}\n           {pair.b}")
    print(f"\n  the same story but missed (false negatives at {threshold:.2f}), "
          f"closest first")
    for value, pair in reversed(fns[-limit:]):
        print(f"    {value:.3f}  {pair.a}\n           {pair.b}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", default="test", choices=("train", "dev", "test"))
    parser.add_argument("--window", type=int, default=WINDOW_HOURS // 24,
                        help="window in days (default 3, the shipped 72 h)")
    parser.add_argument("--strip-suffixes", action="store_true",
                        help='drop a trailing " - Outlet" before comparing')
    parser.add_argument("--cross-source-only", action="store_true",
                        help="keep only pairs from different sites, as the app does")
    parser.add_argument("--embeddings", metavar="MODEL",
                        help="also score a sentence-embedding cosine")
    parser.add_argument("--embedding-prefix", default="query: ",
                        help='prepended to every headline before encoding; E5 models '
                             'want "query: ", others want nothing (pass "")')
    parser.add_argument("--sweep", action="store_true",
                        help="print the full threshold table for the shipped matcher")
    parser.add_argument("--examples", type=float, metavar="T", nargs="?",
                        const=SUPPRESS_THRESHOLD,
                        help="print the pairs the shipped matcher gets wrong at T "
                             f"(default {SUPPRESS_THRESHOLD})")
    parser.add_argument("--examples-limit", type=int, default=8)
    args = parser.parse_args()

    pairs = load_pairs(args.split, args.strip_suffixes)
    if args.cross_source_only:
        pairs = [p for p in pairs if p.cross_source]

    positives = sum(1 for p in pairs if p.label)
    same_site = sum(1 for p in pairs if not p.cross_source)
    dated = [p for p in pairs if p.days_apart is not None]
    lost = [p for p in dated if p.label and p.days_apart > args.window]
    short = [p for p in pairs if not p.long_enough]

    print(f"\nHLGD {args.split}: {len(pairs)} pairs, {positives} same-story "
          f"({positives / len(pairs):.0%}), {len(pairs) - positives} not")
    print(f"  {same_site} pairs come from one site "
          f"({same_site / len(pairs):.0%}); the app would never compare those")
    print(f"  {len(lost)}/{positives} same-story pairs are more than {args.window} days "
          f"apart ({len(lost) / positives:.0%} of the recall the window gives up)")
    if short:
        print(f"  {len(short)} pairs have a headline under {MIN_TITLE_CHARS} characters "
              "and are never compared")

    marks = {COLLAPSE_THRESHOLD: "shipped: fold", SUPPRESS_THRESHOLD: "shipped: hide"}
    scorers: list[tuple[str, object]] = [
        ("trigram, no window (challenge 1)", score_trigram),
        (f"trigram + {args.window}-day window (challenge 2, = the app)",
         windowed(score_trigram, args.window)),
        ("word Jaccard, no window", score_token_jaccard),
        ("IDF cosine, no window", make_idf_cosine(pairs)),
    ]
    if args.embeddings:
        scorers.append((f"embedding cosine ({args.embeddings})",
                        make_embedding_cosine(pairs, args.embeddings,
                                              args.embedding_prefix)))

    print("\n  reference: humans ~0.90 F1, best models in the paper 0.75, "
          "released Electra+time checkpoint 0.74")
    all_scored: dict[str, list[tuple[float, bool]]] = {}
    for name, score in scorers:
        scored = [(score(p), p.label) for p in pairs]
        all_scored[name] = scored
        report(name, scored, marks if "trigram" in name else {})
        if args.sweep and name.startswith("trigram +"):
            sweep_table(name, scored)

    gate_name = f"trigram + {args.window}-day window (challenge 2, = the app)"
    for name, scored in all_scored.items():
        if name.startswith("trigram"):
            continue
        stacked(name, all_scored[gate_name], scored,
                COLLAPSE_THRESHOLD, SUPPRESS_THRESHOLD)

    if args.examples is not None:
        examples(pairs, windowed(score_trigram, args.window), args.examples,
                 args.examples_limit)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
