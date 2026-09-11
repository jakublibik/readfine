from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PageViewHourly(Base):
    """Hourly view counts for the public pages (see ``app.services.traffic_service``).

    Views add up, so hourly rows can be folded into days in any timezone at query
    time -- the same thing ``stats_service`` does with reading history. ``path`` only
    ever holds a value from a fixed allowlist, never anything the client sent.
    """

    __tablename__ = "page_view_hourly"

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    path: Mapped[str] = mapped_column(String(40), primary_key=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bot_views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TrafficSourceHourly(Base):
    """Where the hour's human views came from: a referrer host, ``utm:<value>``,
    ``direct``, or ``other`` once the per-hour source cap is reached.

    Bot views are deliberately left out (they almost never send a referrer and would
    pile up in ``direct``), so these counts total less than ``page_view_hourly.views``.
    """

    __tablename__ = "traffic_source_hourly"

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(80), primary_key=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class VisitorDaily(Base):
    """Approximate distinct visitors per day, in the instance owner's timezone.

    Daily, not hourly, because visitors do NOT add up: someone who comes at 09:00 and
    again at 14:00 is one visitor, and summing the two hours would say two. The day a
    visit belongs to therefore has to be decided when it is recorded, which needs one
    fixed timezone.

    CAREFUL: ``path`` mixes two kinds of row. ``'*'`` is the whole instance, anything
    else is a single page, so the two overlap completely. Every query for the total
    must filter ``path = '*'`` and every per-page query ``path <> '*'`` -- a plain
    ``SUM(visitors)`` returns roughly double and looks entirely plausible.

    The same non-additivity applies across days: summing a window's rows counts a
    daily visitor once per day. The admin page shows an average per day instead.
    """

    __tablename__ = "visitor_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    path: Mapped[str] = mapped_column(String(40), primary_key=True)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
