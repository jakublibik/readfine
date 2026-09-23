"""Lexical relevance scoring: BM25 of an article against the reader's term list.

The weaker of the two scorers. It runs on every article at fetch time, from the
title and the start of the feed's description, and needs no API key, unlike AI
scoring, which stays on labeled articles only. It reads its own profile, a list
of terms (`relevance_terms`, see `parse_terms`), not the AI profile: a model reads a
description it understands, BM25 only looks words up, and the brackets and
generic words of a profile written for a model were what made it misfire.

Measured offline (`scripts/embedding_eval/`, step 2b of the plan): with a term
list this scorer gets AUC 0.659 against engagement on the September window and
0.677 on the independent summer one, where the LLM gets 0.729 and no score at all
0.50. A real signal and a thin one, and the UI says so.

What is a decision here and not detail:

- **A term is loose words, not a phrase.** Requiring the words to stand next
  to each other cost 0.035 AUC: "AI" from "AI safety" carries signal a phrase
  cuts off. The score is the max over terms, so a translation or a synonym is
  simply another term and nothing is counted twice.
- **Inflection by graded truncation, no stemmer and no language detection.** A
  query word that does not match exactly may match on its prefix (the word minus
  its last two characters, never shorter than four) at half the weight, with the
  prefix's own document frequency as its IDF. That is what lets `válka` find
  `války`; English mostly matches exactly and loses nothing (+0.026 AUC on Czech,
  +0.001 elsewhere, and the same again on the summer window).
- **Tokenization by script, not by language.** Han, kana and Hangul runs have no
  spaces, so they become overlapping character bigrams (the Lucene `cjk`
  approach, no unigrams: those matched noise on two thirds of Chinese articles),
  and a CJK term has to match as consecutive bigrams, since it is one word cut up
  rather than a list of words. Everything else is `\\w\\w+` words with accents
  stripped. NFKC first; accents are stripped outside CJK runs only, because NFKD
  splits a Hangul syllable into jamo and drops the voicing mark off kana.
- **Unknown terms score zero**, they are not smoothed. A term the corpus has not
  used often enough to be in the table is one this scorer has never seen, and
  guessing an IDF for it would hand the highest weight in the formula to the
  terms it knows the least about.
- **No length normalization** (BM25 `b = 0`). The input is already capped at a
  title and 300 characters, so normalizing mostly penalized an article for
  having a summary at all; it cost 0.013 AUC.
- **No negative side.** Subtracting an avoid list scored AUC 0.486 in the eval,
  below chance. The basic profile has no avoid list, and `parse_profile` below,
  which reads the AI profile for the one-off seed, returns its negatives only so
  that nothing mistakes them for positives.

Every number above was measured through exactly this tokenizer and this scorer,
so changing either one invalidates them rather than tuning them. `TOKENIZER`
travels with the stored term table for the same reason.
"""
from __future__ import annotations

import html as _html
import math
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Mapping, NamedTuple, Sequence

import nh3

# Bumped whenever tokenization changes. The stored term table records the version
# it was counted with, and a table counted by another version is treated as no
# table at all until it has been rebuilt: scoring against a vocabulary the
# tokenizer can no longer produce fails silently, with every score near zero.
TOKENIZER = 2

BM25_K1 = 1.5

# Graded truncation (step 2b, E3b, confirmed on a second window): cut the last
# two characters of a query word, never below four, and let what is left match
# at half the weight of an exact hit. Four is short enough for English to conflate
# a few words (`stress`/`street`), and five would lose the Czech gain entirely
# (`Praha`/`Praze`, `mozek`/`mozku`), which is what truncation is here for.
PREFIX_CUT = 2
PREFIX_MIN = 4
PREFIX_WEIGHT = 0.5

# How much of the summary the scorer reads. The eval measured `title300` (title
# plus the first 300 characters of the body); longer inputs did not help and
# waiting for readable extraction would mean scoring nothing at fetch time.
SUMMARY_MAX_CHARS = 300


# ── tokenization ──────────────────────────────────────────────────────────────

