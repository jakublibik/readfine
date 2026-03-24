from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, SmallInteger, String, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#6366f1")
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="labels")
    article_labels: Mapped[list["ArticleLabel"]] = relationship(back_populates="label", cascade="all, delete-orphan")


class ArticleLabel(Base):
    __tablename__ = "article_labels"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True)
    label_id: Mapped[int] = mapped_column(Integer, ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    assigned_by_filter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    user: Mapped["User"] = relationship()
    article: Mapped["Article"] = relationship(back_populates="article_labels")
    label: Mapped["Label"] = relationship(back_populates="article_labels")


from app.models.user import User  # noqa: E402
from app.models.article import Article  # noqa: E402
