"""Auto-summary on star: the summary must only be produced when the user has
enabled auto-summary of starred articles, must fire immediately after a short
debounce, and a quick unstar (mis-click) must cancel the pending job."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse
from sqlalchemy import Delete

from app.routers.web.app.articles import _summary_after_star_bg


def _session_factory(db):
    """A no-arg callable returning an async context manager yielding `db`."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _bg_db(state, article):
    db = AsyncMock()
    db.scalar = AsyncMock(side_effect=[state, article])
    db.commit = AsyncMock()
    return db


# ── Debounce background task: only summarizes a still-starred article ──────────

class TestSummaryAfterStarBg:
    async def test_processes_when_still_starred(self):
        state = SimpleNamespace(is_starred=True, ai_summary=None)
        article = SimpleNamespace(id=7)
        db = _bg_db(state, article)
        with (
            patch("app.routers.web.app.articles._STAR_SUMMARY_DEBOUNCE_S", 0),
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.ai_pipeline_service._run_summary_now", new=AsyncMock()) as run_now,
        ):
            await _summary_after_star_bg(7, 1)
        run_now.assert_awaited_once()
        assert run_now.await_args.args[0] is article
        db.commit.assert_awaited_once()

    async def test_skips_when_unstarred_during_debounce(self):
        state = SimpleNamespace(is_starred=False, ai_summary=None)
        db = _bg_db(state, None)
        with (
            patch("app.routers.web.app.articles._STAR_SUMMARY_DEBOUNCE_S", 0),
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.ai_pipeline_service._run_summary_now", new=AsyncMock()) as run_now,
        ):
            await _summary_after_star_bg(7, 1)
        run_now.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_skips_when_already_summarized(self):
        state = SimpleNamespace(is_starred=True, ai_summary="already here")
        db = _bg_db(state, None)
        with (
            patch("app.routers.web.app.articles._STAR_SUMMARY_DEBOUNCE_S", 0),
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.ai_pipeline_service._run_summary_now", new=AsyncMock()) as run_now,
        ):
            await _summary_after_star_bg(7, 1)
        run_now.assert_not_awaited()

    async def test_skips_when_state_missing(self):
        db = _bg_db(None, None)
        with (
            patch("app.routers.web.app.articles._STAR_SUMMARY_DEBOUNCE_S", 0),
            patch("app.database.async_session_factory", _session_factory(db)),
            patch("app.services.ai_pipeline_service._run_summary_now", new=AsyncMock()) as run_now,
        ):
            await _summary_after_star_bg(7, 1)
        run_now.assert_not_awaited()


# ── Star route: gating + cancel-on-unstar ─────────────────────────────────────

def _noop_coro_factory(recorder):
    async def _noop():
        return None

    def _fake(article_id, user_id):
        recorder.append((article_id, user_id))
        return _noop()

    return _fake


