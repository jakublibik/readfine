from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LexicalTerm(Base):
    """One term of the corpus statistics the lexical scorer reads its IDF from.

    Rebuilt whole by a nightly job over a recent window of articles (see
    ``app.services.relevance_corpus_service``), never updated in place: an
    incremental count has to be told about purges and failed fetches as well, and
    the repair for a drifted one is the rebuild anyway.

    Instance-wide, not per user. Document frequency is a property of the language
    the feeds are written in, and splitting it per reader would make the same word
    rare for one and common for another for no reason beyond who subscribed to
    what.
    """

    __tablename__ = "lexical_terms"

    term: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_freq: Mapped[int] = mapped_column(Integer, nullable=False)


class LexicalCorpus(Base):
    """Single row (``id = 1``) holding what the term counts alone cannot say.

    BM25 needs the document count for the IDF and the average document length for
    the length normalization, and both have to come from the same build as the
    terms. Keeping them here means a scorer can never pair one build's terms with
    another build's averages.

    ``built_at`` is also the cache key: the in-process dictionary reloads when this
    changes, so the workers do not each hold a stale table after a rebuild.
    """

    __tablename__ = "lexical_corpus"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    n_docs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_doc_len: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # What the table was built with. Stored rather than read from the module
    # constants so that a build made under different settings is visible as such
    # instead of being scored against silently.
    min_df: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    ngram_max: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    build_seconds: Mapped[float | None] = mapped_column(Float)
