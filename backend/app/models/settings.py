from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, SmallInteger, String, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    registration_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_fetch_interval_min: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=60)
    min_fetch_interval_min: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=15)
    max_feeds_per_user: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=200)
    default_purge_after_days: Mapped[int | None] = mapped_column(SmallInteger, default=60)
    default_purge_keep_count: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(SmallInteger, default=587)
    smtp_user: Mapped[str | None] = mapped_column(String(255))
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text)
    smtp_from_email: Mapped[str | None] = mapped_column(String(255))
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    legal_operator_name: Mapped[str | None] = mapped_column(String(255))
    legal_contact_email: Mapped[str | None] = mapped_column(String(255))
    legal_jurisdiction: Mapped[str | None] = mapped_column(String(100))
    legal_last_updated: Mapped[str | None] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    admin_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(30))
    target_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    admin: Mapped["User"] = relationship(back_populates="audit_logs", foreign_keys=[admin_id])


from app.models.user import User  # noqa: E402