class TestStarRouteSummaryGating:
    def test_star_without_auto_summary_does_not_enqueue(self, client, mock_db):
        """Auto-summary disabled → no job enqueued, no immediate processing scheduled."""
        scheduled: list = []
        mock_db.scalar = AsyncMock(side_effect=[
            SimpleNamespace(ai_summary_enabled_default=False),
        ])
        enqueue = AsyncMock(return_value=True)
        with (
            patch("app.routers.web.app.articles.toggle_article_state",
                  new=AsyncMock(return_value=SimpleNamespace(is_starred=True))),
            patch("app.routers.web.app.articles._star_response", return_value=HTMLResponse("ok")),
            patch("app.routers.web.app.articles._summary_after_star_bg",
                  new=_noop_coro_factory(scheduled)),
            patch("app.services.ai_summary_service.enqueue_summary_job", new=enqueue),
        ):
            resp = client.post("/htmx/articles/5/star")
        assert resp.status_code == 200
        enqueue.assert_not_awaited()
        assert scheduled == []

    def test_star_with_auto_summary_enqueues_and_schedules(self, client, mock_db):
        """Auto-summary enabled → job enqueued and immediate processing scheduled."""
        scheduled: list = []
        mock_db.scalar = AsyncMock(side_effect=[
            SimpleNamespace(ai_summary_enabled_default=True),  # settings
            SimpleNamespace(id=5),                              # article_obj
        ])
        enqueue = AsyncMock(return_value=True)
        with (
            patch("app.routers.web.app.articles.toggle_article_state",
                  new=AsyncMock(return_value=SimpleNamespace(is_starred=True))),
            patch("app.routers.web.app.articles._star_response", return_value=HTMLResponse("ok")),
            patch("app.routers.web.app.articles._summary_after_star_bg",
                  new=_noop_coro_factory(scheduled)),
            patch("app.services.ai_summary_service.enqueue_summary_job", new=enqueue),
        ):
            resp = client.post("/htmx/articles/5/star")
        assert resp.status_code == 200
        enqueue.assert_awaited_once()
        assert scheduled == [(5, 1)]

    def test_star_enabled_but_enqueue_ineligible_does_not_schedule(self, client, mock_db):
        """Enabled but article ineligible (e.g. too short) → enqueue returns False,
        so nothing is scheduled."""
        scheduled: list = []
        mock_db.scalar = AsyncMock(side_effect=[
            SimpleNamespace(ai_summary_enabled_default=True),
            SimpleNamespace(id=5),
        ])
        enqueue = AsyncMock(return_value=False)
        with (
            patch("app.routers.web.app.articles.toggle_article_state",
                  new=AsyncMock(return_value=SimpleNamespace(is_starred=True))),
            patch("app.routers.web.app.articles._star_response", return_value=HTMLResponse("ok")),
            patch("app.routers.web.app.articles._summary_after_star_bg",
                  new=_noop_coro_factory(scheduled)),
            patch("app.services.ai_summary_service.enqueue_summary_job", new=enqueue),
        ):
            resp = client.post("/htmx/articles/5/star")
        assert resp.status_code == 200
        enqueue.assert_awaited_once()
        assert scheduled == []

    def test_enqueued_summary_signals_the_open_article(self, client, mock_db):
        """The response must carry summaryStarted so an open article can show the
        "Generating summary…" spinner for a summary that runs in the background."""
        scheduled: list = []
        mock_db.scalar = AsyncMock(side_effect=[
            SimpleNamespace(ai_summary_enabled_default=True),
            SimpleNamespace(id=5),
        ])
        with (
            patch("app.routers.web.app.articles.toggle_article_state",
                  new=AsyncMock(return_value=SimpleNamespace(id=5, is_starred=True))),
            patch("app.routers.web.app.articles._summary_after_star_bg",
                  new=_noop_coro_factory(scheduled)),
            patch("app.services.ai_summary_service.enqueue_summary_job",
                  new=AsyncMock(return_value=True)),
        ):
            resp = client.post("/htmx/articles/5/star")
        assert resp.status_code == 200
        assert json.loads(resp.headers["HX-Trigger"])["summaryStarted"] == {"id": 5}

    def test_no_summary_signal_when_nothing_enqueued(self, client, mock_db):
        mock_db.scalar = AsyncMock(side_effect=[
            SimpleNamespace(ai_summary_enabled_default=True),
            SimpleNamespace(id=5),
        ])
        with (
            patch("app.routers.web.app.articles.toggle_article_state",
                  new=AsyncMock(return_value=SimpleNamespace(id=5, is_starred=True))),
            patch("app.services.ai_summary_service.enqueue_summary_job",
                  new=AsyncMock(return_value=False)),
        ):
            resp = client.post("/htmx/articles/5/star")
        assert resp.status_code == 200
        assert "summaryStarted" not in json.loads(resp.headers["HX-Trigger"])

    def test_unstar_cancels_pending_summary_job(self, client, mock_db):
        """Unstarring deletes a not-yet-run summary job so a mis-click bills nothing."""
        mock_db.execute = AsyncMock()
        with (
            patch("app.routers.web.app.articles.toggle_article_state",
                  new=AsyncMock(return_value=SimpleNamespace(is_starred=False))),
            patch("app.routers.web.app.articles._star_response", return_value=HTMLResponse("ok")),
        ):
            resp = client.post("/htmx/articles/5/star")
        assert resp.status_code == 200
        mock_db.execute.assert_awaited_once()
        stmt = mock_db.execute.await_args.args[0]
        assert isinstance(stmt, Delete)
        assert stmt.table.name == "article_ai_jobs"
