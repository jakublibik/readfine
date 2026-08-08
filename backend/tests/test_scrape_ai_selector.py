"""Tests for AI CSS selector generation: service + /settings/feeds/scrape-ai-selector endpoint."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services.ai_service import Completion


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_settings(**kwargs):
    defaults = {
        "user_id": 1,
        "ai_quality_provider": "anthropic",
        "ai_quality_model": "claude-sonnet-4-6",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── Fixture: client with require_ai_enabled bypassed ─────────────────────────

@pytest.fixture
def ai_client(mock_db, monkeypatch):
    monkeypatch.setenv("RATELIMIT_ENABLED", "0")
    from app.main import app
    from app.auth.dependencies import get_api_user, get_current_user, require_ai_enabled
    from app.database import get_db
    from app.rate_limit import limiter
    from tests.conftest import MOCK_USER

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_api_user] = lambda: MOCK_USER
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_ai_enabled] = lambda: None

    limiter.enabled = False
    try:
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()


def make_anthropic_response(text: str):
    resp = MagicMock()
    # type is required on every real content block, and extraction keys off it.
    resp.content = [MagicMock(type="text", text=text)]
    resp.stop_reason = "end_turn"
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 5
    return resp


_SAMPLE_HTML = "<html><body><a href='/a'>Article One</a><a href='/b'>Article Two</a></body></html>"


# ── generate_css_selector_from_sample ────────────────────────────────────────

class TestGenerateCssSelectorFromSample:
    async def test_returns_cleaned_selector(self):
        from app.services.ai_service import generate_css_selector_from_sample
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=make_anthropic_response("  a.article  "))
        selector, in_tok, out_tok = await generate_css_selector_from_sample(
            url="https://example.com",
            sample=_SAMPLE_HTML,
            history=[],
            client=client,
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        assert selector == "a.article"
        assert in_tok == 10
        assert out_tok == 5

    async def test_strips_backticks_and_quotes(self):
        from app.services.ai_service import generate_css_selector_from_sample
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=make_anthropic_response("`div.post`"))
        selector, _, _ = await generate_css_selector_from_sample(
            url="https://example.com",
            sample=_SAMPLE_HTML,
            history=[],
            client=client,
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        assert selector == "div.post"

    async def test_takes_only_first_line(self):
        from app.services.ai_service import generate_css_selector_from_sample
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=make_anthropic_response("a.link\nSome explanation"))
        selector, _, _ = await generate_css_selector_from_sample(
            url="https://example.com",
            sample=_SAMPLE_HTML,
            history=[],
            client=client,
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        assert selector == "a.link"

    async def test_passes_history_to_prompt(self):
        from app.services.ai_service import generate_css_selector_from_sample
        from app.utils.scrape_ai import build_selector_prompt
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=make_anthropic_response("a"))
        history = [{"selector": "div", "feedback": "too broad"}]

        with patch("app.services.ai_service._complete", new=AsyncMock(return_value=Completion("a", 5, 3, False))) as mock_complete:
            await generate_css_selector_from_sample(
                url="https://example.com",
                sample=_SAMPLE_HTML,
                history=history,
                client=client,
                provider="anthropic",
                model="claude-sonnet-4-6",
            )
            prompt_used = mock_complete.call_args[0][0]
            assert "too broad" in prompt_used


# ── /settings/feeds/scrape-ai-selector endpoint ──────────────────────────────

class TestScrapeAiSelectorEndpoint:
    URL = "/settings/feeds/scrape-ai-selector"

    def test_missing_url_returns_error(self, ai_client, mock_db):
        resp = ai_client.post(self.URL, data={"url": "", "html_sample": _SAMPLE_HTML})
        assert resp.status_code == 200
        assert "URL is required" in resp.text

    def test_quality_model_not_configured(self, ai_client, mock_db):
        with patch("app.routers.web.settings.scrape.get_ai_client",
                   new=AsyncMock(return_value=(None, None, None))):
            resp = ai_client.post(self.URL, data={"url": "https://example.com", "html_sample": _SAMPLE_HTML})
        assert resp.status_code == 200
        assert "Quality model not configured" in resp.text

    def test_valid_selector_returned(self, ai_client, mock_db):
        with (
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  new=AsyncMock(return_value=("a.article-link", 10, 5))),
        ):
            resp = ai_client.post(self.URL, data={"url": "https://example.com", "html_sample": _SAMPLE_HTML})
        assert resp.status_code == 200
        assert "a.article-link" in resp.text

    def test_ai_generates_prose_returns_invalid_error(self, ai_client, mock_db):
        with (
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  new=AsyncMock(return_value=("Sorry I cannot find a selector", 10, 5))),
        ):
            resp = ai_client.post(self.URL, data={"url": "https://example.com", "html_sample": _SAMPLE_HTML})
        assert resp.status_code == 200
        assert "valid selector" in resp.text.lower() or "error" in resp.text.lower()

    def test_ai_error_returns_error_partial(self, ai_client, mock_db):
        with (
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  side_effect=Exception("provider overloaded")),
        ):
            resp = ai_client.post(self.URL, data={"url": "https://example.com", "html_sample": _SAMPLE_HTML})
        assert resp.status_code == 200
        assert "provider overloaded" in resp.text or "AI error" in resp.text

    def test_fetch_url_when_no_html_sample(self, ai_client, mock_db):
        with (
            patch("app.routers.web.settings.scrape.fetch_page_html",
                  new=AsyncMock(return_value=_SAMPLE_HTML)),
            patch("app.routers.web.settings.scrape.extract_article_sample",
                  return_value=_SAMPLE_HTML),
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  new=AsyncMock(return_value=("a.post", 10, 5))),
        ):
            resp = ai_client.post(self.URL, data={"url": "https://example.com"})
        assert resp.status_code == 200
        assert "a.post" in resp.text

    def test_fetch_failure_returns_error(self, ai_client, mock_db):
        with patch("app.routers.web.settings.scrape.fetch_page_html",
                   new=AsyncMock(side_effect=Exception("connection refused"))):
            resp = ai_client.post(self.URL, data={"url": "https://example.com"})
        assert resp.status_code == 200
        assert "Could not fetch page" in resp.text

    def test_selector_logged_to_ai_usage_log(self, ai_client, mock_db):
        with (
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  new=AsyncMock(return_value=("a.item", 12, 6))),
        ):
            ai_client.post(self.URL, data={"url": "https://example.com", "html_sample": _SAMPLE_HTML})
        mock_db.add.assert_called_once()
        log_obj = mock_db.add.call_args[0][0]
        assert log_obj.operation == "css_selector_generation"
        assert log_obj.input_tokens == 12
        assert log_obj.output_tokens == 6

    def test_invalid_selector_too_long(self, ai_client, mock_db):
        long_selector = "a" * 301
        with (
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  new=AsyncMock(return_value=(long_selector, 10, 5))),
        ):
            resp = ai_client.post(self.URL, data={"url": "https://example.com", "html_sample": _SAMPLE_HTML})
        assert resp.status_code == 200
        assert "valid selector" in resp.text.lower() or "error" in resp.text.lower()

    def test_conversation_history_passed_to_ai(self, ai_client, mock_db):
        import json
        history = [{"selector": "div", "feedback": "too broad"}]
        captured = {}

        async def fake_generate(url, sample, history_arg, client, provider, model):
            captured["history"] = history_arg
            return ("a.article", 10, 5)

        with (
            patch("app.routers.web.settings.scrape.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.routers.web.settings.scrape.generate_css_selector_from_sample",
                  side_effect=fake_generate),
        ):
            ai_client.post(self.URL, data={
                "url": "https://example.com",
                "html_sample": _SAMPLE_HTML,
                "conversation_history": json.dumps(history),
            })
        assert captured.get("history") == history
