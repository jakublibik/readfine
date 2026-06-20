"""Tests for cross-feed article deduplication logic."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.fetcher.rss import _dedup_cross_feed, dedup_cross_feed_global

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _db_returning(rows):
    """Mock AsyncSession where the first execute() returns .all() == rows."""
    first_result = MagicMock()
    first_result.all.return_value = rows

    call_idx = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        return first_result if idx == 0 else MagicMock()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


def _capturing_db():
    """Mock AsyncSession that captures all statements passed to execute()."""
    stmts = []

    async def _execute(stmt, *args, **kwargs):
        stmts.append(stmt)
        r = MagicMock()
        r.all.return_value = []
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db._captured_stmts = stmts
    return db


# ── dedup_cross_feed_global ───────────────────────────────────────────────────

class TestDedupCrossFeedGlobal:
    async def test_no_duplicates_returns_zero(self):
        db = _db_returning([])
        assert await dedup_cross_feed_global(SINCE, db) == 0

    async def test_no_duplicates_only_one_execute(self):
        db = _db_returning([])
        await dedup_cross_feed_global(SINCE, db)
        assert db.execute.call_count == 1

    async def test_no_duplicates_no_commit(self):
        db = _db_returning([])
        await dedup_cross_feed_global(SINCE, db)
        db.commit.assert_not_called()

    async def test_duplicate_triggers_insert_update_commit(self):
        # 1 unique feed_id → SELECT + INSERT + UPDATE(unread_count) = 3 executes
        rows = [SimpleNamespace(user_id=1, article_id=5, feed_id=10)]
        db = _db_returning(rows)
        await dedup_cross_feed_global(SINCE, db)
        assert db.execute.call_count == 3
        db.commit.assert_called_once()

    async def test_returns_count_of_marked_rows(self):
        rows = [
            SimpleNamespace(user_id=1, article_id=5, feed_id=10),
            SimpleNamespace(user_id=2, article_id=5, feed_id=10),
        ]
        db = _db_returning(rows)
        assert await dedup_cross_feed_global(SINCE, db) == 2

    async def test_two_feeds_execute_two_unread_updates(self):
        rows = [
            SimpleNamespace(user_id=1, article_id=5, feed_id=10),
            SimpleNamespace(user_id=1, article_id=7, feed_id=20),
        ]
        db = _db_returning(rows)
        await dedup_cross_feed_global(SINCE, db)
        # SELECT + INSERT + UPDATE(feed 10) + UPDATE(feed 20) = 4
        assert db.execute.call_count == 4

    async def test_sql_uses_less_than_not_neq(self):
        """Regression: dup_exists subquery must use < so only the higher-ID duplicate
        is marked as read.

        Bug scenario: Feed A and B both publish the same URL in the same scheduler
        round (race condition — neither sees the other during _dedup_cross_feed).
        With !=  both articles satisfy dup_exists → both marked read → user sees neither.
        With <   only the higher-ID article has a lower-ID counterpart → only it is
                 marked read; the lower-ID article (first in DB) stays unread.
        """
        from sqlalchemy.dialects.postgresql import dialect as pg_dialect

        db = _capturing_db()
        await dedup_cross_feed_global(SINCE, db)

        sql = str(db._captured_stmts[0].compile(dialect=pg_dialect()))
        assert " < " in sql, "dup_exists subquery must use < comparison for article id"
        assert "!=" not in sql and "<>" not in sql, (
            "dup_exists must not use != / <> — that would mark both race-condition "
            "duplicates as read"
        )


# ── _dedup_cross_feed ─────────────────────────────────────────────────────────

class TestDedupCrossFeed:
    def _article(self, id: int, url: str | None = "https://example.com/a"):
        return SimpleNamespace(id=id, url_normalized=url)

    async def test_empty_articles_skips_db(self):
        db = AsyncMock()
        await _dedup_cross_feed(10, [], db)
        db.execute.assert_not_called()

    async def test_no_url_articles_skips_db(self):
        db = AsyncMock()
        await _dedup_cross_feed(10, [self._article(1, url=None)], db)
        db.execute.assert_not_called()

    async def test_no_duplicate_rows_no_insert(self):
        db = _db_returning([])
        await _dedup_cross_feed(10, [self._article(5)], db)
        # Only the SELECT was executed
        assert db.execute.call_count == 1

    async def test_duplicate_rows_trigger_insert_and_update(self):
        rows = [SimpleNamespace(user_id=1, article_id=5)]
        db = _db_returning(rows)
        await _dedup_cross_feed(10, [self._article(5)], db)
        # SELECT + INSERT + UPDATE(unread_count) = 3
        assert db.execute.call_count == 3

    async def test_multiple_new_articles_mixed_duplicates(self):
        # Only article 5 is a duplicate; article 6 is not
        rows = [SimpleNamespace(user_id=1, article_id=5)]
        db = _db_returning(rows)
        await _dedup_cross_feed(10, [self._article(5), self._article(6)], db)
        assert db.execute.call_count == 3
