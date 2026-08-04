"""Tests for AI pipeline: enqueue logic, pipeline orchestration, and on-demand processing."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_article(**kwargs):
    defaults = {
        "id": 10,
        "feed_id": 5,
        "title": "Test Article",
        "content": "Some content " * 100,
        "readable_content": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_settings(**kwargs):
    defaults = {
        "user_id": 1,
        "ai_scoring_enabled_default": True,
        "ai_summary_enabled_default": True,
        "ai_preference_text": "Technology and science news",
        "ai_fast_provider": "anthropic",
        "ai_fast_model": "claude-haiku-4-5",
        "ai_quality_provider": "anthropic",
        "ai_quality_model": "claude-sonnet-4-6",
        "ai_content_limit": 20_000,
        "ai_summary_prompt": None,
        "last_ai_error": None,
        "last_ai_error_at": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_user_feed(**kwargs):
    defaults = {
        "user_id": 1,
        "feed_id": 5,
        "ai_scoring_enabled": None,
        "ai_summary_enabled": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_job(**kwargs):
    defaults = {
        "id": 1,
        "article_id": 10,
        "user_id": 1,
        "operation": "scoring",
        "status": "pending",
        "retry_count": 0,
        "next_retry_at": None,
        "processed_at": None,
        "error_message": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_state(**kwargs):
    defaults = {
        "user_id": 1,
        "article_id": 10,
        "ai_score": None,
        "ai_filters_applied": False,
        "ai_summary": None,
        "ai_summary_truncated": False,
        "is_starred": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


def make_execute_result(rows=None, rowcount=1):
    """Mock for db.execute() — supports .scalars().all() and .rowcount."""
    result = MagicMock()
    result.rowcount = rowcount
    result.scalars.return_value.all.return_value = rows or []
    return result


# ── enqueue_scoring_job ───────────────────────────────────────────────────────

class TestEnqueueScoringJob:
    async def test_returns_false_when_ai_globally_disabled(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=False)  # ai_enabled = False
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_no_user_settings(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[True, None])  # ai_enabled, no settings
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_scoring_disabled_globally(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_scoring_enabled_default=False),
        ])
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_per_feed_scoring_disabled(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(ai_scoring_enabled=False),
        ])
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_no_preference_text(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_preference_text=""),
            make_user_feed(),
        ])
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_no_fast_provider(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_fast_provider=None),
            make_user_feed(),
        ])
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_job_already_exists(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(),
            42,  # existing job id
        ])
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False

    async def test_returns_true_when_job_created(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(),
            None,  # no existing job
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is True

    async def test_returns_false_on_conflict(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(),
            None,
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=0))
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False


# ── enqueue_summary_job ───────────────────────────────────────────────────────

class TestEnqueueSummaryJob:
    async def test_returns_false_when_ai_globally_disabled(self):
        from app.services.ai_summary_service import enqueue_summary_job
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=False)
        assert await enqueue_summary_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_no_quality_provider(self):
        from app.services.ai_summary_service import enqueue_summary_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_quality_provider=None),
        ])
        assert await enqueue_summary_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_per_feed_summary_disabled(self):
        from app.services.ai_summary_service import enqueue_summary_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(ai_summary_enabled=False),
        ])
        assert await enqueue_summary_job(make_article(), user_id=1, db=db) is False

    async def test_not_blocked_by_summary_default_disabled(self):
        """enqueue_summary_job does NOT check ai_summary_enabled_default — that's for auto pipeline only."""
        from app.services.ai_summary_service import enqueue_summary_job
        long_content = "word " * 500
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_summary_enabled_default=False),  # disabled globally
            make_user_feed(ai_summary_enabled=None),           # no per-feed override either
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
        # Should still return True — on-demand is not gated by ai_summary_enabled_default
        assert await enqueue_summary_job(make_article(content=long_content), user_id=1, db=db) is True

    async def test_returns_false_when_content_too_short(self):
        from app.services.ai_summary_service import enqueue_summary_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(),
        ])
        assert await enqueue_summary_job(make_article(content="short"), user_id=1, db=db) is False

    async def test_returns_true_when_eligible(self):
        from app.services.ai_summary_service import enqueue_summary_job
        long_content = "word " * 500
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(),
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
        assert await enqueue_summary_job(make_article(content=long_content), user_id=1, db=db) is True


