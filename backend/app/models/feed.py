from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, SmallInteger, String, Text, ForeignKey, func, CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'error', 'paused', 'disabled')", name="ck_feeds_status"),
        CheckConstraint("feed_type IN ('rss', 'youtube', 'scrape', 'twitter', 'podcast')", name="ck_feeds_feed_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetch_auth_user: Mapped[str | None] = mapped_column(String(255))
    fetch_auth_pass_encrypted: Mapped[str | None] = mapped_column(Text)
    site_url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    favicon_url: Mapped[str | None] = mapped_column(String(2048))
    favicon_data: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    fetch_error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When set, the scheduler skips this feed until this time (honors HTTP 429 Retry-After).
    retry_after_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # HTTP validators for conditional requests: sent back as If-None-Match /
    # If-Modified-Since so an unchanged feed answers 304 Not Modified (no body).
    etag: Mapped[str | None] = mapped_column(String(255))
    last_modified: Mapped[str | None] = mapped_column(String(255))
    last_fetch_duration_ms: Mapped[int | None] = mapped_column(Integer)
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_interval_min: Mapped[int | None] = mapped_column(SmallInteger)
    # Auto (adaptive) interval derived from the feed's publish cadence; recomputed
    # daily (see app.fetcher.interval). NULL = not enough history → falls back to the
    # global default. Stored UNCAPPED and 15-min-quantized; the max cap is applied at
    # read time. Ignored when fetch_interval_min (manual override) is set.
    derived_interval_min: Mapped[int | None] = mapped_column(SmallInteger)
    subscriber_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    feed_type: Mapped[str] = mapped_column(String(20), nullable=False, default="rss")
    type_config: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user_feeds: Mapped[list["UserFeed"]] = relationship(back_populates="feed", cascade="all, delete-orphan")
    articles: Mapped[list["Article"]] = relationship(back_populates="feed")
    fetch_logs: Mapped[list["FetchLog"]] = relationship(back_populates="feed", cascade="all, delete-orphan")


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_folders_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="folders")
    user_feeds: Mapped[list["UserFeed"]] = relationship(back_populates="folder")


class UserFeed(Base):
    __tablename__ = "user_feeds"
    __table_args__ = (UniqueConstraint("user_id", "feed_id", name="uq_user_feeds_user_feed"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    feed_id: Mapped[int] = mapped_column(Integer, ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    folder_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("folders.id", ondelete="SET NULL"))
    custom_title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    extract_readable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    readable_auto_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Why readable was auto-disabled: 'full_content' (feed already has full text) or
    # 'blocked' (site refused/returned no extractable content). None when not disabled.
    readable_auto_disabled_reason: Mapped[str | None] = mapped_column(String(20))
    ai_scoring_enabled: Mapped[bool | None] = mapped_column(Boolean)
    ai_summary_enabled: Mapped[bool | None] = mapped_column(Boolean)
    purge_after_days: Mapped[int | None] = mapped_column(SmallInteger)
    purge_keep_count: Mapped[int | None] = mapped_column(SmallInteger)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="user_feeds")
    feed: Mapped["Feed"] = relationship(back_populates="user_feeds")
    folder: Mapped["Folder | None"] = relationship(back_populates="user_feeds")


from app.models.user import User  # noqa: E402
from app.models.article import Article  # noqa: E402
from app.models.fetch_log import FetchLog  # noqa: E402
