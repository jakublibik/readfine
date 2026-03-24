from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FetchLog(Base):
    __tablename__ = "fetch_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int] = mapped_column(Integer, ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    feed: Mapped["Feed"] = relationship(back_populates="fetch_logs")


from app.models.feed import Feed  # noqa: E402
