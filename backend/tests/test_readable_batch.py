"""The scheduled readable batch worker, process_pending_readable.

Two kinds of article arrive in the same batch and must not be handled the same way.
A feed article feeds the per-feed bookkeeping that turns extraction off on a host
that keeps refusing us, and goes on to scoring. A saved-by-URL article has no feed,
so none of that bookkeeping can hold it — pooled under a `None` key, unrelated hosts
would share a 403 streak and could disable a feed that does not exist — and its
post-processing is per-saver with no scoring at all.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.readable_service import ReadableResult, process_pending_readable
from tests.conftest import make_mock_db


def make_article(**kwargs):
    defaults = {
        "id": 10,
        "feed_id": 5,
        "url": "https://example.com/a",
        "title": "T",
        "content": "<p>feed body</p>",
        "readable_content": None,
        "readable_status": "pending",
        "readable_error": None,
        "readable_retries": 0,
        "readable_next_retry_at": None,
        "readable_failed_at": None,
        "summary": None,
        "published_at": None,
        "word_count": None,
        "estimated_read_min": None,
        "trimmed_at": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def batch_db(articles, feed_rows=()):
    """A session whose first execute() yields the batch, the second the feed auth rows.

    Everything after that is the per-article work, which the tests stub out at the
    function boundary rather than at the query.
    """
    db = make_mock_db()
    batch = MagicMock()
    batch.scalars.return_value.all.return_value = list(articles)
    feeds = MagicMock()
    feeds.__iter__ = lambda self: iter(feed_rows)
    db.execute = AsyncMock(side_effect=[batch, feeds] + [MagicMock() for _ in range(20)])
    return db


class _Patched:
    """Stub out everything the loop reaches for, and record what it was asked to do."""

    def __init__(self, feed_result=None, saved_result=None):
        self.feed_result = feed_result or ReadableResult(content="<p>extracted</p>")
        self.saved_result = saved_result or ReadableResult(
            content="<p>extracted</p>", title="Real Title", resolved_url=None,
        )
        self.pipeline_calls = []
        self.finalize_calls = []
        self.saved_extractions = []
        self.feed_extractions = []

    def _extract_readable(self, url, auth_user, auth_pass):
        self.feed_extractions.append(url)
        r = self.feed_result
        return r.content, r.error, r.http_status, r.published_at

    def _extract_with_title(self, url, auth_user, auth_pass, reject_wrong_content=False):
        self.saved_extractions.append((url, reject_wrong_content))
        return self.saved_result

    def __enter__(self):
        self._patches = [
            patch("app.services.readable_service.extract_readable",
                  side_effect=self._extract_readable),
            patch("app.services.readable_service.extract_readable_with_title",
                  side_effect=self._extract_with_title),
            patch("app.services.ai_pipeline_service.run_pipeline_for_article_all_users",
                  new=AsyncMock(side_effect=lambda a, db: self.pipeline_calls.append(a.id))),
            patch("app.services.saved_article_service.finalize_for_all_savers",
                  new=AsyncMock(side_effect=lambda a, db: self.finalize_calls.append(a.id))),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


class TestBatchRoutesByKindOfArticle:
    async def test_a_saved_article_is_not_scored_and_is_finalized_per_saver(self):
        article = make_article(id=7, feed_id=None, content=None)
        db = batch_db([article])
        with _Patched() as p:
            processed = await process_pending_readable(db)

        assert processed == 1
        assert p.finalize_calls == [7], "saved article must run its per-saver pass"
        assert p.pipeline_calls == [], "saved articles are deliberately never scored"
        assert article.readable_status == "success"
        assert article.title == "Real Title", "a feedless article takes the page's title"

    async def test_a_saved_article_gets_the_consent_page_check(self):
        """Only feedless articles opt into it: a feed article tripping the heuristic
        would lose a body it has been showing fine."""
        db = batch_db([make_article(id=7, feed_id=None, content=None)])
        with _Patched() as p:
            await process_pending_readable(db)
        assert p.saved_extractions == [("https://example.com/a", True)]
        assert p.feed_extractions == []

    async def test_a_feed_article_is_scored_and_keeps_its_title(self):
        article = make_article(id=8, feed_id=5, title="From the feed")
        db = batch_db([article])
        with _Patched() as p:
            processed = await process_pending_readable(db)

        assert processed == 1
        assert p.pipeline_calls == [8]
        assert p.finalize_calls == []
        assert article.title == "From the feed"

    async def test_both_kinds_in_one_batch_go_their_own_way(self):
        saved = make_article(id=7, feed_id=None, content=None)
        feed = make_article(id=8, feed_id=5)
        db = batch_db([saved, feed])
        with _Patched() as p:
            processed = await process_pending_readable(db)

        assert processed == 2
        assert p.finalize_calls == [7]
        assert p.pipeline_calls == [8]


class TestBatchFailures:
    async def test_a_crashing_extraction_does_not_take_the_batch_down(self):
        first, second = make_article(id=7), make_article(id=8)
        db = batch_db([first, second])
        with _Patched() as p:
            with patch("app.services.readable_service.extract_readable",
                       side_effect=[RuntimeError("boom"), ("<p>ok</p>", None, None, None)]):
                processed = await process_pending_readable(db)

        assert processed == 2
        assert first.readable_error == "boom"
        assert second.readable_status == "success"

    async def test_a_saved_article_that_failed_still_runs_its_post_processing(self):
        """Filters have to run on a failed save too, or the article sits in Saved
        having never been labelled by a rule that would have caught it."""
        article = make_article(id=7, feed_id=None, content=None)
        db = batch_db([article])
        failed = ReadableResult(error="HTTP 404 Not Found", http_status=404, title="Gone")
        with _Patched(saved_result=failed) as p:
            await process_pending_readable(db)

        assert article.readable_status == "failed"
        assert p.finalize_calls == [7]

    async def test_a_saved_article_still_retrying_is_left_alone(self):
        """A transient error keeps the article 'pending' with a backoff, so it has
        not reached a verdict yet and its filters must not run on a half result."""
        article = make_article(id=7, feed_id=None, content=None)
        db = batch_db([article])
        transient = ReadableResult(error="Timeout after 15s")
        with _Patched(saved_result=transient) as p:
            await process_pending_readable(db)

        assert article.readable_status == "pending"
        assert p.finalize_calls == []


class TestOnDemandExtractionWins:
    async def test_an_article_finished_elsewhere_is_left_untouched(self):
        """The on-demand path may have extracted the article while this batch was
        fetching it; the refresh in the loop is what notices."""
        article = make_article(id=7, feed_id=None, content=None)
        db = batch_db([article])

        async def finished(obj):
            obj.readable_status = "success"
            obj.readable_content = "<p>from the reader</p>"

        db.refresh = AsyncMock(side_effect=finished)
        with _Patched() as p:
            processed = await process_pending_readable(db)

        assert processed == 1
        assert article.readable_content == "<p>from the reader</p>"
        assert p.finalize_calls == [] and p.pipeline_calls == []
