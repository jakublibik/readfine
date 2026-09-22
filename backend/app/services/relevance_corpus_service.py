"""The corpus statistics behind the lexical scorer: building them, caching them.

`relevance_service` knows how to score an article against a profile given a
`CorpusStats`. This is where that object comes from: a nightly job counts how
many of the last month's articles each term appears in, writes the counts to
`lexical_terms`, and every process reads them back into a dictionary it keeps
until the next build.

Rebuilt whole rather than kept up to date as articles arrive. An incremental
count has to hear about purges, re-fetches and deleted feeds as well, and once
it has drifted the only repair is the rebuild this does anyway. The job is
cheap: a few seconds of tokenizing over some tens of thousands of short texts,
once a day, off the fetch path.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.relevance import LexicalCorpus, LexicalTerm
from app.services.relevance_service import (
    NGRAM_MAX,
    CorpusStats,
    article_text,
    known_term_count,
    unique_terms,
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
# job's IO: without it, two passes over a month of articles would read every
# extracted article body in full, twice.
BODY_FETCH_CHARS = 4000
_INSERT_CHUNK = 5000

_cached: CorpusStats | None = None
_cached_built_at: datetime | None = None


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
                  min_df: int = MIN_DF, ngram_max: int = NGRAM_MAX) -> dict:
    """Recount the corpus and replace the stored table. Idempotent.

    Two passes over the window, because the average document length has to be
    measured over the terms that survive the cutoff and the cutoff is not known
    until the first pass has finished. Both stream; neither holds the articles.
    """
    started = time.monotonic()
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    doc_freq, n_docs = await _count(db, cutoff, ngram_max)
    kept = {t: df for t, df in doc_freq.items()
            if df >= min_df and len(t) <= TERM_MAX_CHARS}
    del doc_freq
    avg_doc_len = await _mean_len(db, cutoff, kept, ngram_max) if kept else 0.0

    await db.execute(delete(LexicalTerm))
    rows = [{"term": t, "doc_freq": df} for t, df in kept.items()]
    for i in range(0, len(rows), _INSERT_CHUNK):
        await db.execute(LexicalTerm.__table__.insert(), rows[i:i + _INSERT_CHUNK])

    elapsed = time.monotonic() - started
    corpus = await db.get(LexicalCorpus, 1)
    if corpus is None:
        corpus = LexicalCorpus(id=1)
        db.add(corpus)
    corpus.n_docs = n_docs
    corpus.avg_doc_len = avg_doc_len
    corpus.min_df = min_df
    corpus.ngram_max = ngram_max
    corpus.window_days = window_days
    corpus.built_at = datetime.now(timezone.utc)
    corpus.build_seconds = elapsed
    await db.commit()

    logger.info("lexical corpus rebuilt: %d terms over %d articles in %.1fs",
                len(rows), n_docs, elapsed)
    return {"terms": len(rows), "n_docs": n_docs, "avg_doc_len": avg_doc_len,
            "seconds": elapsed}


async def _count(db: AsyncSession, cutoff: datetime,
                 ngram_max: int) -> tuple[dict[str, int], int]:
    doc_freq: dict[str, int] = {}
    n_docs = 0
    async for text in _iter_texts(db, cutoff):
        n_docs += 1
        for term in unique_terms(text, ngram_max):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    return doc_freq, n_docs


async def _mean_len(db: AsyncSession, cutoff: datetime, doc_freq: dict[str, int],
                    ngram_max: int) -> float:
    total = 0
    n_docs = 0
    async for text in _iter_texts(db, cutoff):
        n_docs += 1
        total += known_term_count(text, doc_freq, ngram_max)
    return (total / n_docs) if n_docs else 0.0


async def get_stats(db: AsyncSession) -> CorpusStats | None:
    """The current corpus statistics, or None before the first build.

    Reads one row to find out whether the cached dictionary is still the current
    build, and only re-reads the terms when it is not. None means the scorer has
    nothing to work with and must write no score, which is not the same as a
    score of zero.
    """
    global _cached, _cached_built_at

    row = (await db.execute(
        select(LexicalCorpus.built_at, LexicalCorpus.n_docs,
               LexicalCorpus.avg_doc_len, LexicalCorpus.ngram_max)
        .where(LexicalCorpus.id == 1)
    )).first()
    if row is None or row.built_at is None or not row.n_docs or not row.avg_doc_len:
        return None
    if _cached is not None and _cached_built_at == row.built_at:
        return _cached

    terms = dict((await db.execute(
        select(LexicalTerm.term, LexicalTerm.doc_freq))).all())
    if not terms:
        return None
    _cached = CorpusStats(n_docs=row.n_docs, avg_doc_len=row.avg_doc_len,
                          doc_freq=terms, ngram_max=row.ngram_max)
    _cached_built_at = row.built_at
    logger.info("lexical corpus loaded: %d terms, built %s",
                len(terms), row.built_at.isoformat())
    return _cached


def reset_cache() -> None:
    """Drop the in-process copy. For tests and for the rebuild job's own process."""
    global _cached, _cached_built_at
    _cached = None
    _cached_built_at = None
