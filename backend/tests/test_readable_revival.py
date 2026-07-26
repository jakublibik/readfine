"""Revival of readable extraction for feeds auto-disabled after repeated 403s.

A feed that answers 403 three times has extraction switched off for every subscriber
and nothing ever switches it back on, so a temporary block (the kind the switch to
HTTP/2 fixed wholesale) became permanent. These tests cover the probe's decision logic
with a mocked DB session, mirroring the project's other readable service tests."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.readable_service import (
    _EMPTY_CONTENT_MSG,
    _REVIVAL_BACKOFF_DAYS,
    _defer_revival,
    _revive_readable_for_feed,
    retry_blocked_feeds,
)


def _feed(**kwargs):
    defaults = dict(
        id=7,
        title="Blocked feed",
        fetch_auth_user=None,
        fetch_auth_pass_encrypted=None,
        readable_revival_next_at=datetime.now(timezone.utc) - timedelta(hours=1),
        readable_revival_attempts=0,
        readable_revived_at=None,
    )
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _db(feeds, *, still_disabled=1, article_url="https://example.com/a", user_feeds=None):
    """Mocked session for retry_blocked_feeds.

    execute() serves the feed query first and then the subscriber query issued by
    _revive_readable_for_feed; scalar() serves the still-disabled count and the probe
    article's URL, in that order.
    """
    db = AsyncMock()
    feeds_result = MagicMock()
    feeds_result.scalars.return_value.all.return_value = feeds
    uf_result = MagicMock()
    uf_result.scalars.return_value.all.return_value = user_feeds or []
    db.execute = AsyncMock(side_effect=[feeds_result, uf_result])
    db.scalar = AsyncMock(side_effect=[still_disabled, article_url])
    db.commit = AsyncMock()
    return db


def _probe(content=None, error=None, http_status=None):
    """Patch the extraction call the probe runs in the executor."""
    return patch(
        "app.services.readable_service.extract_readable",
        MagicMock(return_value=(content, error, http_status, None)),
    )


class TestDeferRevival:
    async def test_schedules_first_attempt(self):
        feed = _feed(readable_revival_attempts=0, readable_revival_next_at=None)
        now = datetime.now(timezone.utc)
        _defer_revival(feed, now)
        assert feed.readable_revival_next_at == now + timedelta(days=_REVIVAL_BACKOFF_DAYS[0])

    async def test_schedules_second_attempt_further_out(self):
        feed = _feed(readable_revival_attempts=1, readable_revival_next_at=None)
        now = datetime.now(timezone.utc)
        _defer_revival(feed, now)
        assert feed.readable_revival_next_at == now + timedelta(days=_REVIVAL_BACKOFF_DAYS[1])

    async def test_stops_once_attempts_are_spent(self):
        feed = _feed(readable_revival_attempts=len(_REVIVAL_BACKOFF_DAYS))
        _defer_revival(feed, datetime.now(timezone.utc))
        assert feed.readable_revival_next_at is None

    async def test_counter_is_cumulative_across_disable_episodes(self):
        """A feed re-disabled after a passing probe must not get a fresh set of tries.

        This is the flapping guard: 403s are usually per-IP, so a probe can pass while
        the feed is still blocked. Extraction comes back, users collect 403s, the feed
        is disabled again — and a reset counter would repeat that every few days.
        """
        feed = _feed(readable_revival_attempts=len(_REVIVAL_BACKOFF_DAYS),
                     readable_revived_at=datetime.now(timezone.utc))
        _defer_revival(feed, datetime.now(timezone.utc))
        assert feed.readable_revival_next_at is None


class TestReviveReadableForFeed:
    async def test_only_revives_subscribers_we_disabled(self):
        ours = MagicMock(extract_readable=False, readable_auto_disabled=True,
                         readable_auto_disabled_reason="blocked")
        feed = _feed(readable_revival_attempts=1)
        db = AsyncMock()
        uf_result = MagicMock()
        uf_result.scalars.return_value.all.return_value = [ours]
        db.execute = AsyncMock(return_value=uf_result)
        db.commit = AsyncMock()

        count = await _revive_readable_for_feed(feed, db)

        assert count == 1
        assert ours.extract_readable is True
        assert ours.readable_auto_disabled is False
        assert ours.readable_auto_disabled_reason is None

    async def test_records_revival_without_resetting_attempts(self):
        feed = _feed(readable_revival_attempts=1)
        db = AsyncMock()
        uf_result = MagicMock()
        uf_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=uf_result)
        db.commit = AsyncMock()

        await _revive_readable_for_feed(feed, db)

        assert feed.readable_revival_next_at is None
        assert feed.readable_revived_at is not None
        assert feed.readable_revival_attempts == 1  # never reset by the job


class TestSchedulingOnDisable:
    async def test_403_disable_schedules_a_probe(self):
        from app.services.readable_service import _disable_readable_for_403

        feed = _feed(readable_revival_next_at=None)
        db = AsyncMock()
        db.get = AsyncMock(return_value=feed)
        with patch(
            "app.services.readable_service._disable_readable_for_feed",
            new=AsyncMock(return_value=2),
        ):
            await _disable_readable_for_403(feed.id, db)

        assert feed.readable_revival_next_at is not None

    async def test_empty_disable_schedules_nothing(self):
        """An empty extraction means the page downloaded fine and held nothing usable.

        Two weeks will not change that, and probing HTTP would pass and revive a feed
        the empty detector immediately disables again.
        """
        from app.services.readable_service import _disable_readable_for_empty

        feed = _feed(readable_revival_next_at=None)
        db = AsyncMock()
        db.get = AsyncMock(return_value=feed)
        with patch(
            "app.services.readable_service._disable_readable_for_feed",
            new=AsyncMock(return_value=2),
        ):
            await _disable_readable_for_empty(feed.id, db)

        assert feed.readable_revival_next_at is None

    async def test_exhausted_feed_is_not_rescheduled_on_re_disable(self):
        """The flapping guard end to end: a spent feed disabled again stays quiet."""
        from app.services.readable_service import _disable_readable_for_403

        feed = _feed(readable_revival_next_at=None,
                     readable_revival_attempts=len(_REVIVAL_BACKOFF_DAYS))
        db = AsyncMock()
        db.get = AsyncMock(return_value=feed)
        with patch(
            "app.services.readable_service._disable_readable_for_feed",
            new=AsyncMock(return_value=1),
        ):
            await _disable_readable_for_403(feed.id, db)

        assert feed.readable_revival_next_at is None


class TestRetryBlockedFeeds:
    async def test_revives_when_page_downloads(self):
        feed = _feed()
        db = _db([feed])
        with _probe(content="<p>hello</p>"):
            revived = await retry_blocked_feeds(db)

        assert revived == 1
        assert feed.readable_revived_at is not None
        assert feed.readable_revival_next_at is None

    async def test_empty_extraction_counts_as_a_pass(self):
        """The probe tests the block, not this article's markup.

        A video post or live blog at the top of the feed extracts to nothing even
        though the page came down fine, and must not condemn the whole feed.
        """
        feed = _feed()
        db = _db([feed])
        with _probe(error=_EMPTY_CONTENT_MSG):
            revived = await retry_blocked_feeds(db)

        assert revived == 1
        assert feed.readable_revived_at is not None

    async def test_403_spends_an_attempt_and_reschedules(self):
        feed = _feed(readable_revival_attempts=0)
        db = _db([feed])
        with _probe(error="HTTP 403 Forbidden", http_status=403):
            revived = await retry_blocked_feeds(db)

        assert revived == 0
        assert feed.readable_revival_attempts == 1
        assert feed.readable_revival_next_at is not None
        assert feed.readable_revived_at is None

    async def test_second_403_stops_probing(self):
        feed = _feed(readable_revival_attempts=len(_REVIVAL_BACKOFF_DAYS) - 1)
        db = _db([feed])
        with _probe(error="HTTP 403 Forbidden", http_status=403):
            await retry_blocked_feeds(db)

        assert feed.readable_revival_attempts == len(_REVIVAL_BACKOFF_DAYS)
        assert feed.readable_revival_next_at is None

    async def test_timeout_is_not_treated_as_a_pass(self):
        """A timeout proves nothing about the block, so it must not revive the feed."""
        feed = _feed()
        db = _db([feed])
        with _probe(error="Timeout after 15s"):
            revived = await retry_blocked_feeds(db)

        assert revived == 0
        assert feed.readable_revived_at is None
        assert feed.readable_revival_attempts == 1

    async def test_probe_does_not_touch_the_article(self):
        feed = _feed()
        db = _db([feed])
        with _probe(content="<p>hello</p>"), patch(
            "app.services.readable_service.apply_readable_result"
        ) as apply:
            await retry_blocked_feeds(db)
        apply.assert_not_called()

    async def test_feed_without_a_probeable_article_is_rescheduled(self):
        """No article means no probe, but next_at must leave the past.

        Left behind, the feed would eat a batch slot every single day.
        """
        feed = _feed()
        db = _db([feed], article_url=None)
        with _probe(content="<p>hello</p>") as extract:
            revived = await retry_blocked_feeds(db)

        assert revived == 0
        extract.assert_not_called()
        assert feed.readable_revival_attempts == 1
        assert feed.readable_revival_next_at is not None
        assert feed.readable_revival_next_at > datetime.now(timezone.utc)

    async def test_unschedules_when_nobody_is_auto_disabled_anymore(self):
        feed = _feed()
        db = _db([feed], still_disabled=0)
        with _probe(content="<p>hello</p>") as extract:
            revived = await retry_blocked_feeds(db)

        assert revived == 0
        extract.assert_not_called()  # no HTTP request for a feed nobody needs revived
        assert feed.readable_revival_next_at is None

    async def test_no_due_feeds_is_a_no_op(self):
        db = _db([])
        with _probe(content="<p>hello</p>") as extract:
            assert await retry_blocked_feeds(db) == 0
        extract.assert_not_called()
