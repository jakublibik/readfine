from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, SmallInteger, String, func
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


class LexicalPrefix(Base):
    """Document frequency of a token prefix: how many articles hold a word starting with it.

    The IDF of a truncated match (`válka` finding `války`). Counted in the same
    pass and window as ``LexicalTerm``, for every prefix of four characters and
    up of every token, because a query word of any length may cut down to any of
    them. Around 200k rows at production size; the app never loads it whole, only
    the prefixes of the term lists it is scoring against.
    """

    __tablename__ = "lexical_prefixes"

    prefix: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_freq: Mapped[int] = mapped_column(Integer, nullable=False)


class LexicalCorpus(Base):
    """Single row (``id = 1``) holding what the term counts alone cannot say.

    BM25 needs the document count for the IDF, and it has to come from the same
    build as the terms. Keeping it here means a scorer can never pair one build's
    terms with another build's count.

    ``built_at`` is also the cache key: the in-process dictionary reloads when this
    changes, so the workers do not each hold a stale table after a rebuild.
    """

    __tablename__ = "lexical_corpus"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    n_docs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # What the table was built with. Stored rather than read from the module
    # constants so that a build made under different settings is visible as such
    # instead of being scored against silently. A `tokenizer` other than
    # `relevance_service.TOKENIZER` counts as no build at all.
    tokenizer: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    min_df: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    build_seconds: Mapped[float | None] = mapped_column(Float)


class RelevanceSuggestionDismissal(Base):
    """A suggested term the reader said no to, so it is never suggested again.

    ``kind`` is which suggestion it was (``add`` or ``remove``): turning down
    "remove crypto" says nothing about whether "crypto" would be welcome as an
    addition after the reader deleted it by hand.
    """

    __tablename__ = "relevance_suggestion_dismissals"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    term: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
