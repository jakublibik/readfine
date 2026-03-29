from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, SmallInteger, String, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional

from app.database import Base


class Filter(Base):
    __tablename__ = "filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    match_operator: Mapped[str] = mapped_column(String(5), nullable=False, default="AND")
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    stop_on_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope_type: Mapped[str] = mapped_column(String(10), nullable=False, default="all")
    scope_feed_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("feeds.id", ondelete="SET NULL"), nullable=True)
    scope_folder_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("folders.id", ondelete="SET NULL"), nullable=True)
    scope_except: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Relationships
    user: Mapped["User"] = relationship(back_populates="filters")
    conditions: Mapped[list["FilterCondition"]] = relationship(back_populates="filter", cascade="all, delete-orphan")
    actions: Mapped[list["FilterAction"]] = relationship(back_populates="filter", cascade="all, delete-orphan")


class FilterCondition(Base):
    __tablename__ = "filter_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filter_id: Mapped[int] = mapped_column(Integer, ForeignKey("filters.id", ondelete="CASCADE"), nullable=False)
    field: Mapped[str] = mapped_column(String(30), nullable=False)
    operator: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    filter: Mapped["Filter"] = relationship(back_populates="conditions")


class FilterAction(Base):
    __tablename__ = "filter_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filter_id: Mapped[int] = mapped_column(Integer, ForeignKey("filters.id", ondelete="CASCADE"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    action_value: Mapped[str | None] = mapped_column(Text)

    filter: Mapped["Filter"] = relationship(back_populates="actions")


from app.models.user import User  # noqa: E402
