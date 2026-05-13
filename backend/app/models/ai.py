from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base



class UserAiKey(Base):
    """Phase 2 – per-user API keys for AI providers."""
    __tablename__ = "user_ai_keys"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str | None] = mapped_column(String(12))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user: Mapped["User"] = relationship()


from app.models.user import User  # noqa: E402
