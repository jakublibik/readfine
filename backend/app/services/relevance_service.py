"""Lexical relevance scoring: BM25 of an article against the interest profile.

The weaker of the two scorers. It runs on every article at fetch time, from the
title and the start of the summary, and needs no API key — unlike AI scoring,
which stays on labeled articles only. Measured offline (see
`scripts/embedding_eval/`): on the clean September window this scorer gets AUC
0.566 against engagement with a fresh three-topic profile and 0.628 with a
generated one, where the LLM gets 0.729 and no score at all gets 0.50. It is a
real signal and a thin one, and the UI says so.

Three things here are decisions, not detail:

- **The avoid list is never subtracted.** BM25 with the negative side subtracted
  scored AUC 0.486 in the eval, i.e. *below chance*: it ranked by the avoid list
  in reverse. `parse_profile` still returns the negatives, because the LLM reads
  them and a later hard rule could, but `bm25_raw` never looks at them.
- **Unknown terms score zero**, they are not smoothed. A term the corpus has not
  used often enough to be in `CorpusStats` is one this scorer has never seen, and
  guessing an IDF for it would hand the highest weight in the formula to the
  terms it knows the least about.
- **The tokenizer is the eval's tokenizer** (lowercase, accents stripped,
  `\\w\\w+` tokens). Every number quoted above was measured through it, so changing
  it invalidates them rather than merely tuning them. The one deviation is
  bigrams, which the eval had and this does not: see `NGRAM_MAX`.
"""
from __future__ import annotations

import html as _html
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import nh3

BM25_K1 = 1.5
# Length normalization, off. Standard BM25 uses 0.75, and the eval baseline did
# too, but the input here is already capped at a title plus 300 characters, so
# what the normalization mostly does is penalize an article for having a summary
# next to one that arrived with none (14.6% of the sample). Measured on the clean
# window it costs AUC 0.013 [0.006, 0.019], above zero in every one of 2000
# resamples, and one in five of the top twenty articles.
BM25_B = 0.0

# Matches scikit-learn's default `(?u)\b\w\w+\b`: two or more word characters,
# so single letters and punctuation drop out.
_TOKEN_RE = re.compile(r"\b\w\w+\b", re.UNICODE)

# How much of the summary the scorer reads. The eval measured `title300` (title
# plus the first 300 characters of the body); longer inputs did not help and
# waiting for readable extraction would mean scoring nothing at fetch time.
SUMMARY_MAX_CHARS = 300


