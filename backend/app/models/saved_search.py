from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SavedSearch(Base):
    """A search the reader named and kept: a live query, run again on every open.
    Every saved search is listed in the sidebar and opens there like a feed.

    ``params`` holds the search's knobs under the keys its query string uses, already
    cleaned by `search_params.normalize_search_params`. The multi-select scopes
    (``scope_include``, ``label_filter``) are stored as JSON lists rather than the
    JSON strings the query string carries, so cleanup can match them with ``@>`` and
    an exclude list can be added later as one more key, without a migration.

    Names are unique per user regardless of case: two entries called "Tech" in the
    sidebar could only be told apart by opening them. That index leads with user_id,
    so it serves the per-user lookups too and there is no index of user_id alone.
    """

    __tablename__ = "saved_searches"
    __table_args__ = (
        Index("uq_saved_searches_user_name", "user_id", text("lower(name)"), unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
