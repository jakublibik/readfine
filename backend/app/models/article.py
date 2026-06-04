from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, Integer, SmallInteger,
    String, Text, ForeignKey, func, CheckConstraint, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint(
            "readable_status IN ('pending', 'success', 'failed', 'skipped')",
            name="ck_articles_readable_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("feeds.id", ondelete="SET NULL"))
    guid: Mapped[str] = mapped_column(String(2048), nullable=False)
    guid_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    url_normalized: Mapped[str | None] = mapped_column(String(2048), index=True)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(Text)
    content_source: Mapped[str | None] = mapped_column(String(20))
    readable_content: Mapped[str | None] = mapped_column(Text)
    readable_status: Mapped[str] = mapped_column(String(10), nullable=False, default="skipped")
    readable_error: Mapped[str | None] = mapped_column(String(500))
    readable_retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    readable_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    readable_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    estimated_read_min: Mapped[int | None] = mapped_column(SmallInteger)
    word_count: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(String(2048))
    # Set by the retention trim pass (#tiered retention): body stripped to a profile
    # snippet, article hidden from listings/search/counts. NULL = not trimmed.
    trimmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    feed: Mapped["Feed | None"] = relationship(back_populates="articles")
    user_states: Mapped[list["UserArticleState"]] = relationship(back_populates="article", cascade="all, delete-orphan")
    article_labels: Mapped[list["ArticleLabel"]] = relationship(back_populates="article", cascade="all, delete-orphan")


class UserArticleState(Base):
    __tablename__ = "user_article_states"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ever_starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    starred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dwell_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unstar_dwell_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_opened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    share_token: Mapped[str | None] = mapped_column(String(32), unique=True)
    ai_score: Mapped[float | None] = mapped_column(Float)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_context: Mapped[str | None] = mapped_column(Text)
    ai_filters_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="article_states")
    article: Mapped["Article"] = relationship(back_populates="user_states")


class ArticleAiJob(Base):
    __tablename__ = "article_ai_jobs"
    __table_args__ = (
        CheckConstraint("operation IN ('scoring', 'summary', 'context')", name="ck_article_ai_jobs_operation"),
        CheckConstraint("status IN ('pending', 'success', 'failed', 'skipped')", name="ck_article_ai_jobs_status"),
        UniqueConstraint("article_id", "user_id", "operation", name="uq_article_ai_jobs_article_user_op"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    job_params: Mapped[dict | None] = mapped_column(JSONB)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    article: Mapped["Article"] = relationship()
    user: Mapped["User"] = relationship()


class AiUsageLog(Base):
    """Generic log for non-article AI operations (e.g. preference_generation)."""
    __tablename__ = "ai_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    operation: Mapped[str] = mapped_column(String(50), nullable=False)
    model_slot: Mapped[str | None] = mapped_column(String(10), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

class ArticleAiChat(Base):
    __tablename__ = "article_ai_chats"
    __table_args__ = (
        UniqueConstraint("user_id", "article_id", name="uq_article_ai_chats_user_article"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


class GeneralChatLog(Base):
    __tablename__ = "general_chat_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


from app.models.feed import Feed  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.label import ArticleLabel  # noqa: E402