# ── run_article_pipeline ──────────────────────────────────────────────────────

class TestRunArticlePipeline:
    """Test pipeline orchestration logic by patching the helper functions."""

    async def test_stops_when_scoring_ineligible(self):
        """enqueue_scoring_job=False + no existing job → pipeline stops, neither filters nor summary run."""
        article = make_article()
        db = make_mock_db()
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=False)),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value=None)),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()) as mock_filters,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_filters.assert_not_called()
            mock_summary.assert_not_called()

    async def test_continues_when_already_scored(self):
        """enqueue_scoring_job=False but job is 'success' → skip scoring, run filters."""
        article = make_article()
        db = make_mock_db()
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=False)),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()) as mock_scoring,
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()) as mock_filters,
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=False)),
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_scoring.assert_not_called()
            mock_filters.assert_called_once()

    async def test_stops_when_scoring_fails(self):
        """Newly enqueued job fails → AI filters not called."""
        article = make_article()
        db = make_mock_db()
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="failed")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()) as mock_filters,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_filters.assert_not_called()

    async def test_full_pipeline_scoring_success(self):
        """Scoring succeeds → AI filters → summary all called."""
        article = make_article()
        db = make_mock_db()
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()) as mock_filters,
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_filters.assert_called_once_with(article, 1, db)
            mock_summary.assert_called_once_with(article, 1, db)

    async def test_skips_summary_when_not_eligible(self):
        """Summary not enqueued (ineligible) → _run_summary_now not called."""
        article = make_article()
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            make_settings(ai_summary_enabled_default=True),  # UserSettings
            make_state(is_starred=True),                     # UserArticleState
        ])
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=False)),
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_summary.assert_not_called()

    async def test_auto_summary_skipped_when_default_disabled(self):
        """ai_summary_enabled_default=False → auto-summary never triggered, even if enqueue would succeed."""
        article = make_article()
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=make_settings(ai_summary_enabled_default=False))
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)) as mock_enqueue,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_enqueue.assert_not_called()
            mock_summary.assert_not_called()

    async def test_auto_summary_runs_when_default_enabled(self):
        """ai_summary_enabled_default=True AND article starred → auto-summary triggered after scoring."""
        article = make_article()
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            make_settings(ai_summary_enabled_default=True),  # UserSettings
            make_state(is_starred=True),                     # UserArticleState
        ])
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_summary.assert_called_once_with(article, 1, db)


# ── process_pending_scoring pipeline continuation ─────────────────────────────

class TestProcessPendingScoringContinuation:
    """Verify that after scoring, AI filters + summary run inline."""

    async def test_pipeline_continuation_on_success(self):
        """After job.status='success', _run_ai_filters_now is called; _run_summary_now not called when summary ineligible."""
        job = make_job()
        article = make_article()
        settings = make_settings()
        db = make_mock_db()

        async def fake_execute_scoring(j, a, s, d, now):
            j.status = "success"

        # Mock db.scalars so pre-load maps return our objects (articles, settings, states)
        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], [settings], []])
        db.scalars = AsyncMock(return_value=scalars_mock)

        # First execute call returns jobs; no other execute calls expected
        call_count = 0

        async def smart_execute(query):
            nonlocal call_count
            call_count += 1
            return make_execute_result(rows=[job] if call_count == 1 else [])

        db.execute = AsyncMock(side_effect=smart_execute)
        db.scalar = AsyncMock(return_value=True)  # ai_enabled check

        with (
            patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=fake_execute_scoring),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()) as mock_filters,
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=False)),
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

            mock_filters.assert_called_once_with(article, job.user_id, db)
            mock_summary.assert_not_called()  # enqueue_summary_job returned False

    async def test_no_auto_summary_when_default_disabled(self):
        """ai_summary_enabled_default=False → summary not triggered after scoring, even if enqueue would succeed."""
        job = make_job()
        article = make_article()
        settings = make_settings(ai_summary_enabled_default=False)

        async def fake_execute_scoring(j, a, s, d, now):
            j.status = "success"

        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)  # ai_enabled check; settings loaded via scalars_mock

        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], [settings], []])
        db.scalars = AsyncMock(return_value=scalars_mock)

        call_count = 0

        async def smart_execute(query):
            nonlocal call_count
            call_count += 1
            return make_execute_result(rows=[job] if call_count == 1 else [])

        db.execute = AsyncMock(side_effect=smart_execute)

        with (
            patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=fake_execute_scoring),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)) as mock_enqueue,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

        mock_enqueue.assert_not_called()
        mock_summary.assert_not_called()

    async def test_no_pipeline_continuation_on_failure(self):
        """After scoring failure, AI filters must NOT be called."""
        job = make_job()
        article = make_article()
        settings = make_settings()

        async def fake_execute_scoring_fail(j, a, s, d, now):
            j.status = "failed"

        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)

        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], [settings], []])
        db.scalars = AsyncMock(return_value=scalars_mock)

        call_count = 0

        async def smart_execute(query):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_execute_result(rows=[job])
            return make_execute_result()

        db.execute = AsyncMock(side_effect=smart_execute)

        with (
            patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=fake_execute_scoring_fail),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()) as mock_filters,
        ):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

        mock_filters.assert_not_called()