def strip_accents(text: str) -> str:
    """NFKD-normalize and drop combining marks, as `strip_accents="unicode"` does.

    Czech loses its diacritics here, which is deliberate: the profile is usually
    written in English and lexical matching is literal, so "bezpečnost" and
    "bezpecnost" being two different terms only splits an already thin signal.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(strip_accents(text.lower()))


# Bigrams are what would let "AI safety" match as a phrase rather than as two of
# the commonest words in the corpus. Measured, they are worth +0.004 AUC
# [+0.001, +0.008] with a generated profile and +0.000 [-0.000, +0.001] on a
# cold start, for twice the term table (11144 terms against 5404 on 3106
# articles). Left off: a gain that small sits well inside the gap this scorer is
# trying to close, and the table is the one part of this that grows with the
# install. The offline script still builds them, so flipping this back is a
# constant and a table rebuild, not a rewrite.
NGRAM_MAX = 1


def terms(text: str, ngram_max: int = NGRAM_MAX) -> list[str]:
    """Tokens in document order, repeats included, plus n-grams up to `ngram_max`."""
    tokens = tokenize(text)
    out = list(tokens)
    for n in range(2, ngram_max + 1):
        out.extend(" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1))
    return out


_WHITESPACE_RE = re.compile(r"\s+")


def plain_text(content: str | None) -> str:
    """Strip HTML, unescape entities, collapse whitespace.

    Deliberately not `ai_jobs.normalize_text`, which does the same three steps:
    importing it would pull the app config in, and this module has to stay
    importable by the offline scripts, which run without one. The steps are kept
    identical because the offline sample was exported through that path, and the
    measured numbers only carry over while the text does.
    """
    plain = nh3.clean(content or "", tags=set())
    return _WHITESPACE_RE.sub(" ", _html.unescape(plain)).strip()


def article_text(title: str | None, content: str | None) -> str:
    """The input the scorer reads: title plus the head of the body, HTML stripped.

    At fetch time `content` is the feed's own description, which is HTML. Scoring
    it raw would tokenize tag and attribute names, and `href` would end up one of
    the commonest terms in the corpus.
    """
    head = plain_text(content)[:SUMMARY_MAX_CHARS]
    return f"{title or ''}\n\n{head}".strip() if head else (title or "").strip()


@dataclass(frozen=True)
class CorpusStats:
    """Document frequencies over a recent window of articles.

    `avg_doc_len` counts only terms that are in `doc_freq`, because those are the
    only ones any score is built from; measuring length over terms the scorer
    then ignores would make the length normalization answer a different question
    than the score does.
    """

    n_docs: int
    avg_doc_len: float
    doc_freq: Mapping[str, int]
    # Carried with the table rather than read from the module constant: a table
    # built with bigrams and a scorer tokenizing without them would silently
    # score every article against a vocabulary it cannot produce.
    ngram_max: int = NGRAM_MAX

    def idf(self, term: str) -> float:
        """BM25 IDF, or 0.0 for a term the window never saw often enough.

        Can go slightly negative for a term in more than half the corpus, which
        is the intended "this word tells us nothing" behaviour.
        """
        df = self.doc_freq.get(term)
        if not df:
            return 0.0
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))


def unique_terms(text: str, ngram_max: int = NGRAM_MAX) -> set[str]:
    """The distinct terms of one document, i.e. its contribution to any count."""
    return set(terms(text, ngram_max))


def known_term_count(text: str, doc_freq: Mapping[str, int],
                     ngram_max: int = NGRAM_MAX) -> int:
    """Length of one document measured the way BM25 will measure it."""
    return sum(1 for t in terms(text, ngram_max) if t in doc_freq)


def count_doc_freq(texts: Iterable[str],
                   ngram_max: int = NGRAM_MAX) -> tuple[dict[str, int], int]:
    """First pass: how many documents each term appears in, and how many there are.

    Streams, and keeps only the counter. The app builds this over every article of
    the last month, so holding the tokenized documents would mean millions of
    strings in memory on a VPS that has none to spare.
    """
    doc_freq: dict[str, int] = {}
    n_docs = 0
    for text in texts:
        n_docs += 1
        for term in unique_terms(text, ngram_max):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    return doc_freq, n_docs


def mean_doc_len(texts: Iterable[str], doc_freq: Mapping[str, int],
                 ngram_max: int = NGRAM_MAX) -> float:
    """Second pass: average length counted over the terms that survived the cutoff.

    It takes a second pass because the cutoff is not known until the first one has
    finished, and a length measured over terms the scorer will ignore would make
    the length normalization answer a different question than the score does.
    """
    total = 0
    n_docs = 0
    for text in texts:
        n_docs += 1
        total += known_term_count(text, doc_freq, ngram_max)
    return (total / n_docs) if n_docs else 0.0


def build_corpus_stats(texts: Sequence[str], min_df: int = 3,
                       ngram_max: int = NGRAM_MAX) -> CorpusStats:
    """Count document frequencies over a corpus held in memory.

    For the offline scripts and the tests. The app streams the same two passes
    straight out of the database, see `relevance_corpus_service`. `min_df` keeps
    the table to terms that recur: the long tail of typos and one-off proper nouns
    is most of the term count and none of the signal.
    """
    doc_freq, n_docs = count_doc_freq(texts, ngram_max)
    kept = {t: df for t, df in doc_freq.items() if df >= min_df}
    return CorpusStats(n_docs=n_docs,
                       avg_doc_len=mean_doc_len(texts, kept, ngram_max),
                       doc_freq=kept, ngram_max=ngram_max)


def bm25_raw(text: str, units: Iterable[str], stats: CorpusStats) -> float:
    """Unbounded BM25 score of one article against the best-matching profile unit.

    Max over units, not a sum: a profile with thirty topics should not outscore
    one with three just by having more chances to match a little.

    Query terms count once each. BM25's k3 term-frequency component does nothing
    on queries as short as a profile topic.
    """
    if stats.n_docs <= 0 or not stats.avg_doc_len:
        return 0.0

    doc_terms = terms(text, stats.ngram_max)
    freqs: dict[str, int] = {}
    doc_len = 0
    for term in doc_terms:
        if term in stats.doc_freq:
            freqs[term] = freqs.get(term, 0) + 1
            doc_len += 1
    if not freqs:
        return 0.0

    norm = BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / stats.avg_doc_len)
    best = 0.0
    for unit in units:
        score = 0.0
        for term in set(terms(unit, stats.ngram_max)):
            f = freqs.get(term)
            if not f:
                continue
            score += stats.idf(term) * f * (BM25_K1 + 1.0) / (f + norm)
        best = max(best, score)
    return best


# Fitted offline against the decile-by-decile LLM score distribution of the same
# articles (`scripts/embedding_eval/run_lexical_fidelity.py --calibrate`).
# Matching the whole distribution is not possible and was not attempted: the LLM
# has a fat top tail and BM25 a thin one, so on the clean window 28.2% of
# articles clear an LLM 0.7 and 3.3% clear a lexical one. Thin is the safe
# direction for a filter that sweeps, but the same threshold does read
# differently depending on which scorer produced the number, and the UI has to
# say so.
SQUASH_K = 3.7


def squash(raw: float, k: float = SQUASH_K) -> float:
    """Map an unbounded BM25 score onto 0..1 with `s / (s + k)`.

    A percentile inside the fetch batch would be the other obvious mapping and is
    the wrong one: in a quiet hour a handful of mediocre articles would score
    high for lack of competition, and a threshold in a filter would stop meaning
    anything from one hour to the next.
    """
    if raw <= 0.0:
        return 0.0
    return raw / (raw + k)


# ── profile parsing ───────────────────────────────────────────────────────────

# The generator writes `label: topics` lines and asks for "High relevance /
# Moderate relevance / Avoid", but the model may translate or reword the labels,
# so both are matched loosely and anything unrecognised counts as positive.
_NEGATIVE_LABEL_RE = re.compile(
    r"avoid|exclude|not interested|no interest|dislike|skip|irrelevant|"
    r"nezajím|vyhýb|vynech|nechci", re.IGNORECASE)
# Splitting on the Czech conjunction "a" is deliberately left out: it collides
# with the English article, and the profile is written in English by default.
_TOPIC_SEPARATOR_RE = re.compile(r"[,;]|\band\b|\bnebo\b")
_MIN_TOPIC_CHARS = 4


def split_topics(line: str) -> list[str]:
    """Split a topic list on separators that sit outside brackets.

    The generator writes topics like "health science with mechanistic findings
    (nutrition, exercise, longevity)". Splitting on every comma turns that into
    bare "exercise" and "longevity)" — fragments that have lost the context that
    made them a topic, and that then match unrelated articles.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    tokens = _TOPIC_SEPARATOR_RE.split(line)
    separators = _TOPIC_SEPARATOR_RE.findall(line)
    for i, token in enumerate(tokens):
        current.append(token)
        depth += token.count("(") - token.count(")")
        if i < len(separators):
            if depth > 0:  # separator inside brackets: keep the topic together
                current.append(separators[i])
            else:
                parts.append("".join(current))
                current = []
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


