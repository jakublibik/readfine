from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostRateLimit(Base):
    """Persisted learned per-host fetch spacing (see ``app.fetcher.host_throttle``).

    Keyed by host, not by feed: many feeds share a host, and the spacing is a
    property of the host's rate limit. Survives restarts so an aggressive host
    (Reddit) isn't re-probed into a 429 after every deploy. ``source`` records how
    the value was set (``200`` = precise from RateLimit headers, ``429`` = ratcheted,
    ``manual`` = admin override).
    """

    __tablename__ = "host_rate_limits"

    host: Mapped[str] = mapped_column(String(255), primary_key=True)
    spacing_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    consecutive_429: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