# ── run_summary_on_demand ─────────────────────────────────────────────────────

class TestRunSummaryOnDemand:
    async def test_on_demand_works_when_default_disabled(self):
        """On-demand summary works even when ai_summary_enabled_default=False."""
        from app.services.ai_summary_service import run_summary_on_demand
        long_content = "word " * 500
        job = make_job(operation="summary", status="pending")
        settings = make_settings(ai_summary_enabled_default=False)
        state_after = make_state(ai_summary="On-demand summary text")
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,           # _ai_enabled_globally in enqueue_summary_job
            settings,       # UserSettings in enqueue_summary_job (default=False, but not checked)
            make_user_feed(),  # UserFeed in enqueue_summary_job
            job,            # job SELECT after flush
            settings,       # UserSettings for _execute_summary_job
            state_after,    # state SELECT after execution
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))

        async def fake_execute(j, a, s, d, now):
            j.status = "success"

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute):
            summary, truncated, error = await run_summary_on_demand(
                make_article(content=long_content), user_id=1, db=db
            )

        assert summary == "On-demand summary text"
        assert error is None

    async def test_returns_none_when_ineligible(self):
        """If enqueue_summary_job finds no eligible path, return None."""
        from app.services.ai_summary_service import run_summary_on_demand
        db = make_mock_db()
        # enqueue_summary_job returns False (AI disabled), so no job row is created
        # db.scalar after flush returns None (job doesn't exist)
        db.scalar = AsyncMock(side_effect=[
            False,  # _ai_enabled_globally in enqueue_summary_job
            None,   # job SELECT returns None
        ])
        summary, truncated, error = await run_summary_on_demand(make_article(), user_id=1, db=db)
        assert summary is None
        assert error is not None  # specific message, not None

    async def test_returns_existing_summary_when_already_done(self):
        """Job status='success' → returns (summary, None) without re-running."""
        from app.services.ai_summary_service import run_summary_on_demand
        job = make_job(operation="summary", status="success")
        state = make_state(ai_summary="Existing summary text")
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            False,  # enqueue_summary_job: ai_enabled False → no new job
            job,    # job SELECT
            state,  # state SELECT
        ])
        summary, truncated, error = await run_summary_on_demand(make_article(), user_id=1, db=db)
        assert summary == "Existing summary text"
        assert error is None

    async def test_resets_failed_job_and_retries(self):
        """job.status='failed' → reset to pending, execute, return (summary, None)."""
        from app.services.ai_summary_service import run_summary_on_demand
        job = make_job(operation="summary", status="failed", retry_count=2)
        settings = make_settings()
        state_after = make_state(ai_summary="Fresh summary")
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            False,        # enqueue: ai_enabled False
            job,          # job SELECT
            settings,     # UserSettings
            state_after,  # state SELECT after execution
        ])

        executed = []

        async def fake_execute(j, a, s, d, now):
            executed.append(True)
            j.status = "success"

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute):
            summary, truncated, error = await run_summary_on_demand(make_article(), user_id=1, db=db)

        assert job.status == "success"
        assert job.retry_count == 0
        assert job.next_retry_at is None
        assert len(executed) == 1
        assert summary == "Fresh summary"
        assert error is None

    async def test_resets_skipped_job_and_retries(self):
        """job.status='skipped' → also reset and retry on demand."""
        from app.services.ai_summary_service import run_summary_on_demand
        job = make_job(operation="summary", status="skipped")
        settings = make_settings()
        state_after = make_state(ai_summary="New summary")
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            False,
            job,
            settings,
            state_after,
        ])

        async def fake_execute(j, a, s, d, now):
            j.status = "success"

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute):
            summary, truncated, error = await run_summary_on_demand(make_article(), user_id=1, db=db)

        assert job.retry_count == 0
        assert summary == "New summary"
        assert error is None

    async def test_returns_error_message_when_execution_fails(self):
        """Execution sets status='failed' → returns (None, error_message)."""
        from app.services.ai_summary_service import run_summary_on_demand
        job = make_job(operation="summary", status="pending")
        settings = make_settings()
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            False,
            job,
            settings,
        ])

        async def fake_execute_fail(j, a, s, d, now):
            j.status = "failed"
            j.error_message = "Rate limit exceeded: too many requests"

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute_fail):
            summary, truncated, error = await run_summary_on_demand(make_article(), user_id=1, db=db)

        assert summary is None
        assert error == "Rate limit exceeded: too many requests"

    async def test_returns_skipped_message_when_content_rejected(self):
        """Execution sets status='skipped' → returns (None, descriptive message)."""
        from app.services.ai_summary_service import run_summary_on_demand
        job = make_job(operation="summary", status="pending")
        settings = make_settings()
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            False,
            job,
            settings,
        ])

        async def fake_execute_skip(j, a, s, d, now):
            j.status = "skipped"

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute_skip):
            summary, truncated, error = await run_summary_on_demand(make_article(), user_id=1, db=db)

        assert summary is None
        assert error is not None
        assert "too short" in error or "not available" in error


