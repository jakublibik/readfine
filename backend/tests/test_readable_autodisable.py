"""Auto-disable of readable extraction for feeds that consistently extract nothing.

Reddit-style feeds whose article pages produce no usable readable content should not
be retried forever: after a streak of empty extractions the feed is disabled and its
feed (RSS) content is shown instead. These tests cover the decision logic with a
mocked DB session (mirroring the project's mock-based service tests)."""
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.readable_service import (
    _maybe_disable_readable_for_empty,
    _CONSECUTIVE_EMPTY_THRESHOLD,
    _EMPTY_CONTENT_MSG,
)


def _db_returning_rows(rows):
    """An AsyncMock DB whose execute(...).all() yields `rows` (status, error tuples)."""
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


class TestMaybeDisableReadableForEmpty:
    async def test_disables_when_all_recent_empty(self):
        rows = [("failed", _EMPTY_CONTENT_MSG)] * _CONSECUTIVE_EMPTY_THRESHOLD
        db = _db_returning_rows(rows)
        with patch(
            "app.services.readable_service._disable_readable_for_empty",
            new=AsyncMock(),
        ) as disable:
            await _maybe_disable_readable_for_empty(5, db)
        disable.assert_awaited_once_with(5, db)

    async def test_no_disable_below_threshold(self):
        rows = [("failed", _EMPTY_CONTENT_MSG)] * (_CONSECUTIVE_EMPTY_THRESHOLD - 1)
        db = _db_returning_rows(rows)
        with patch(
            "app.services.readable_service._disable_readable_for_empty",
            new=AsyncMock(),
        ) as disable:
            await _maybe_disable_readable_for_empty(5, db)
        disable.assert_not_awaited()

    async def test_no_disable_when_a_recent_article_succeeded(self):
        rows = [("failed", _EMPTY_CONTENT_MSG)] * (_CONSECUTIVE_EMPTY_THRESHOLD - 1)
        rows.append(("success", None))
        db = _db_returning_rows(rows)
        with patch(
            "app.services.readable_service._disable_readable_for_empty",
            new=AsyncMock(),
        ) as disable:
            await _maybe_disable_readable_for_empty(5, db)
        disable.assert_not_awaited()

    async def test_no_disable_for_other_failure_reason(self):
        # A different failure (e.g. 403/timeout) is not an empty-extraction streak.
        rows = [("failed", "HTTP 403 Forbidden")] * _CONSECUTIVE_EMPTY_THRESHOLD
        db = _db_returning_rows(rows)
        with patch(
            "app.services.readable_service._disable_readable_for_empty",
            new=AsyncMock(),
        ) as disable:
            await _maybe_disable_readable_for_empty(5, db)
        disable.assert_not_awaited()


class TestDisableReadableForFeed:
    async def test_skips_pending_and_disables_subscribers(self):
        from app.services.readable_service import _disable_readable_for_feed

        uf1 = MagicMock(extract_readable=True, readable_auto_disabled=False)
        uf2 = MagicMock(extract_readable=True, readable_auto_disabled=False)
        art = MagicMock(readable_status="pending", readable_error=None)

        db = AsyncMock()
        uf_result = MagicMock()
        uf_result.scalars.return_value.all.return_value = [uf1, uf2]
        pending_result = MagicMock()
        pending_result.scalars.return_value.all.return_value = [art]
        db.execute = AsyncMock(side_effect=[uf_result, pending_result])
        db.commit = AsyncMock()

        with patch(
            "app.services.ai_pipeline_service.run_pipeline_for_article_all_users",
            new=AsyncMock(),
        ) as pipe:
            cancelled = await _disable_readable_for_feed(
                5, db, pending_error=_EMPTY_CONTENT_MSG
            )

        assert cancelled == 1
        assert uf1.extract_readable is False and uf1.readable_auto_disabled is True
        assert uf2.extract_readable is False and uf2.readable_auto_disabled is True
        assert uf1.readable_auto_disabled_reason == "blocked"
        assert uf2.readable_auto_disabled_reason == "blocked"
        assert art.readable_status == "skipped"
        assert art.readable_error == _EMPTY_CONTENT_MSG
        pipe.assert_awaited_once()

    async def test_returns_none_when_no_active_subscribers(self):
        from app.services.readable_service import _disable_readable_for_feed

        db = AsyncMock()
        uf_result = MagicMock()
        uf_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=uf_result)

        cancelled = await _disable_readable_for_feed(
            5, db, pending_error=_EMPTY_CONTENT_MSG
        )
        assert cancelled is None
