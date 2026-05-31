from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, Integer, SmallInteger, String, Text, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_reset_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verification_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    settings: Mapped["UserSettings"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    folders: Mapped[list["Folder"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    user_feeds: Mapped[list["UserFeed"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    labels: Mapped[list["Label"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    filters: Mapped[list["Filter"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    api_tokens: Mapped[list["ApiToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    article_states: Mapped[list["UserArticleState"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="admin", foreign_keys="AuditLog.admin_id")
    catchup_configs: Mapped[list["UserCatchupConfig"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    catchup_logs: Mapped[list["CatchupLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    list_density_web: Mapped[str] = mapped_column(String(20), default="comfortable")
    list_density_mobile: Mapped[str] = mapped_column(String(20), default="comfortable")
    mark_read_on_scroll: Mapped[bool] = mapped_column(Boolean, default=True)
    unread_filter: Mapped[str] = mapped_column(String(20), default="adaptive")
    default_sort_order: Mapped[str] = mapped_column(String(10), default="newest")
    articles_per_page: Mapped[int] = mapped_column(SmallInteger, default=50)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    language: Mapped[str] = mapped_column(String(10), default="en")
    keyboard_shortcuts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    label_display: Mapped[str] = mapped_column(String(20), default="indicator")
    bucket_small_max: Mapped[int] = mapped_column(SmallInteger, default=640)
    bucket_medium_max: Mapped[int] = mapped_column(SmallInteger, default=1100)
    reading_font_size: Mapped[str] = mapped_column(String(10), nullable=False, default="md")
    reading_font_family: Mapped[str] = mapped_column(String(10), nullable=False, default="sans")

    # AI settings
    ai_fast_provider: Mapped[str | None] = mapped_column(String(30))
    ai_fast_model: Mapped[str | None] = mapped_column(String(100))
    ai_quality_provider: Mapped[str | None] = mapped_column(String(30))
    ai_quality_model: Mapped[str | None] = mapped_column(String(100))
    ai_preference_text: Mapped[str | None] = mapped_column(Text)
    ai_scoring_enabled_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_summary_enabled_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_summary_prompt: Mapped[str | None] = mapped_column(Text)
    ai_context_prompt: Mapped[str | None] = mapped_column(Text)
    ai_score_show_in_list: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_chat_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_content_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20_000)
    last_ai_error: Mapped[str | None] = mapped_column(Text)
    last_ai_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    user: Mapped["User"] = relationship(back_populates="settings")


class UserCatchupConfig(Base):
    __tablename__ = "user_catchup_configs"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_catchup_configs_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scope_include: Mapped[str | None] = mapped_column(Text, nullable=True)
    period: Mapped[str] = mapped_column(String(20), nullable=False, default="7days")
    filter_status: Mapped[str] = mapped_column(String(20), nullable=False, default="all")
    filter_labeled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filter_score_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    article_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    model_slot: Mapped[str] = mapped_column(String(10), nullable=False, default="fast")
    custom_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    include_snippet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Briefing (scheduled email digest)
    briefing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    briefing_interval: Mapped[str | None] = mapped_column(String(10), nullable=True)   # 'daily' | 'weekly'
    briefing_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)       # 0=Mon … 6=Sun
    briefing_time: Mapped[str | None] = mapped_column(String(5), nullable=True)         # 'HH:MM' user tz
    briefing_recipients: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON array of extra emails
    briefing_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    briefing_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    briefing_retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    briefing_next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="catchup_configs")
    logs: Mapped[list["CatchupLog"]] = relationship(back_populates="config", cascade="all, delete-orphan")


class CatchupLog(Base):
    __tablename__ = "catchup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    config_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("user_catchup_configs.id", ondelete="SET NULL"), nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_slot: Mapped[str] = mapped_column(String(10), nullable=False, default="fast")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="catchup_logs")
    config: Mapped["UserCatchupConfig | None"] = relationship(back_populates="logs")


# Import here to avoid circular imports
from app.models.auth import ApiToken  # noqa: E402
from app.models.feed import Folder, UserFeed  # noqa: E402
from app.models.label import Label  # noqa: E402
from app.models.filter import Filter  # noqa: E402
from app.models.article import UserArticleState  # noqa: E402
from app.models.settings import AuditLog  # noqa: E402