# ── run_pipeline_for_article_all_users ───────────────────────────────────────

class TestRunPipelineForArticleAllUsers:
    async def test_no_pipeline_when_no_labels(self):
        """Article with no labels → run_article_pipeline never called."""
        db = make_mock_db()
        # db.scalars returns empty list (no labeled users)
        scalars_result = AsyncMock()
        scalars_result.all = MagicMock(return_value=[])
        db.scalars = AsyncMock(return_value=scalars_result)

        with patch("app.services.ai_pipeline_service.run_article_pipeline", AsyncMock()) as mock_pipeline:
            from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
            await run_pipeline_for_article_all_users(make_article(), db=db)
            mock_pipeline.assert_not_called()

    async def test_runs_pipeline_for_each_labeled_user(self):
        """Two users labeled the article → pipeline runs twice with correct user IDs."""
        db = make_mock_db()
        scalars_result = AsyncMock()
        scalars_result.all = MagicMock(return_value=[1, 2])  # user_id 1 and 2
        db.scalars = AsyncMock(return_value=scalars_result)

        article = make_article()
        calls = []

        async def fake_pipeline(art, user_id, d):
            calls.append(user_id)

        with patch("app.services.ai_pipeline_service.run_article_pipeline", side_effect=fake_pipeline):
            from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
            await run_pipeline_for_article_all_users(article, db=db)

        assert calls == [1, 2]


# ── _run_ai_filters_now idempotency ──────────────────────────────────────────

class TestRunAiFiltersNowIdempotency:
    async def test_skips_when_already_applied(self):
        """ai_filters_applied=True → _apply_ai_filters_for_state must NOT be called."""
        db = make_mock_db()
        state = make_state(ai_filters_applied=True)
        db.scalar = AsyncMock(return_value=state)  # returns state with ai_filters_applied=True

        with patch("app.services.filter_service._apply_ai_filters_for_state", AsyncMock()) as mock_apply:
            from app.services.ai_pipeline_service import _run_ai_filters_now
            await _run_ai_filters_now(make_article(), user_id=1, db=db)
            mock_apply.assert_not_called()

    async def test_skips_when_no_state(self):
        """No UserArticleState (score never written) → nothing to apply."""
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=None)  # no state row

        with patch("app.services.filter_service._apply_ai_filters_for_state", AsyncMock()) as mock_apply:
            from app.services.ai_pipeline_service import _run_ai_filters_now
            await _run_ai_filters_now(make_article(), user_id=1, db=db)
            mock_apply.assert_not_called()


