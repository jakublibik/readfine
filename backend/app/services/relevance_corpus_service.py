"""The corpus statistics behind the lexical scorer: building them, caching them.

`relevance_service` knows how to score an article against a term list given a
`CorpusStats`. This is where that object comes from: a nightly job counts how
many of the last month's articles each token and each token prefix appears in,
writes the counts to `lexical_terms` and `lexical_prefixes`, and every process
reads the tokens back into a dictionary it keeps until the next build. Prefixes
are looked up only as the term lists being scored need them.

Rebuilt whole rather than kept up to date as articles arrive. An incremental
count has to hear about purges, re-fetches and deleted feeds as well, and once
it has drifted the only repair is the rebuild this does anyway. The job is
cheap: around ten seconds of tokenizing over a hundred thousand short texts,
once a day, off the fetch path.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.relevance import LexicalCorpus, LexicalPrefix, LexicalTerm
from app.services.relevance_service import (
    TOKENIZER,
    CorpusStats,
    article_text,
    doc_terms,
)

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
MIN_DF = 3
# `lexical_terms.term` is 64 chars. Anything longer is a run-on from stripped
# markup or a URL fragment, never a word the profile will contain, so it is
# dropped at build time and then simply never found at scoring time.
TERM_MAX_CHARS = 64
# Raw characters of body pulled per article. The scorer reads 300 characters of
# stripped text, and this is what that costs with markup around it. It bounds the
# job's IO: without it, a pass over a month of articles would read every
# extracted article body in full.
BODY_FETCH_CHARS = 4000
_INSERT_CHUNK = 5000

# Below this, the instance is still filling up and the statistics are worth
# recounting as often as anyone asks: a corpus of a few dozen articles at
# min_df=3 has almost no terms in it, so the scores it produces are close to
# useless and the rebuild that fixes them costs milliseconds. Above it, the
# nightly job is enough.
BOOTSTRAP_MAX_DOCS = 500

_cached: CorpusStats | None = None
_cached_built_at: datetime | None = None
# Prefix frequencies looked up so far for the cached build, including the ones
# the table does not have (as 0), so a missing prefix is asked about once.
_prefix_cache: dict[str, int] = {}


async def _iter_texts(db: AsyncSession, cutoff: datetime):
    """Stream the scorer's input text for every article in the window."""
    stmt = (
        select(
            Article.title,
            func.left(Article.readable_content, BODY_FETCH_CHARS),
            func.left(Article.content, BODY_FETCH_CHARS),
        )
        .where(Article.fetched_at >= cutoff)
        .execution_options(yield_per=1000)
    )
    result = await db.stream(stmt)
    async for title, readable, content in result:
        yield article_text(title, readable or content)


async def rebuild(db: AsyncSession, *, window_days: int = WINDOW_DAYS,
                  min_df: int = MIN_DF) -> dict:
    """Recount the corpus and replace the stored tables. Idempotent.

    One streaming pass counts tokens and token prefixes together. The counters
    are the job's whole memory cost, around 100 MB at the peak for a month of a
    hundred thousand articles, released when it returns; the articles are never
    held.
    """
    started = time.monotonic()
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    doc_freq: dict[str, int] = {}
    prefix_freq: dict[str, int] = {}
    n_docs = 0
    async for text in _iter_texts(db, cutoff):
        n_docs += 1
        tokens, prefixes = doc_terms(text)
        for t in tokens:
            doc_freq[t] = doc_freq.get(t, 0) + 1
        for p in prefixes:
            prefix_freq[p] = prefix_freq.get(p, 0) + 1
    terms = [{"term": t, "doc_freq": df} for t, df in doc_freq.items()
             if df >= min_df and len(t) <= TERM_MAX_CHARS]
    del doc_freq
    prefixes = [{"prefix": p, "doc_freq": df} for p, df in prefix_freq.items()
                if df >= min_df and len(p) <= TERM_MAX_CHARS]
    del prefix_freq

    await db.execute(delete(LexicalTerm))
    await db.execute(delete(LexicalPrefix))
    for table, rows in ((LexicalTerm.__table__, terms),
                        (LexicalPrefix.__table__, prefixes)):
        for i in range(0, len(rows), _INSERT_CHUNK):
            await db.execute(table.insert(), rows[i:i + _INSERT_CHUNK])

    elapsed = time.monotonic() - started
    corpus = await db.get(LexicalCorpus, 1)
    if corpus is None:
        corpus = LexicalCorpus(id=1)
        db.add(corpus)
    corpus.n_docs = n_docs
    corpus.tokenizer = TOKENIZER
    corpus.min_df = min_df
    corpus.window_days = window_days
    corpus.built_at = datetime.now(timezone.utc)
    corpus.build_seconds = elapsed
    await db.commit()

    logger.info("lexical corpus rebuilt: %d terms, %d prefixes over %d articles in %.1fs",
                len(terms), len(prefixes), n_docs, elapsed)
    return {"terms": len(terms), "prefixes": len(prefixes), "n_docs": n_docs,
            "seconds": elapsed}