@dataclass(frozen=True)
class Profile:
    """The interest profile split into the units a lexical score is built from."""

    positive: list[str]
    negative: list[str]

    def __bool__(self) -> bool:
        return bool(self.positive)


def parse_profile(text: str | None) -> Profile:
    """Split `ai_preference_text` into positive and negative topic units.

    High and Moderate land in the same positive list. The eval's discount for
    Moderate topics is left out on purpose: it was expressed in standard
    deviations of the scorer's own similarity distribution, which needs the whole
    batch, and per-article scoring has no batch.

    The negatives are returned but never scored, see the module docstring.
    """
    if not text or not text.strip():
        return Profile([], [])

    positive_lines: list[str] = []
    negative_lines: list[str] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip(" -•\t")
        if not line:
            continue
        label, _, topics = line.partition(":")
        if not topics.strip():
            # A line without a label is a plain sentence: keep it whole and positive.
            positive_lines.append(line)
        elif _NEGATIVE_LABEL_RE.search(label):
            negative_lines.append(topics.strip())
        else:
            positive_lines.append(topics.strip())

    def units(lines: list[str]) -> list[str]:
        out = [t for line in lines
               for t in split_topics(line) if len(t) >= _MIN_TOPIC_CHARS]
        return out or ([" ".join(lines)] if lines else [])

    positive = units(positive_lines)
    if not positive and negative_lines:
        # Nothing but an avoid list: there is nothing to rank by, and scoring the
        # avoid list as if it were positive is the failure mode this whole module
        # is written around.
        return Profile([], units(negative_lines))
    return Profile(positive, units(negative_lines))


def lexical_score(text: str, profile: Profile, stats: CorpusStats,
                  k: float = SQUASH_K) -> float | None:
    """The 0..1 score stored on the article, or None when there is nothing to score.

    None and 0.0 mean different things: None is "this scorer had nothing to say"
    (no profile, no term statistics yet), 0.0 is "it read the article and found
    no overlap".
    """
    if not profile or stats.n_docs <= 0 or not stats.doc_freq:
        return None
    return squash(bm25_raw(text, profile.positive, stats), k)
