"""Wall-clock budgets on model calls, and what a batch does when one runs out.

The SDK timeouts are per read, so an endpoint that trickles bytes never trips
them; the budget is what bounds a call. A batch that hits it must stop waiting
on that account for the rest of the run, or one slow endpoint holds up every
other account's scores.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import ai_service
from app.services.ai_service import AiCallTimeout
from tests.test_ai_pipeline import (
    make_article, make_execute_result, make_job, make_mock_db, make_settings,
)


def _hanging_anthropic_client():
    async def hang(**_kwargs):
        await asyncio.sleep(3600)

    return SimpleNamespace(messages=SimpleNamespace(create=hang))


class TestWithinBudget:
    async def test_completion_past_budget_raises_ai_call_timeout(self):
        with pytest.raises(AiCallTimeout, match="did not finish answering"):
            await ai_service._complete(
                "prompt", _hanging_anthropic_client(), "anthropic", "m", budget=0.05
            )

    async def test_score_article_uses_the_scoring_budget(self, monkeypatch):
        monkeypatch.setattr(ai_service, "_SCORING_BUDGET_SECONDS", 0.05)
        with pytest.raises(AiCallTimeout):
            await ai_service.score_article(
                "content", "profile", _hanging_anthropic_client(), "anthropic", "m"
            )

    async def test_chat_is_bounded_too(self, monkeypatch):
        monkeypatch.setattr(ai_service, "_TEXT_BUDGET_SECONDS", 0.05)
        with pytest.raises(AiCallTimeout):
            await ai_service.chat_with_article(
                [{"role": "user", "content": "hi"}], None,
                _hanging_anthropic_client(), "anthropic", "m",
            )

    async def test_timeout_is_retried_not_terminal(self):
        """A timeout says nothing permanent about the model, so the job backs off."""
        from datetime import datetime, timezone

        from app.services.ai_jobs import apply_job_failure

        job = make_job()
        apply_job_failure(job, AiCallTimeout("The model did not finish answering within 120 seconds."),
                          datetime.now(timezone.utc), operation="scoring", settings=None)
        assert job.status == "pending"
        assert job.next_retry_at is not None
        assert "120 seconds" in job.error_message

    async def test_verify_reports_a_hanging_endpoint(self, monkeypatch):
        monkeypatch.setattr(ai_service, "_VERIFY_BUDGET_SECONDS", 0.05)
        monkeypatch.setattr(ai_service, "_make_client", lambda *a, **k: _hanging_anthropic_client())
        monkeypatch.setattr(ai_service, "close_ai_client", AsyncMock())
        monkeypatch.setattr(ai_service, "get_api_key", AsyncMock(return_value="k"))
        result = await ai_service.verify_ai_slot(
            1, "fast", make_mock_db(), provider_override="anthropic", model_override="m",
        )
        assert result["ok"] is False
        assert "Timed out" in result["error"]


def _batch_db(jobs, articles, settings):
    db = make_mock_db()
    scalars_mock = AsyncMock()
    scalars_mock.all = MagicMock(side_effect=[articles, settings, []])
    db.scalars = AsyncMock(return_value=scalars_mock)
    calls = 0

    async def execute(_query):
        nonlocal calls
        calls += 1
        return make_execute_result(rows=jobs if calls == 1 else [])

    db.execute = AsyncMock(side_effect=execute)
    db.scalar = AsyncMock(return_value=True)  # ai_enabled
    return db


class TestBatchSkipsTimedOutAccount:
    async def test_scoring_batch_stops_waiting_on_a_slow_account(self):
        slow_1 = make_job(id=1, user_id=1, article_id=10)
        slow_2 = make_job(id=2, user_id=1, article_id=10)
        other = make_job(id=3, user_id=2, article_id=10)
        db = _batch_db(
            [slow_1, slow_2, other],
            [make_article()],
            [make_settings(user_id=1), make_settings(user_id=2)],
        )
        seen = []

        async def execute_job(job, *_args, **_kwargs):
            seen.append(job.id)
            return job.user_id == 1  # the first account's model runs out of time

        with patch("app.services.ai_scoring_service._execute_scoring_job", side_effect=execute_job):
            from app.services.ai_scoring_service import process_pending_scoring
            await process_pending_scoring(db)

        assert seen == [1, 3]
        assert slow_2.status == "pending"  # left for the next run, untouched
        assert slow_2.retry_count == 0

    async def test_summary_batch_stops_waiting_on_a_slow_account(self):
        slow_1 = make_job(id=1, user_id=1, operation="summary")
        slow_2 = make_job(id=2, user_id=1, operation="summary")
        other = make_job(id=3, user_id=2, operation="summary")
        db = _batch_db(
            [slow_1, slow_2, other],
            [make_article()],
            [make_settings(user_id=1), make_settings(user_id=2)],
        )
        # The summary batch preloads two maps, not three.
        scalars_mock = AsyncMock()
        scalars_mock.all = MagicMock(side_effect=[[make_article()],
                                                  [make_settings(user_id=1), make_settings(user_id=2)]])
        db.scalars = AsyncMock(return_value=scalars_mock)
        seen = []

        async def execute_job(job, *_args, **_kwargs):
            seen.append(job.id)
            return job.user_id == 1

        with patch("app.services.ai_summary_service._execute_summary_job", side_effect=execute_job):
            from app.services.ai_summary_service import process_pending_summaries
            await process_pending_summaries(db)

        assert seen == [1, 3]
        assert slow_2.status == "pending"

    async def test_execute_scoring_job_reports_the_timeout(self):
        """The real job function, so the flag the batch reads is the one it sets."""
        from datetime import datetime, timezone

        from app.services.ai_scoring_service import _execute_scoring_job

        job = make_job()
        db = make_mock_db()
        pool = MagicMock()
        pool.get = AsyncMock(return_value=(object(), "anthropic", "m"))
        with patch("app.services.ai_service.score_article",
                   AsyncMock(side_effect=AiCallTimeout("slow"))):
            timed_out = await _execute_scoring_job(
                job, make_article(), make_settings(), db, datetime.now(timezone.utc), pool,
            )
        assert timed_out is True
        assert job.status == "pending"

        job = make_job()
        with patch("app.services.ai_service.score_article",
                   AsyncMock(side_effect=RuntimeError("500 server error"))):
            timed_out = await _execute_scoring_job(
                job, make_article(), make_settings(), db, datetime.now(timezone.utc), pool,
            )
        assert timed_out is False