async def ensure_built(db: AsyncSession, **rebuild_kwargs) -> bool:
    """Build the statistics if there are none worth using yet. Returns whether it did.

    Without this, a fresh install would score nothing until the first nightly
    run, which is up to a day of a new account seeing exactly what the feature
    exists to prevent: an unsorted list. It is also why the check is not simply
    "has it ever been built" — the first build on a new instance may count a
    handful of articles, and a corpus that small is worth redoing as the feeds
    fill in. A table counted by another tokenizer is rebuilt at once as well,
    which is how an upgrade that changes tokenization takes effect without
    waiting for the night.

    Costs one small query on an instance that is past that stage. The job passes
    no `rebuild_kwargs`; they exist so a test can narrow the window.
    """
    row = (await db.execute(
        select(LexicalCorpus.built_at, LexicalCorpus.n_docs, LexicalCorpus.tokenizer)
        .where(LexicalCorpus.id == 1)
    )).first()
    if (row is not None and row.built_at is not None
            and row.n_docs >= BOOTSTRAP_MAX_DOCS and row.tokenizer == TOKENIZER):
        return False
    if not await db.scalar(select(Article.id).limit(1)):
        return False  # nothing to count yet; a brand new instance with no fetch
    await rebuild(db, **rebuild_kwargs)
    return True


async def get_stats(db: AsyncSession) -> CorpusStats | None:
    """The current corpus statistics, or None before the first usable build.

    Reads one row to find out whether the cached dictionary is still the current
    build, and only re-reads the terms when it is not. None means the scorer has
    nothing to work with and must write no score, which is not the same as a
    score of zero. A build counted by another tokenizer is None too: its
    vocabulary is one the scorer can no longer produce.

    The prefix table is not part of what this returns, see `with_prefixes`.
    """
    global _cached, _cached_built_at

    row = (await db.execute(
        select(LexicalCorpus.built_at, LexicalCorpus.n_docs, LexicalCorpus.tokenizer)
        .where(LexicalCorpus.id == 1)
    )).first()
    if (row is None or row.built_at is None or not row.n_docs
            or row.tokenizer != TOKENIZER):
        return None
    if _cached is not None and _cached_built_at == row.built_at:
        return _cached

    terms = dict((await db.execute(
        select(LexicalTerm.term, LexicalTerm.doc_freq))).all())
    if not terms:
        return None
    _cached = CorpusStats(n_docs=row.n_docs, doc_freq=terms)
    _cached_built_at = row.built_at
    _prefix_cache.clear()
    logger.info("lexical corpus loaded: %d terms, built %s",
                len(terms), row.built_at.isoformat())
    return _cached


async def with_prefixes(db: AsyncSession, stats: CorpusStats,
                        prefixes: set[str]) -> CorpusStats:
    """`stats` with the prefix frequencies a set of term lists needs.

    The prefix table is several times the size of the term table and scoring
    only ever looks up the prefixes of the reader's own terms, so it is read a
    handful of rows at a time and remembered for the rest of the build.
    """
    missing = [p for p in prefixes if p not in _prefix_cache]
    if missing:
        found = dict((await db.execute(
            select(LexicalPrefix.prefix, LexicalPrefix.doc_freq)
            .where(LexicalPrefix.prefix.in_(missing)))).all())
        for p in missing:
            _prefix_cache[p] = found.get(p, 0)
    return replace(stats, prefix_freq={p: _prefix_cache[p] for p in prefixes
                                       if _prefix_cache[p]})


def reset_cache() -> None:
    """Drop the in-process copy. For tests and for the rebuild job's own process."""
    global _cached, _cached_built_at
    _cached = None
    _cached_built_at = None
    _prefix_cache.clear()