# ── _apply_ai_filters_for_state ───────────────────────────────────────────────

class TestApplyAiFiltersForState:
    async def test_sets_ai_filters_applied_true(self):
        from app.services.filter_service import _apply_ai_filters_for_state
        state = make_state()
        db = make_mock_db()
        await _apply_ai_filters_for_state(state, make_article(), None, [], db)
        assert state.ai_filters_applied is True

    async def test_executes_matching_filter_action(self):
        from app.services.filter_service import _apply_ai_filters_for_state
        state = make_state(ai_score=0.8)
        article = make_article()
        db = make_mock_db()

        filter_obj = SimpleNamespace(
            id=1, user_id=1, is_active=True, stop_on_match=False,
            conditions=[SimpleNamespace(field="ai_score", operator="gt", value="50")],
            actions=[SimpleNamespace(action_type="mark_read")],
            match_operator="AND",
            scope_include=None, scope_except=None,
        )

        executed_actions = []

        async def fake_execute_actions(f, art, uid, uf, d):
            executed_actions.append(f.id)

        with (
            patch("app.services.filter_service.evaluate_filter", return_value=True),
            patch("app.services.filter_service._execute_actions", side_effect=fake_execute_actions),
        ):
            await _apply_ai_filters_for_state(state, article, None, [filter_obj], db)

        assert 1 in executed_actions
        assert state.ai_filters_applied is True

    async def test_respects_stop_on_match(self):
        from app.services.filter_service import _apply_ai_filters_for_state
        state = make_state(ai_score=0.8)
        article = make_article()
        db = make_mock_db()

        f1 = SimpleNamespace(
            id=1, stop_on_match=True,
            conditions=[], actions=[], match_operator="AND",
            scope_include=None, scope_except=None,
        )
        f2 = SimpleNamespace(
            id=2, stop_on_match=False,
            conditions=[], actions=[], match_operator="AND",
            scope_include=None, scope_except=None,
        )

        executed = []

        async def fake_execute_actions(f, art, uid, uf, d):
            executed.append(f.id)

        with (
            patch("app.services.filter_service.evaluate_filter", return_value=True),
            patch("app.services.filter_service._execute_actions", side_effect=fake_execute_actions),
        ):
            await _apply_ai_filters_for_state(state, article, None, [f1, f2], db)

        assert executed == [1]  # f2 not reached

    async def test_no_actions_when_filter_does_not_match(self):
        from app.services.filter_service import _apply_ai_filters_for_state
        state = make_state(ai_score=0.3)
        db = make_mock_db()

        f = SimpleNamespace(
            id=1, stop_on_match=False,
            conditions=[], actions=[], match_operator="AND",
            scope_include=None, scope_except=None,
        )

        executed = []

        async def fake_execute_actions(f, art, uid, uf, d):
            executed.append(f.id)

        with (
            patch("app.services.filter_service.evaluate_filter", return_value=False),
            patch("app.services.filter_service._execute_actions", side_effect=fake_execute_actions),
        ):
            await _apply_ai_filters_for_state(state, make_article(), None, [f], db)

        assert executed == []
        assert state.ai_filters_applied is True  # always set, even with no matches


# ── auto-summary starring requirement ────────────────────────────────────────

