from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AiProfile(Base):
    """Phase 2 – created in DB from Phase 1, ignored by app until AI is enabled."""
    __tablename__ = "ai_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    max_tokens: Mapped[int | None] = mapped_column(Integer, default=1000)
    summary_language: Mapped[str] = mapped_column(String(10), default="cs")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserAiKey(Base):
    """Phase 2 – per-user API keys for AI providers."""
    __tablename__ = "user_ai_keys"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user: Mapped["User"] = relationship()


from app.models.user import User  # noqa: E402
