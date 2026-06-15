"""Tests for _extract_readable_bg: on-demand readable extraction must complete
the label-deferred AI pipeline, mirroring the batch readable path (#9)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.web.app import _extract_readable_bg


def _session_factory(db):
    """A no-arg callable returning an async context manager yielding `db`."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _db_with_article(article):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = article
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


class TestExtractReadableBg:
    async def test_runs_pipeline_on_success(self):
        article = SimpleNamespace(id=7, feed_id=5, readable_status="pending")
        db = _db_with_article(article)

        def fake_apply(a, content, error, http_status):
            a.readable_status = "success"
            return False

        with (
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.readable_service.extract_readable", return_value=("body text", None, 200)),
            patch("app.routers.web.app.apply_readable_result", side_effect=fake_apply),
            patch("app.services.ai_pipeline_service.run_pipeline_for_article_all_users", new=AsyncMock()) as pipe,
        ):
            await _extract_readable_bg(7, "https://example.com/a", None, None)

        pipe.assert_awaited_once()
        assert pipe.await_args.args[0] is article

    async def test_runs_pipeline_on_terminal_failure(self):
        article = SimpleNamespace(id=7, feed_id=5, readable_status="pending")
        db = _db_with_article(article)

        def fake_apply(a, content, error, http_status):
            a.readable_status = "failed"
            return False

        with (
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.readable_service.extract_readable", return_value=(None, "err", 500)),
            patch("app.routers.web.app.apply_readable_result", side_effect=fake_apply),
            patch("app.services.ai_pipeline_service.run_pipeline_for_article_all_users", new=AsyncMock()) as pipe,
        ):
            await _extract_readable_bg(7, "https://example.com/a", None, None)

        pipe.assert_awaited_once()

    async def test_no_pipeline_when_not_terminal(self):
        article = SimpleNamespace(id=7, feed_id=5, readable_status="pending")
        db = _db_with_article(article)

        def fake_apply(a, content, error, http_status):
            a.readable_status = "skipped"  # non-terminal (e.g. 403 retry-later)
            return False

        with (
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.readable_service.extract_readable", return_value=(None, "403", 403)),
            patch("app.routers.web.app.apply_readable_result", side_effect=fake_apply),
            patch("app.services.ai_pipeline_service.run_pipeline_for_article_all_users", new=AsyncMock()) as pipe,
        ):
            await _extract_readable_bg(7, "https://example.com/a", None, None)

        pipe.assert_not_awaited()