class TestAutoSummaryStarringRequirement:
    """Auto-summary triggered by the pipeline must require a starred article.

    Regression tests for the bug where summary was generated for ALL labeled
    articles when ai_summary_enabled_default=True, instead of only starred ones.

    test_labeled_not_starred_no_summary is expected to FAIL before the fix.
    """

    async def test_labeled_not_starred_no_summary(self):
        """Article labeled (pipeline triggered) but NOT starred → summary must NOT run.

        EXPECTED TO FAIL before fix: currently run_article_pipeline checks only
        ai_summary_enabled_default, without verifying is_starred on UserArticleState.
        """
        article = make_article()
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            make_settings(ai_summary_enabled_default=True),  # UserSettings
            make_state(is_starred=False),                    # UserArticleState (checked after fix)
        ])
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)) as mock_enqueue,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_enqueue.assert_not_called()
            mock_summary.assert_not_called()

    async def test_starred_article_gets_auto_summary(self):
        """Article is starred → pipeline auto-summary IS triggered when ai_summary_enabled_default=True."""
        article = make_article()
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            make_settings(ai_summary_enabled_default=True),  # UserSettings
            make_state(is_starred=True),                     # UserArticleState
        ])
        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_article_pipeline
            await run_article_pipeline(article, user_id=1, db=db)
            mock_summary.assert_called_once_with(article, 1, db)

    async def test_run_pipeline_all_users_labeled_not_starred_no_summary(self):
        """run_pipeline_for_article_all_users: labeled user who hasn't starred → no summary.

        Simulates the exact bug path: readable extraction triggers pipeline for labeled
        users → must NOT generate summary unless user also starred the article.
        EXPECTED TO FAIL before fix.
        """
        article = make_article()
        db = make_mock_db()

        scalars_result = AsyncMock()
        scalars_result.all = MagicMock(return_value=[1])  # user_id=1 has label on article
        db.scalars = AsyncMock(return_value=scalars_result)

        db.scalar = AsyncMock(side_effect=[
            make_settings(ai_summary_enabled_default=True),  # UserSettings inside pipeline
            make_state(is_starred=False),                    # UserArticleState — not starred
        ])

        with (
            patch("app.services.ai_scoring_service.enqueue_scoring_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_scoring_now", AsyncMock()),
            patch("app.services.ai_pipeline_service._get_scoring_job_status", AsyncMock(return_value="success")),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)) as mock_enqueue,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
            await run_pipeline_for_article_all_users(article, db=db)
            mock_enqueue.assert_not_called()
            mock_summary.assert_not_called()


# ── enqueue_scoring_job — missing edge cases ──────────────────────────────────

class TestEnqueueScoringJobEdgeCases:
    async def test_returns_true_when_per_feed_scoring_none(self):
        """ai_scoring_enabled=None (no per-feed override) → inherits default, job created."""
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(ai_scoring_enabled=None),  # None = inherit default
            None,  # no existing job
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is True

    async def test_returns_false_when_no_fast_model(self):
        """ai_fast_provider set but ai_fast_model is None → False."""
        from app.services.ai_scoring_service import enqueue_scoring_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_fast_model=None),
            make_user_feed(),
        ])
        assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is False


# ── enqueue_summary_job — missing edge cases ──────────────────────────────────

class TestEnqueueSummaryJobEdgeCases:
    async def test_returns_false_when_no_user_settings(self):
        """No UserSettings row → False."""
        from app.services.ai_summary_service import enqueue_summary_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[True, None])  # ai_enabled, no settings
        assert await enqueue_summary_job(make_article(), user_id=1, db=db) is False

    async def test_returns_false_when_no_quality_model(self):
        """ai_quality_provider set but ai_quality_model is None → False."""
        from app.services.ai_summary_service import enqueue_summary_job
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(ai_quality_model=None),
        ])
        assert await enqueue_summary_job(make_article(), user_id=1, db=db) is False

    async def test_returns_true_when_per_feed_summary_none(self):
        """ai_summary_enabled=None (no per-feed override) → job created."""
        from app.services.ai_summary_service import enqueue_summary_job
        long_content = "word " * 500
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[
            True,
            make_settings(),
            make_user_feed(ai_summary_enabled=None),  # None = inherit default
        ])
        db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
        assert await enqueue_summary_job(make_article(content=long_content), user_id=1, db=db) is True


# ── process_pending_scoring — starring requirement ────────────────────────────

