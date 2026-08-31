"""Tests for AI summary/context HTMX endpoints and XSS escaping regression."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.rate_limit import limiter
    limiter.reset()
    yield


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_article(**kwargs):
    defaults = {
        "id": 10,
        "title": "Test Article",
        "content": "word " * 500,
        "readable_content": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_settings(**kwargs):
    defaults = {
        "user_id": 1,
        "ai_quality_provider": "anthropic",
        "ai_quality_model": "claude-sonnet-4-6",
        "ai_content_limit": 20000,
        "ai_min_content_chars": 1_700,
        "ai_summary_prompt": None,
        "ai_context_prompt": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_job(**kwargs):
    defaults = {
        "id": 1,
        "article_id": 10,
        "user_id": 1,
        "operation": "summary",
        "status": "pending",
        "error_message": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_state(**kwargs):
    defaults = {
        "user_id": 1,
        "article_id": 10,
        "ai_summary": None,
        "ai_summary_truncated": False,
        "ai_context": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_execute_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def setup_db(db, *, scalars, article=None):
    db.scalar = AsyncMock(side_effect=scalars)
    db.execute = AsyncMock(return_value=make_execute_result(article))


# ── POST /htmx/articles/{id}/ai-summary ──────────────────────────────────────

class TestHtmxAiSummaryTrigger:
    def test_ai_disabled_returns_disabled_message(self, client, mock_db):
        setup_db(mock_db, scalars=[False])
        resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "AI is disabled" in resp.text
        assert 'id="ai-summary-10"' in resp.text

    def test_no_quality_model_returns_not_configured(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings(ai_quality_provider=None)])
        resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "not configured" in resp.text

    def test_article_not_found_returns_404(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings()], article=None)
        resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 404

    def test_content_too_short_returns_too_short_message(self, client, mock_db):
        short_article = make_article(content="short", title="T")
        setup_db(mock_db, scalars=[True, make_settings()], article=short_article)
        resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "too short" in resp.text
        assert 'id="ai-summary-10"' in resp.text

    def test_button_ignores_the_users_minimum_length(self, client, mock_db):
        """The setting gates the automatic runs. Refusing the button as well left
        no way through short of editing settings and coming back."""
        setup_db(
            mock_db,
            scalars=[True, make_settings(ai_min_content_chars=9_000)],
            article=make_article(),  # 2 500 characters, far below that
        )
        with patch(
            "app.services.ai_summary_service.run_summary_on_demand",
            new=AsyncMock(return_value=("This is the summary.", False, None)),
        ):
            resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "This is the summary." in resp.text

    def test_successful_summary_returns_block(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings()], article=make_article())
        with patch(
            "app.services.ai_summary_service.run_summary_on_demand",
            new=AsyncMock(return_value=("This is the summary.", False, None)),
        ):
            resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "This is the summary." in resp.text
        assert 'id="ai-summary-10"' in resp.text

    def test_summary_failure_returns_error_message(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings()], article=make_article())
        with patch(
            "app.services.ai_summary_service.run_summary_on_demand",
            new=AsyncMock(return_value=(None, False, "Rate limit exceeded")),
        ):
            resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "Summary failed" in resp.text
        assert "Rate limit exceeded" in resp.text


# ── GET /htmx/articles/{id}/ai-summary/poll ──────────────────────────────────

class TestHtmxAiSummaryPoll:
    def test_no_job_stops_polling(self, client, mock_db):
        # Cancelled (unstarred) or never queued — the spinner must not keep spinning.
        mock_db.scalar = AsyncMock(return_value=None)
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert "hx-get" not in resp.text
        assert 'id="ai-summary-10"' in resp.text

    def test_pending_job_returns_spinner(self, client, mock_db):
        mock_db.scalar = AsyncMock(return_value=make_job(status="pending"))
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert "hx-get" in resp.text

    def test_failed_job_returns_error_message(self, client, mock_db):
        mock_db.scalar = AsyncMock(return_value=make_job(
            status="failed", error_message="Provider error"
        ))
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert "Summary failed" in resp.text
        assert "Provider error" in resp.text
        assert 'id="ai-summary-10"' in resp.text

    def test_success_with_state_returns_summary_block(self, client, mock_db):
        job = make_job(status="success")
        state = make_state(ai_summary="Polled summary text")
        mock_db.scalar = AsyncMock(side_effect=[job, state])
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert "Polled summary text" in resp.text
        assert 'id="ai-summary-10"' in resp.text

    def test_success_without_state_returns_empty_div(self, client, mock_db):
        job = make_job(status="success")
        mock_db.scalar = AsyncMock(side_effect=[job, None])
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert 'id="ai-summary-10"' in resp.text

    def test_skipped_job_says_so_instead_of_going_blank(self, client, mock_db):
        """A skipped job used to fall through to the no-summary branch and swap the
        spinner for an empty div, so the summary just stopped arriving with nothing
        said about why."""
        mock_db.scalar = AsyncMock(side_effect=[make_job(status="skipped"), None])
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert "skipped" in resp.text
        assert "main AI model" in resp.text
        assert "hx-get" not in resp.text
        assert 'id="ai-summary-10"' in resp.text


# ── POST /htmx/articles/{id}/ai-context ──────────────────────────────────────

class TestHtmxAiContextTrigger:
    def test_ai_disabled_returns_disabled_message(self, client, mock_db):
        setup_db(mock_db, scalars=[False])
        resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "AI is disabled" in resp.text
        assert 'id="ai-context-10"' in resp.text

    def test_no_quality_model_returns_not_configured(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings(ai_quality_model=None)])
        resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "not configured" in resp.text

    def test_article_not_found_returns_404(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings()], article=None)
        resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 404

    def test_content_too_short_returns_too_short_message(self, client, mock_db):
        short_article = make_article(content="short", title="T")
        setup_db(mock_db, scalars=[True, make_settings()], article=short_article)
        resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "too short" in resp.text
        assert 'id="ai-context-10"' in resp.text

    def test_summary_threshold_does_not_gate_context(self, client, mock_db):
        """Context supplies what the article leaves out, so it is most useful on a
        short piece. Raising the summary threshold must not switch it off there."""
        state = make_state()
        setup_db(
            mock_db,
            scalars=[True, make_settings(ai_min_content_chars=9_000), state],
            article=make_article(),  # 2 500 characters: far below that threshold
        )
        with (
            patch(
                "app.services.ai_service.get_ai_client",
                new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6")),
            ),
            patch(
                "app.services.ai_service.get_article_context",
                new=AsyncMock(return_value=("Broader context text.", 10, 5)),
            ),
        ):
            resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "Broader context text." in resp.text

    def test_context_still_refuses_a_bare_headline(self, client, mock_db):
        """The floor is low, not absent: a headline with no body is nothing to work
        from, whatever the summary threshold says."""
        setup_db(
            mock_db,
            scalars=[True, make_settings(ai_min_content_chars=500)],
            article=make_article(content="", title="A headline and nothing else"),
        )
        resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "too short" in resp.text

    def test_successful_context_returns_block(self, client, mock_db):
        state = make_state()
        setup_db(mock_db, scalars=[True, make_settings(), state], article=make_article())
        with (
            patch(
                "app.services.ai_service.get_ai_client",
                new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6")),
            ),
            patch(
                "app.services.ai_service.get_article_context",
                new=AsyncMock(return_value=("Broader context text.", 10, 5)),
            ),
        ):
            resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "Broader context text." in resp.text
        assert 'id="ai-context-10"' in resp.text

    def test_context_exception_returns_error_message(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings()], article=make_article())
        with (
            patch(
                "app.services.ai_service.get_ai_client",
                new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6")),
            ),
            patch(
                "app.services.ai_service.get_article_context",
                new=AsyncMock(side_effect=Exception("API timeout")),
            ),
        ):
            resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "Context failed" in resp.text
        assert "API timeout" in resp.text

    def test_no_ai_client_returns_not_configured(self, client, mock_db):
        setup_db(mock_db, scalars=[True, make_settings()], article=make_article())
        with patch(
            "app.services.ai_service.get_ai_client",
            new=AsyncMock(return_value=(None, None, None)),
        ):
            resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "not configured" in resp.text


# ── XSS escaping regression ───────────────────────────────────────────────────

class TestAiErrorXSSEscaping:
    def test_summary_poll_error_escapes_html(self, client, mock_db):
        payload = "<script>alert('xss')</script>"
        mock_db.scalar = AsyncMock(return_value=make_job(
            status="failed", error_message=payload
        ))
        resp = client.get("/htmx/articles/10/ai-summary/poll")
        assert resp.status_code == 200
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

    def test_context_exception_escapes_html(self, client, mock_db):
        payload = "<img src=x onerror=alert(1)>"
        setup_db(mock_db, scalars=[True, make_settings()], article=make_article())
        with (
            patch(
                "app.services.ai_service.get_ai_client",
                new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6")),
            ),
            patch(
                "app.services.ai_service.get_article_context",
                new=AsyncMock(side_effect=Exception(payload)),
            ),
        ):
            resp = client.post("/htmx/articles/10/ai-context")
        assert resp.status_code == 200
        assert "<img" not in resp.text
        assert "&lt;img" in resp.text

    def test_summary_on_demand_error_escapes_html(self, client, mock_db):
        payload = '<b onclick="evil()">click</b>'
        setup_db(mock_db, scalars=[True, make_settings()], article=make_article())
        with patch(
            "app.services.ai_summary_service.run_summary_on_demand",
            new=AsyncMock(return_value=(None, False, payload)),
        ):
            resp = client.post("/htmx/articles/10/ai-summary")
        assert resp.status_code == 200
        assert "<b " not in resp.text
        assert "&lt;b " in resp.text
