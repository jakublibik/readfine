from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Integer, SmallInteger,
    String, Text, ForeignKey, func, CheckConstraint,
)
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
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    share_token: Mapped[str | None] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="article_states")
    article: Mapped["Article"] = relationship(back_populates="user_states")


from app.models.feed import Feed  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.label import ArticleLabel  # noqa: E402
