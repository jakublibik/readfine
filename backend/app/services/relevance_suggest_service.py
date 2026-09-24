"""Suggested changes to a reader's term list: words to add, terms to drop.

Suggestions, not silent expansion: nothing changes until the reader clicks one,
which edits the list and saves it. A hand-kept list
that grew by itself would stop being the reader's, and they would not know why
an article scored.

Both directions read engagement over the last `WINDOW_DAYS` of the reader's own
inflow (the articles their feeds brought in). Engagement is dwell of 30 seconds
or more, an opened link or a star, never `is_read`: marking a page read with one
click is not interest.

- **Add** (Rocchio): the heaviest tokens of the TF-IDF centroid of the engaged
  articles, with IDF from the instance-wide corpus, one article per story: five
  outlets reporting one royal funeral are one thing the reader read, not five,
  and without this the names in that story were the whole suggestion list (not
  measured offline, the eval export has no story groups). Story groups do not
  catch every retelling, so a candidate must also come from engaged articles on
  at least `MIN_DAYS` different days: an interest recurs, a news event happens
  once. A candidate must appear in at least `MIN_SUPPORT` engaged stories, be at most `MAX_INFLOW_SHARE` of the
  reader's inflow (a word in every other headline is not a topic), not be a
  stopword and not be covered already by a term the reader has, exactly or
  through the scorer's prefix match.
- **Remove**: a term that matched at least `MIN_MATCHES` articles in the window
  and led to engagement at under `REMOVE_RATIO` of the reader's base rate.

Measured offline (step 2b, E7, and 2026-09-24 for an empty list; see
`scripts/embedding_eval/run_terms_suggest.py`): five Rocchio suggestions lift a
three-term list from AUC 0.545 to 0.678 and an empty one from 0.500 to 0.628,
and dropping the flagged terms from a full list gains a little (+0.006). The
words are a mixed bag (`brain`, `aging` next to `problem`), which is why each
comes with the headlines it was learned from.

The text the scorer reads at fetch time is the feed's own description, so that
is what is counted here too: a word that only occurs in the extracted page
would be suggested and then never match.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.relevance import RelevanceSuggestionDismissal
from app.services.relevance_corpus_service import BODY_FETCH_CHARS, get_stats, with_prefixes
from app.services.relevance_service import (
    _BULLET_CHARS,
    _TERM_SEPARATOR_RE,
    CorpusStats,
    article_text,
    idf,
    parse_terms,
    prefix_of,
    term_scores,
    terms_needed,
    tokenize,
)
from app.services.relevance_stopwords import STOPWORDS

WINDOW_DAYS = 30
# The newest articles of the window, at most. Enough for a base rate and for
# the inflow share, and it keeps the page from tokenizing a heavy reader's month.
MAX_INFLOW = 5000
# Below this many engaged articles there is nothing to learn from, and a single
# article would make a suggestion.
MIN_ENGAGED = 10

ADD_COUNT = 5
MIN_SUPPORT = 3
MIN_DAYS = 2
MAX_INFLOW_SHARE = 0.01
MIN_TOKEN_CHARS = 4

MIN_MATCHES = 15
REMOVE_RATIO = 0.5

# A turned-down suggestion stays away this long, then may come back: interests
# change, and after three windows it would be learned from different reading.
DISMISS_DAYS = 90

EXAMPLE_TITLES = 3

ADD = "add"
REMOVE = "remove"


@dataclass(frozen=True)
class Suggestion:
    kind: str                   # ADD or REMOVE
    term: str                   # as shown, and as it is added to or cut from the list
    key: str                    # what a dismissal is stored under
    titles: tuple[str, ...]     # add: engaged headlines it was learned from
    matched: int                # remove: articles the term matched in the window
    engaged: int                # remove: how many of those were engaged with


@dataclass(frozen=True)
class Suggestions:
    items: tuple[Suggestion, ...]
    engaged: int                # engaged articles in the window
    enough: bool                # engaged >= MIN_ENGAGED

    @property
    def adds(self) -> tuple[Suggestion, ...]:
        return tuple(s for s in self.items if s.kind == ADD)

    @property
    def removes(self) -> tuple[Suggestion, ...]:
        return tuple(s for s in self.items if s.kind == REMOVE)


def term_key(term: str) -> str:
    """One spelling per term: `AI Safety` and `ai safety` are the same suggestion."""
    return " ".join(tokenize(term))


# ── the computation (no database, so the tests and the eval can call it) ──────

def _covered(tok: str, profile_tokens: set[str]) -> bool:
    """Whether a term the reader has would already match `tok`."""
    for q in profile_tokens:
        if tok == q:
            return True
        p = prefix_of(q)
        if p and tok.startswith(p):
            return True
    return False


def _rocchio(docs: list[list[str]], stats: CorpusStats) -> dict[str, tuple[float, int]]:
    """Centroid weight and support (engaged articles containing it) per token."""
    acc: dict[str, float] = {}
    support: dict[str, int] = {}
    n = 0
    for doc in docs:
        if not doc:
            continue
        n += 1
        tf: dict[str, int] = {}
        for t in doc:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            w = idf(stats.doc_freq.get(t), stats.n_docs)
            if w > 0:
                acc[t] = acc.get(t, 0.0) + (c / len(doc)) * w
                support[t] = support.get(t, 0) + 1
    return {t: (v / n, support[t]) for t, v in acc.items()}


def _pick_add(ranked: dict[str, tuple[float, int]], profile_tokens: set[str],
              inflow_df: dict[str, int], n_inflow: int, skip: set[str]) -> list[str]:
    out: list[str] = []
    for t, (_w, sup) in sorted(ranked.items(), key=lambda x: -x[1][0]):
        if sup < MIN_SUPPORT or len(t) < MIN_TOKEN_CHARS or t.isdigit():
            continue
        if t in STOPWORDS or t in skip:
            continue
        df = inflow_df.get(t, 0)
        if not df or df / n_inflow > MAX_INFLOW_SHARE:
            continue
        if _covered(t, profile_tokens) or any(_covered(t, {o}) or _covered(o, {t})
                                              for o in out):
            continue
        out.append(t)
        if len(out) == ADD_COUNT:
            break
    return out


def compute(rows: list[tuple[str | None, str | None, bool, int, date]], terms: list[str],
            stats: CorpusStats, dismissed: set[tuple[str, str]]) -> Suggestions:
    """Suggestions from `(title, description, engaged, story, day)` rows, newest first.

    `story` is the story group (the article's own id when it has none), `day`
    the day it was fetched.
    """
    texts = [article_text(title, body) for title, body, *_ in rows]
    labels = [bool(r[2]) for r in rows]
    docs = [tokenize(t) for t in texts]
    n_engaged = sum(labels)
    if n_engaged < MIN_ENGAGED or not stats:
        return Suggestions((), n_engaged, n_engaged >= MIN_ENGAGED)

    items: list[Suggestion] = []

    base = n_engaged / len(rows)
    matched = {t: [0, 0] for t in terms}
    for text, label in zip(texts, labels):
        for term, score in term_scores(text, terms, stats):
            if score > 0:
                matched[term][0] += 1
                matched[term][1] += label
    for term in terms:
        n, e = matched[term]
        key = term_key(term)
        if n >= MIN_MATCHES and e / n < REMOVE_RATIO * base and (key, REMOVE) not in dismissed:
            items.append(Suggestion(REMOVE, term, key, (), n, e))

    profile_tokens = {t for term in terms for t in tokenize(term)}
    inflow_df: dict[str, int] = {}
    for doc in docs:
        for t in set(doc):
            inflow_df[t] = inflow_df.get(t, 0) + 1
    # The newest engaged article of each story stands for it.
    per_story: dict[int, tuple[str, list[str], date]] = {}
    for (title, _, e, story, day), doc in zip(rows, docs):
        if e and story not in per_story:
            per_story[story] = ((title or "").strip(), doc, day)
    ranked = _rocchio([doc for _, doc, _ in per_story.values()], stats)
    days: dict[str, set[date]] = {}
    for _, doc, day in per_story.values():
        for tok in set(doc):
            days.setdefault(tok, set()).add(day)
    skip = {k for k, kind in dismissed if kind == ADD}
    skip |= {tok for tok in ranked if len(days.get(tok, ())) < MIN_DAYS}
    for tok in _pick_add(ranked, profile_tokens, inflow_df, len(docs), skip):
        titles = tuple(title for title, doc, _ in per_story.values()
                       if tok in doc)[:EXAMPLE_TITLES]
        items.append(Suggestion(ADD, tok, tok, titles, 0, 0))

    return Suggestions(tuple(items), n_engaged, True)


# ── editing the list as written ───────────────────────────────────────────────

_SPLIT_KEEP_RE = re.compile(f"({_TERM_SEPARATOR_RE.pattern})")


def add_term(text: str | None, term: str) -> str:
    """The list with `term` appended, written the way the list already is.

    Nothing is reordered or rewritten. A list of lines gets a new line, anything
    else a comma. A term the list already has leaves it unchanged.
    """
    body = (text or "").rstrip()
    key = term_key(term)
    if not body:
        return term
    if key in {term_key(t) for t in parse_terms(body)}:
        return body
    if _TERM_SEPARATOR_RE.fullmatch(body[-1]):
        return f"{body} {term}"
    last_line = body.rsplit("\n", 1)[-1]
    if "\n" in body and not re.search(r"[,;，；、]", last_line):
        return f"{body}\n{term}"
    return f"{body}, {term}"


def _terms_and_gaps(text: str) -> tuple[list[str], list[str]]:
    """The list as its terms and the raw text around them: `gaps[0] + terms[0] +
    gaps[1] + terms[1] + ... + gaps[-1]` is the text again.

    A gap is everything between two terms: separators, whitespace, and pieces
    with no word in them (a lone `-`, an empty slot between `,,`).
    """
    terms: list[str] = []
    gaps: list[str] = [""]
    for i, part in enumerate(_SPLIT_KEEP_RE.split(text)):
        if i % 2 == 0 and part.strip(_BULLET_CHARS + " \n"):
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            gaps[-1] += lead
            terms.append(part.strip())
            gaps.append(trail)
        else:
            gaps[-1] += part
    return terms, gaps


def _one_separator(gap: str) -> str:
    """What is left between two terms after the one between them went.

    A new line wins, so a list of lines stays a list of lines; otherwise the
    first comma or semicolon of the gap, with a space after it if the gap had one.
    """
    if "\n" in gap:
        return "\n"
    m = re.search(r"[,;\uff0c\uff1b\u3001]", gap)
    if not m:
        return " "
    return m.group() + (" " if " " in gap[m.end():] else "")


def remove_term(text: str | None, term: str) -> str:
    """The list without `term` (every spelling of it), one separator left in its place.

    Where the term stood between two others, the gaps on both sides become one
    separator (`a, b, c` minus `b` is `a, c`, and never `a,, c`). At the start or
    the end the gap goes with it, so the list neither starts nor ends with a
    separator. The rest of the text is not touched, a stray `,,` elsewhere included.
    """
    terms, gaps = _terms_and_gaps(text or "")
    key = term_key(term)
    i = 0
    while i < len(terms):
        if term_key(terms[i].strip(_BULLET_CHARS)) != key:
            i += 1
            continue
        if len(terms) == 1:
            return ""
        if i == 0 or i == len(terms) - 1:
            gaps[i:i + 2] = [""]
        else:
            gaps[i:i + 2] = [_one_separator(gaps[i] + gaps[i + 1])]
        del terms[i]
    out = gaps[0]
    for term_text, gap in zip(terms, gaps[1:]):
        out += term_text + gap
    return out.strip()


# ── database ──────────────────────────────────────────────────────────────────

def _engaged():
    s = UserArticleState
    return or_(s.dwell_seconds >= 30, s.link_opened, s.ever_starred)


async def _rows(user_id: int, db: AsyncSession,
                now: datetime) -> list[tuple[str | None, str | None, bool, int, date]]:
    s = UserArticleState
    stmt = (
        select(Article.title, func.left(Article.content, BODY_FETCH_CHARS),
               func.coalesce(_engaged(), false()),
               func.coalesce(Article.story_id, Article.id),
               func.date(Article.fetched_at))
        .join(UserFeed, and_(UserFeed.feed_id == Article.feed_id, UserFeed.user_id == user_id))
        .outerjoin(s, and_(s.article_id == Article.id, s.user_id == user_id))
        .where(Article.fetched_at >= now - timedelta(days=WINDOW_DAYS))
        .order_by(Article.fetched_at.desc(), Article.id.desc())
        .limit(MAX_INFLOW)
    )
    return [tuple(r) for r in (await db.execute(stmt)).all()]


async def _dismissed(user_id: int, db: AsyncSession, now: datetime) -> set[tuple[str, str]]:
    d = RelevanceSuggestionDismissal
    rows = await db.execute(
        select(d.term, d.kind).where(
            d.user_id == user_id, d.dismissed_at >= now - timedelta(days=DISMISS_DAYS)))
    return {(t, k) for t, k in rows.all()}


async def suggestions(user_id: int, terms_text: str | None, db: AsyncSession,
                      now: datetime | None = None) -> Suggestions | None:
    """The reader's suggestions against their saved list, or None before the
    corpus statistics exist (nothing can be weighed yet)."""
    now = now or datetime.now(timezone.utc)
    stats = await get_stats(db)
    if stats is None:
        return None
    terms = parse_terms(terms_text)
    stats = await with_prefixes(db, stats, terms_needed(terms)[1])
    rows = await _rows(user_id, db, now)
    dismissed = await _dismissed(user_id, db, now)
    # Tokenizing a month of headlines is CPU work; keep it off the event loop.
    return await asyncio.to_thread(compute, rows, terms, stats, dismissed)


async def dismiss(user_id: int, term: str, kind: str, db: AsyncSession) -> None:
    """Turn a suggestion down for `DISMISS_DAYS`. Dismissing again restarts the clock."""
    key = term_key(term)[:200]
    if not key or kind not in (ADD, REMOVE):
        return
    d = RelevanceSuggestionDismissal
    stmt = pg_insert(d).values(user_id=user_id, term=key, kind=kind)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[d.user_id, d.term, d.kind], set_={"dismissed_at": func.now()}))
    await db.commit()