# Kana, CJK ideographs (extension A, unified, compatibility), Hangul syllables,
# jamo and compatibility jamo.
_CJK = ("぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿"
        "가-힯ᄀ-ᇿ㄰-㆏")
_CJK_SPLIT_RE = re.compile(rf"([{_CJK}]+)")
_CJK_RUN_RE = re.compile(rf"^[{_CJK}]+$")
# Matches scikit-learn's default `(?u)\b\w\w+\b`: two or more word characters,
# so single letters and punctuation drop out.
_WORD_RE = re.compile(r"\b\w\w+\b", re.UNICODE)


def strip_accents(text: str) -> str:
    """NFKD-normalize and drop combining marks, as `strip_accents="unicode"` does.

    Czech loses its diacritics here, which is deliberate: "bezpečnost" and
    "bezpecnost" being two different terms would only split a thin signal. Never
    applied to CJK runs, see `tokenize`.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def is_cjk(token: str) -> bool:
    return bool(_CJK_RUN_RE.match(token))


def tokenize(text: str) -> list[str]:
    """Tokens in document order, repeats included. The same for articles and terms.

    Text is split into runs by script, which needs no guess about the language:
    "OpenAI发布" is a Latin word followed by Han bigrams. A lone CJK character
    stays a token of its own, since there is no pair to make.
    """
    text = unicodedata.normalize("NFKC", text.lower())
    out: list[str] = []
    for run in _CJK_SPLIT_RE.split(text):
        if not run:
            continue
        if _CJK_RUN_RE.match(run):
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            out.extend(_WORD_RE.findall(strip_accents(run)))
    return out


def prefix_of(token: str) -> str | None:
    """The prefix a query word may also match on, or None for a short word."""
    if len(token) < PREFIX_MIN:
        return None
    return token[:max(PREFIX_MIN, len(token) - PREFIX_CUT)]


def token_prefixes(token: str) -> list[str]:
    """Every prefix any query word could cut down to and find in this token.

    What the prefix table counts: all prefixes from `PREFIX_MIN` characters up
    to the whole token, because a query word of any length may reduce to any of
    them.
    """
    return [token[:n] for n in range(PREFIX_MIN, len(token) + 1)]


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


# ── corpus statistics ─────────────────────────────────────────────────────────

def idf(df: int | None, n_docs: int) -> float:
    """BM25 IDF, or 0.0 for a term the window never saw often enough."""
    if not df:
        return 0.0
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


@dataclass(frozen=True)
class CorpusStats:
    """Document frequencies of tokens and of token prefixes over a recent window.

    `prefix_freq` counts documents containing any token that starts with the
    prefix, so a prefix match has an IDF of its own rather than borrowing one
    from whichever inflected form happened to be commonest. The app does not
    hold the whole prefix table in memory: it loads the prefixes the profiles
    being scored actually need (`relevance_corpus_service.with_prefixes`).
    """

    n_docs: int
    doc_freq: Mapping[str, int]
    prefix_freq: Mapping[str, int] = field(default_factory=dict)
    tokenizer: int = TOKENIZER

    def __bool__(self) -> bool:
        return self.n_docs > 0 and bool(self.doc_freq)


def doc_terms(text: str) -> tuple[set[str], set[str]]:
    """The distinct tokens and distinct prefixes of one document, for the counts."""
    tokens = set(tokenize(text))
    prefixes = {p for t in tokens for p in token_prefixes(t)}
    return tokens, prefixes


def build_corpus_stats(texts: Iterable[str], min_df: int = 3) -> CorpusStats:
    """Count document frequencies over a corpus held in memory.

    For the offline scripts and the tests; the app streams the same count out of
    the database, see `relevance_corpus_service`. `min_df` keeps the tables to
    terms that recur: the long tail of typos and one-off proper nouns is most of
    the term count and none of the signal.
    """
    doc_freq: dict[str, int] = {}
    prefix_freq: dict[str, int] = {}
    n_docs = 0
    for text in texts:
        n_docs += 1
        tokens, prefixes = doc_terms(text)
        for t in tokens:
            doc_freq[t] = doc_freq.get(t, 0) + 1
        for p in prefixes:
            prefix_freq[p] = prefix_freq.get(p, 0) + 1
    return CorpusStats(
        n_docs=n_docs,
        doc_freq={t: c for t, c in doc_freq.items() if c >= min_df},
        prefix_freq={p: c for p, c in prefix_freq.items() if c >= min_df})


# ── the term list ─────────────────────────────────────────────────────────────

_BULLET_CHARS = " -•*\t"
# What separates one term from the next: a new line, a comma or a semicolon,
# including the full-width forms and the ideographic enumeration comma a
# Chinese or Japanese list is written with. All of them mean the same thing; the
# semicolon is there because people reach for it to set a translation apart.
_TERM_SEPARATOR_RE = re.compile(r"[\n\r,;\uff0c\uff1b\u3001]")


def split_terms(text: str | None) -> list[str]:
    """The pieces of a term list as written, trimmed, empty ones out."""
    if not text:
        return []
    pieces = (p.strip(_BULLET_CHARS) for p in _TERM_SEPARATOR_RE.split(text))
    return [p for p in pieces if p]


def parse_terms(text: str | None) -> list[str]:
    """The basic profile as the scorer reads it: its terms, duplicates out.

    Terms are separated by new lines, commas or semicolons, all alike, and the
    stored text stays the way the reader wrote it; this is the only place that
    reads it apart. The words within a term are the one grouping there is: they
    add up, and the best-matching term is the score (see `bm25_raw`).

    No other syntax on purpose (no quotes, wildcards, weights or groups).
    Duplicates are judged after tokenization, so "AI Safety" and "ai safety" are
    one term, and a piece that yields no token at all (a stray "-" or a single
    letter) is dropped, since it could never match anything. Pasted bullets are
    forgiven.
    """
    return _read_terms(text)[0]


def skipped_terms(text: str | None) -> list[str]:
    """The pieces `parse_terms` leaves out, as written, to show the reader which."""
    return _read_terms(text)[1]


def _read_terms(text: str | None) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    skipped: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for term in split_terms(text):
        key = tuple(tokenize(term))
        if not key or key in seen:
            skipped.append(term)
            continue
        seen.add(key)
        kept.append(term)
    return kept, skipped


def terms_needed(terms: Iterable[str]) -> tuple[set[str], set[str]]:
    """Tokens and prefixes a term list will look up: what the app loads DF for."""
    tokens: set[str] = set()
    prefixes: set[str] = set()
    for term in terms:
        for tok in tokenize(term):
            tokens.add(tok)
            p = prefix_of(tok)
            if p:
                prefixes.add(p)
    return tokens, prefixes


class _Unit(NamedTuple):
    term: str
    tokens: tuple[str, ...]  # distinct, in order
    contiguous: bool         # a CJK term: its bigrams must stand in a row


@lru_cache(maxsize=512)
def _units(terms: tuple[str, ...]) -> tuple[_Unit, ...]:
    """Tokenized once per term list, not once per article it is scored against."""
    out = []
    for term in terms:
        tokens = tokenize(term)
        if not tokens:
            continue
        contiguous = len(tokens) >= 2 and all(is_cjk(t) for t in tokens)
        # A contiguous unit keeps repeats: they are positions, not a bag.
        out.append(_Unit(term, tuple(tokens) if contiguous
                         else tuple(dict.fromkeys(tokens)), contiguous))
    return tuple(out)


def _saturate(f: float) -> float:
    """BM25 term-frequency saturation with b = 0."""
    return f * (BM25_K1 + 1.0) / (f + BM25_K1)


def _run_count(doc: Sequence[str], unit: Sequence[str]) -> int:
    """How often `unit` occurs as consecutive tokens of `doc`."""
    m = len(unit)
    return sum(1 for i in range(len(doc) - m + 1)
               if all(doc[i + j] == unit[j] for j in range(m)))


class Match(NamedTuple):
    """The raw score and the term that produced it (None when nothing matched)."""

    score: float
    term: str | None


def bm25_raw(text: str, terms: Sequence[str], stats: CorpusStats) -> Match:
    """Unbounded BM25 score of one article against its best-matching term.

    Max over terms, not a sum: a list of thirty terms should not outscore one of
    three just by having more chances to match a little. Within a term the words
    add up, each one exact or, failing that, through its prefix.
    """
    if not stats:
        return Match(0.0, None)
    doc = tokenize(text)
    if not doc:
        return Match(0.0, None)
    counts: dict[str, int] = {}
    for t in doc:
        counts[t] = counts.get(t, 0) + 1

    best = Match(0.0, None)
    for unit in _units(tuple(terms)):
        if unit.contiguous:
            n = _run_count(doc, unit.tokens)
            score = (sum(idf(stats.doc_freq.get(t), stats.n_docs)
                         for t in dict.fromkeys(unit.tokens)) * _saturate(n)
                     if n else 0.0)
        else:
            score = sum(_word_score(q, counts, stats) for q in unit.tokens)
        if score > best.score:
            best = Match(score, unit.term)
    return best


def _word_score(q: str, counts: Mapping[str, int], stats: CorpusStats) -> float:
    """One query word against one article: exact match, else its prefix.

    The prefix match counts the article's other words that start with the
    prefix, including ones too rare to be in the token table: an inflected form
    seen twice this month is still the word the reader asked for.
    """
    df = stats.doc_freq.get(q)
    f = counts.get(q, 0)
    if df and f:
        return idf(df, stats.n_docs) * _saturate(f)
    p = prefix_of(q)
    if p is None:
        return 0.0
    pdf = stats.prefix_freq.get(p)
    if not pdf:
        return 0.0
    hits = sum(c for t, c in counts.items() if t != q and t.startswith(p))
    if not hits:
        return 0.0
    return PREFIX_WEIGHT * idf(pdf, stats.n_docs) * _saturate(hits)


# Fitted offline on the term-list configuration (step 2b, E8). Matching the LLM's
# distribution is not possible and was not attempted: 40% of articles score
# exactly zero here, and the LLM has a fat top tail where this has a thin one.
# Of one reader's inflow, 11% clears 0.7 against 29% of the LLM-scored articles,
# so the same threshold reads differently depending on which scorer produced the
# number, and the UI has to say so.
SQUASH_K = 3.15


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


def lexical_score(text: str, terms: Sequence[str], stats: CorpusStats,
                  k: float = SQUASH_K) -> float | None:
    """The 0..1 score stored on the article, or None when there is nothing to score.

    None and 0.0 mean different things: None is "this scorer had nothing to say"
    (no terms, no statistics yet), 0.0 is "it read the article and found no
    overlap".
    """
    if not terms or not stats:
        return None
    return squash(bm25_raw(text, terms, stats).score, k)


# ── the AI profile, read as topics ────────────────────────────────────────────

# Not what the basic scorer reads any more: that is `parse_terms`. This splits the
# AI profile into topics for the one-off seed that offers them as a starting term
# list, and the offline scripts measure the old baseline through it.

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

    High and Moderate land in the same positive list. The negatives are returned
    so that nothing mistakes them for positives, and are never offered as terms.
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


# ── the effective score ───────────────────────────────────────────────────────

def effective_score(ai_score: float | None,
                    lexical_score: float | None) -> tuple[float | None, bool]:
    """The best score an article has, and whether a model produced it.

    The AI score wins wherever it exists: it is measurably the better of the two
    (AUC 0.729 against 0.659 on the same articles), and it only exists where a
    filter labeled the article, so it is also the scarcer one.

    Returns `(None, False)` when neither scorer has said anything, which is not
    the same as a score of zero: zero means a scorer read the article and found
    no overlap.
    """
    if ai_score is not None:
        return ai_score, True
    return lexical_score, False


def effective_score_sql(state):
    """`effective_score` as a SQL expression over a UserArticleState (or alias).

    The Python and the SQL form are kept side by side on purpose: a query that
    ranked by one rule while a row displayed the other would be a bug nobody
    could see.
    """
    from sqlalchemy import func
    return func.coalesce(state.ai_score, state.lexical_score)