class TestProcessPendingScoringStarringRequirement:
    """Batch scoring runner must check is_starred before auto-summary, same as run_article_pipeline."""

    def _make_batch_db(self, job, article, settings, state):
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)  # ai_enabled check
        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], [settings], [state]])
        db.scalars = AsyncMock(return_value=scalars_mock)
        call_count = 0

        async def smart_execute(query):
            nonlocal call_count
            call_count += 1
            return make_execute_result(rows=[job] if call_count == 1 else [])

        db.execute = AsyncMock(side_effect=smart_execute)
        return db

    async def test_no_auto_summary_when_article_not_starred(self):
        """Batch: scoring success + default enabled, but article NOT starred → summary not triggered."""
        job = make_job()
        article = make_article()
        settings = make_settings(ai_summary_enabled_default=True)
        state = make_state(is_starred=False)
        db = self._make_batch_db(job, article, settings, state)

        async def fake_execute_scoring(j, a, s, d, now):
            j.status = "success"

        with (
            patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=fake_execute_scoring),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)) as mock_enqueue,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

        mock_enqueue.assert_not_called()
        mock_summary.assert_not_called()

    async def test_auto_summary_when_article_is_starred(self):
        """Batch: scoring success + default enabled + article IS starred → summary triggered."""
        job = make_job()
        article = make_article()
        settings = make_settings(ai_summary_enabled_default=True)
        state = make_state(is_starred=True)
        db = self._make_batch_db(job, article, settings, state)

        async def fake_execute_scoring(j, a, s, d, now):
            j.status = "success"

        with (
            patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=fake_execute_scoring),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)),
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

        mock_summary.assert_called_once_with(article, job.user_id, db)

    async def test_no_auto_summary_when_state_missing(self):
        """Batch: no UserArticleState row (brand-new article) → summary not triggered."""
        job = make_job()
        article = make_article()
        settings = make_settings(ai_summary_enabled_default=True)
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)
        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], [settings], []])  # empty states
        db.scalars = AsyncMock(return_value=scalars_mock)
        call_count = 0

        async def smart_execute(query):
            nonlocal call_count
            call_count += 1
            return make_execute_result(rows=[job] if call_count == 1 else [])

        db.execute = AsyncMock(side_effect=smart_execute)

        async def fake_execute_scoring(j, a, s, d, now):
            j.status = "success"

        with (
            patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=fake_execute_scoring),
            patch("app.services.ai_pipeline_service._run_ai_filters_now", AsyncMock()),
            patch("app.services.ai_summary_service.enqueue_summary_job", AsyncMock(return_value=True)) as mock_enqueue,
            patch("app.services.ai_pipeline_service._run_summary_now", AsyncMock()) as mock_summary,
        ):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

        mock_enqueue.assert_not_called()
        mock_summary.assert_not_called()


# ── process_pending_summaries ─────────────────────────────────────────────────

