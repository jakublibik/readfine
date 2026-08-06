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
    _FULL_CONTENT_SAMPLE,
    maybe_disable_readable_for_feed,
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


class TestMaybeDisableReadableForFullContent:
    """The other auto-disable: a feed that already delivers whole articles itself.

    The measurement has to come from the feed body (Article.content). Reading
    Article.word_count instead made a working extraction disable itself, because a
    successful extraction overwrites that column with the extracted page's count."""

    @staticmethod
    def _body(words: int) -> str:
        return "<p>" + " ".join(["slovo"] * words) + "</p>"

    @staticmethod
    def _db_with_bodies(bodies, uf=None):
        """DB answering the queries in order: subscribers, (content,) rows, pending."""
        db = AsyncMock()
        uf_result = MagicMock()
        uf_result.scalars.return_value.all.return_value = list(uf or [])
        rows = [(b,) for b in bodies]
        pending_result = MagicMock()
        pending_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(side_effect=[uf_result, rows, pending_result])
        db.commit = AsyncMock()
        return db

    async def test_feed_nobody_extracts_reads_no_bodies(self):
        # The check runs on every fetch with new articles. With extraction already off
        # there is nothing to decide, and it must not pay for the article bodies —
        # which are at their largest on exactly these feeds.
        db = self._db_with_bodies([self._body(600)] * _FULL_CONTENT_SAMPLE, uf=[])

        assert await maybe_disable_readable_for_feed(5, db) is False
        assert db.execute.await_count == 1  # subscribers only, no sample

    async def test_truncated_feed_with_working_extraction_stays_enabled(self):
        # The regression: every article extracted fine (word_count would be in the
        # thousands), but the feed itself ships a 30-word teaser. Extraction must stay on.
        uf = MagicMock(extract_readable=True, readable_auto_disabled=False)
        db = self._db_with_bodies([self._body(30)] * _FULL_CONTENT_SAMPLE, uf=[uf])

        assert await maybe_disable_readable_for_feed(5, db) is False
        assert uf.extract_readable is True
        assert uf.readable_auto_disabled is False

    async def test_full_content_feed_is_disabled(self):
        uf1 = MagicMock(extract_readable=True, readable_auto_disabled=False)
        uf2 = MagicMock(extract_readable=True, readable_auto_disabled=False)
        db = self._db_with_bodies([self._body(600)] * _FULL_CONTENT_SAMPLE, uf=[uf1, uf2])

        assert await maybe_disable_readable_for_feed(5, db) is True
        for uf in (uf1, uf2):
            assert uf.extract_readable is False
            assert uf.readable_auto_disabled is True
            assert uf.readable_auto_disabled_reason == "full_content"

    async def test_mixed_feed_below_threshold_stays_enabled(self):
        # 7 of 10 long is under the 0.8 threshold.
        bodies = [self._body(600)] * 7 + [self._body(40)] * 3
        uf = MagicMock(extract_readable=True)
        db = self._db_with_bodies(bodies, uf=[uf])

        assert await maybe_disable_readable_for_feed(5, db) is False
        assert uf.extract_readable is True

    async def test_not_enough_articles_yet(self):
        bodies = [self._body(600)] * (_FULL_CONTENT_SAMPLE - 1)
        uf = MagicMock(extract_readable=True)
        db = self._db_with_bodies(bodies, uf=[uf])

        assert await maybe_disable_readable_for_feed(5, db) is False
        assert uf.extract_readable is True

    async def test_pending_articles_are_skipped_when_disabling(self):
        uf = MagicMock(extract_readable=True)
        art = MagicMock(readable_status="pending")
        db = AsyncMock()
        rows = [(self._body(600),)] * _FULL_CONTENT_SAMPLE
        uf_result = MagicMock()
        uf_result.scalars.return_value.all.return_value = [uf]
        pending_result = MagicMock()
        pending_result.scalars.return_value.all.return_value = [art]
        db.execute = AsyncMock(side_effect=[uf_result, rows, pending_result])
        db.commit = AsyncMock()

        assert await maybe_disable_readable_for_feed(5, db) is True
        assert art.readable_status == "skipped"


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
