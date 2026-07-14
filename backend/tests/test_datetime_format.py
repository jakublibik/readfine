"""Unit tests for server-side date formatting and timezone helpers."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.utils.datetime_format import (
    format_local,
    format_until,
    is_valid_timezone,
    available_timezone_list,
    timezone_groups,
)

# Fixed reference "now" so relative formatting is deterministic.
NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


class TestFormatUntil:
    def _at(self, **delta):
        from datetime import timedelta
        return format_until(NOW + timedelta(**delta), now=NOW)

    def test_none_returns_none(self):
        assert format_until(None, now=NOW) is None

    def test_past_is_due(self):
        assert self._at(minutes=-5) == "due"
        assert self._at(seconds=0) == "due"

    def test_minutes(self):
        assert self._at(minutes=8) == "~8m"
        assert self._at(minutes=59) == "~59m"

    def test_hours(self):
        assert self._at(minutes=60) == "~1h"
        assert self._at(minutes=90) == "~2h"   # rounds to nearest hour
        assert self._at(hours=23) == "~23h"

    def test_days(self):
        assert self._at(hours=24) == "~1d"
        assert self._at(days=2) == "~2d"

    def test_tz_independent(self):
        # A tz-aware target in a non-UTC zone still yields the same delta label.
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        dt = (NOW + timedelta(hours=2)).astimezone(ZoneInfo("Asia/Tokyo"))
        assert format_until(dt, now=NOW) == "~2h"


class TestFormatLocalShort:
    def test_today_shows_time_only(self):
        dt = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
        # Prague is UTC+2 in June → 16:30, same calendar day → time only
        assert format_local(dt, "Europe/Prague", "short", now=NOW) == "16:30"

    def test_this_year_other_day(self):
        dt = datetime(2026, 3, 5, 9, 15, tzinfo=timezone.utc)
        # Prague UTC+1 in March → 10:15
        assert format_local(dt, "Europe/Prague", "short", now=NOW) == "05.03. 10:15"

    def test_older_year_includes_year(self):
        dt = datetime(2020, 1, 5, 9, 0, tzinfo=timezone.utc)
        assert format_local(dt, "America/New_York", "short", now=NOW) == "05.01.2020 04:00"


class TestFormatLocalOtherFormats:
    def test_date_format(self):
        dt = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
        assert format_local(dt, "Europe/Prague", "date", now=NOW) == "Jun 2, 2026"

    def test_long_format(self):
        dt = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
        assert format_local(dt, "Europe/Prague", "long", now=NOW) == "2. 6. 2026 16:30"


class TestFormatLocalEdgeCases:
    def test_none_returns_empty(self):
        assert format_local(None, "Europe/Prague", "short") == ""

    def test_naive_datetime_assumed_utc(self):
        naive = datetime(2026, 6, 2, 14, 30)  # no tzinfo
        aware = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
        assert format_local(naive, "Europe/Prague", "long", now=NOW) == \
            format_local(aware, "Europe/Prague", "long", now=NOW)

    def test_invalid_timezone_falls_back_to_utc(self):
        dt = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
        assert format_local(dt, "Not/AZone", "short", now=NOW) == "14:30"

    def test_empty_timezone_falls_back_to_utc(self):
        dt = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)
        assert format_local(dt, "", "short", now=NOW) == "14:30"


class TestTimezoneHelpers:
    def test_is_valid_timezone(self):
        assert is_valid_timezone("Europe/Prague")
        assert is_valid_timezone("UTC")
        assert not is_valid_timezone("Mars/Olympus")
        assert not is_valid_timezone("")
        assert not is_valid_timezone(None)

    def test_available_list_nonempty_and_sorted(self):
        tzs = available_timezone_list()
        assert "Europe/Prague" in tzs
        assert tzs == sorted(tzs)

    def test_groups_contain_region(self):
        groups = dict(timezone_groups())
        assert "Europe" in groups
        assert "Europe/Prague" in groups["Europe"]


class TestRescheduleBriefings:
    @pytest.mark.asyncio
    async def test_recomputes_active_briefings(self):
        from app.routers.web.settings import _reschedule_briefings
        from tests.conftest import make_mock_db, make_scalar_result

        active = SimpleNamespace(
            briefing_interval="daily", briefing_day=None,
            briefing_time="08:00", briefing_next_send_at=None,
        )
        # missing time → skipped, stays None
        incomplete = SimpleNamespace(
            briefing_interval="daily", briefing_day=None,
            briefing_time=None, briefing_next_send_at=None,
        )
        db = make_mock_db()
        db.execute.return_value = make_scalar_result([active, incomplete])

        await _reschedule_briefings(user_id=1, tz_str="Europe/Prague", db=db)

        assert active.briefing_next_send_at is not None
        assert active.briefing_next_send_at > datetime.now(timezone.utc)
        assert incomplete.briefing_next_send_at is None