class TestProcessPendingSummaries:
    """Batch summary processor — completely separate from pipeline trigger logic."""

    async def test_returns_zero_when_ai_globally_disabled(self):
        """Global AI kill-switch off → return 0 immediately."""
        from app.services.ai_summary_service import process_pending_summaries
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=False)  # ai_enabled = False
        result = await process_pending_summaries(db)
        assert result == 0
        db.execute.assert_not_called()

    async def test_returns_zero_when_no_pending_jobs(self):
        """No pending summary jobs → return 0."""
        from app.services.ai_summary_service import process_pending_summaries
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)  # ai_enabled

        jobs_result = MagicMock()
        jobs_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=jobs_result)

        result = await process_pending_summaries(db)
        assert result == 0

    async def test_processes_job_successfully(self):
        """One pending job → _execute_summary_job called, returns 1."""
        from app.services.ai_summary_service import process_pending_summaries
        job = make_job(operation="summary")
        article = make_article()
        settings = make_settings()
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)

        jobs_result = MagicMock()
        jobs_result.scalars.return_value.all.return_value = [job]
        db.execute = AsyncMock(return_value=jobs_result)

        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], [settings]])
        db.scalars = AsyncMock(return_value=scalars_mock)

        executed = []

        async def fake_execute(j, a, s, d, now):
            executed.append(j)
            j.status = "success"

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute):
            result = await process_pending_summaries(db)

        assert result == 1
        assert len(executed) == 1

    async def test_skips_when_article_not_found(self):
        """Job references article that no longer exists → status=skipped, count=1."""
        from app.services.ai_summary_service import process_pending_summaries
        job = make_job(operation="summary", article_id=99)
        settings = make_settings()
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)

        jobs_result = MagicMock()
        jobs_result.scalars.return_value.all.return_value = [job]
        db.execute = AsyncMock(return_value=jobs_result)

        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[], [settings]])  # no articles found
        db.scalars = AsyncMock(return_value=scalars_mock)

        with patch("app.services.ai_summary_service._execute_summary_job", AsyncMock()) as mock_exec:
            result = await process_pending_summaries(db)

        assert result == 1
        assert job.status == "skipped"
        mock_exec.assert_not_called()

    async def test_skips_when_settings_not_found(self):
        """Job references user with no settings → status=skipped, count=1."""
        from app.services.ai_summary_service import process_pending_summaries
        job = make_job(operation="summary")
        article = make_article()
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)

        jobs_result = MagicMock()
        jobs_result.scalars.return_value.all.return_value = [job]
        db.execute = AsyncMock(return_value=jobs_result)

        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article], []])  # no settings found
        db.scalars = AsyncMock(return_value=scalars_mock)

        with patch("app.services.ai_summary_service._execute_summary_job", AsyncMock()) as mock_exec:
            result = await process_pending_summaries(db)

        assert result == 1
        assert job.status == "skipped"
        mock_exec.assert_not_called()

    async def test_returns_correct_count_for_multiple_jobs(self):
        """Two pending jobs → both processed, returns 2."""
        from app.services.ai_summary_service import process_pending_summaries
        job1 = make_job(id=1, operation="summary", article_id=10)
        job2 = make_job(id=2, operation="summary", article_id=11, user_id=2)
        article1 = make_article(id=10)
        article2 = make_article(id=11)
        settings1 = make_settings(user_id=1)
        settings2 = make_settings(user_id=2)
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=True)

        jobs_result = MagicMock()
        jobs_result.scalars.return_value.all.return_value = [job1, job2]
        db.execute = AsyncMock(return_value=jobs_result)

        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[article1, article2], [settings1, settings2]])
        db.scalars = AsyncMock(return_value=scalars_mock)

        executed = []

        async def fake_execute(j, a, s, d, now):
            executed.append(j.id)

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=fake_execute):
            result = await process_pending_summaries(db)

        assert result == 2
        assert sorted(executed) == [1, 2]


# ── _execute_summary_job: truncation flag ─────────────────────────────────────

class TestSummaryTruncationFlag:
    """A summary cut off by the model's token cap is still worth keeping, but it
    must be stored as truncated so the reader is not shown a half sentence that
    looks complete."""

    @staticmethod
    def _db_with_state(state):
        db = make_mock_db()
        db.scalar = AsyncMock(return_value=state)
        return db

    @staticmethod
    def _patched(summarize_result):
        return patch.multiple(
            "app.services.ai_service",
            get_ai_client=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6")),
            summarize_article=AsyncMock(return_value=summarize_result),
        )

    async def _run(self, state, summarize_result):
        from app.services.ai_summary_service import _execute_summary_job
        job = make_job(operation="summary")
        # Comfortably over _MIN_CONTENT_CHARS, so the job reaches the provider call.
        article = make_article(content="Some content " * 200)
        with self._patched(summarize_result):
            await _execute_summary_job(
                job, article, make_settings(), self._db_with_state(state),
                datetime.now(timezone.utc),
            )
        return job

    async def test_truncated_summary_is_stored_and_flagged(self):
        state = make_state()
        job = await self._run(state, ("Cut off mid-sen", 500, 400, True))
        assert job.status == "success"          # kept, not failed
        assert state.ai_summary == "Cut off mid-sen"
        assert state.ai_summary_truncated is True

    async def test_complete_summary_is_not_flagged(self):
        state = make_state()
        await self._run(state, ("A whole summary.", 500, 120, False))
        assert state.ai_summary_truncated is False

    async def test_regenerating_clears_a_stale_flag(self):
        """A previously truncated summary that regenerates in full must lose the
        badge — the flag is written on every success, not only when true."""
        state = make_state(ai_summary="Old cut off", ai_summary_truncated=True)
        await self._run(state, ("Now complete.", 500, 130, False))
        assert state.ai_summary == "Now complete."
        assert state.ai_summary_truncated is False
