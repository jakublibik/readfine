"""Tests for AI chat: chat_with_article() service + htmx endpoints."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_article(**kwargs):
    defaults = {
        "id": 10,
        "title": "Test Article",
        "content": "Article content " * 100,
        "readable_content": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_settings(**kwargs):
    defaults = {
        "user_id": 1,
        "ai_quality_provider": "anthropic",
        "ai_quality_model": "claude-sonnet-4-6",
        "ai_fast_provider": "anthropic",
        "ai_fast_model": "claude-haiku-4-5",
        "ai_chat_enabled": True,
        "ai_content_limit": 20000,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_chat(**kwargs):
    defaults = {
        "user_id": 1,
        "article_id": 10,
        "messages": [],
        "updated_at": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_execute_result(value):
    """Mock db.execute() return that supports .scalar_one_or_none()."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def make_anthropic_response(text: str):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


def make_openai_response(text: str):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def make_gemini_response(text: str):
    resp = MagicMock()
    resp.text = text
    return resp


# ── chat_with_article ─────────────────────────────────────────────────────────

class TestChatWithArticle:
    async def test_anthropic_with_article(self):
        from app.services.ai_service import chat_with_article
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=make_anthropic_response("  Anthropic answer  "))
        result = await chat_with_article(
            messages=[{"role": "user", "content": "What is this about?"}],
            article_content="Some article text",
            client=client, provider="anthropic", model="claude-sonnet-4-6",
        )
        assert result == "Anthropic answer"
        call_kwargs = client.messages.create.call_args.kwargs
        assert "system" in call_kwargs
        assert "Some article text" in call_kwargs["system"]
        assert call_kwargs["messages"] == [{"role": "user", "content": "What is this about?"}]

    async def test_anthropic_without_article(self):
        from app.services.ai_service import chat_with_article
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=make_anthropic_response("answer"))
        await chat_with_article(
            messages=[{"role": "user", "content": "Hi"}],
            article_content=None,
            client=client, provider="anthropic", model="claude-sonnet-4-6",
        )
        call_kwargs = client.messages.create.call_args.kwargs
        assert "system" not in call_kwargs

    async def test_openai_with_article(self):
        from app.services.ai_service import chat_with_article
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            return_value=make_openai_response("OpenAI answer"))
        result = await chat_with_article(
            messages=[{"role": "user", "content": "Question"}],
            article_content="Article text",
            client=client, provider="openai", model="gpt-4o",
        )
        assert result == "OpenAI answer"
        sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
        assert sent_messages[0]["role"] == "system"
        assert "Article text" in sent_messages[0]["content"]

    async def test_openai_without_article(self):
        from app.services.ai_service import chat_with_article
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            return_value=make_openai_response("answer"))
        await chat_with_article(
            messages=[{"role": "user", "content": "Hi"}],
            article_content=None,
            client=client, provider="openai", model="gpt-4o",
        )
        sent_messages = client.chat.completions.create.call_args.kwargs["messages"]
        assert all(m["role"] != "system" for m in sent_messages)

    async def test_gemini_role_mapping(self):
        from app.services.ai_service import chat_with_article
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(
            return_value=make_gemini_response("Gemini answer"))
        result = await chat_with_article(
            messages=[
                {"role": "user", "content": "Q"},
                {"role": "assistant", "content": "A"},
                {"role": "user", "content": "Follow-up"},
            ],
            article_content="Article",
            client=client, provider="gemini", model="gemini-2.0-flash",
        )
        assert result == "Gemini answer"
        contents = client.aio.models.generate_content.call_args.kwargs["contents"]
        roles = [m["role"] for m in contents]
        assert roles == ["user", "model", "user"]  # assistant → model

    async def test_gemini_without_article_no_config(self):
        from app.services.ai_service import chat_with_article
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(
            return_value=make_gemini_response("answer"))
        await chat_with_article(
            messages=[{"role": "user", "content": "Q"}],
            article_content=None,
            client=client, provider="gemini", model="gemini-2.0-flash",
        )
        call_kwargs = client.aio.models.generate_content.call_args.kwargs
        assert call_kwargs["config"] is None

    async def test_unknown_provider_raises(self):
        from app.services.ai_service import chat_with_article
        with pytest.raises(ValueError, match="Unknown provider"):
            await chat_with_article(
                messages=[{"role": "user", "content": "Q"}],
                article_content=None,
                client=MagicMock(), provider="unknown", model="x",
            )

    async def test_strips_whitespace_from_response(self):
        from app.services.ai_service import chat_with_article
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=make_anthropic_response("\n  Trimmed  \n"))
        result = await chat_with_article(
            messages=[{"role": "user", "content": "Q"}],
            article_content=None,
            client=client, provider="anthropic", model="claude-haiku-4-5",
        )
        assert result == "Trimmed"

    async def test_multi_turn_history_passed_through(self):
        from app.services.ai_service import chat_with_article
        client = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=make_anthropic_response("answer"))
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        await chat_with_article(
            messages=messages, article_content=None,
            client=client, provider="anthropic", model="claude-sonnet-4-6",
        )
        sent = client.messages.create.call_args.kwargs["messages"]
        assert len(sent) == 3
        assert sent[1]["role"] == "assistant"


# ── POST /htmx/articles/{id}/ai-chat ─────────────────────────────────────────

class TestHtmxAiChatEndpoint:
    def _setup_db(self, db, *, scalars, article=None):
        """
        scalars: list of return values for db.scalar() calls in order
                 (ai_on, settings, chat)
        article: value returned by _get_article_access (via db.execute)
        """
        db.scalar = AsyncMock(side_effect=scalars)
        db.execute = AsyncMock(return_value=make_execute_result(article))

    def test_ai_disabled_returns_disabled_message(self, client, mock_db):
        self._setup_db(mock_db, scalars=[False], article=None)
        resp = client.post("/htmx/articles/10/ai-chat", data={"message": "Hello"})
        assert resp.status_code == 200
        assert "AI is disabled" in resp.text

    def test_no_quality_model_returns_message(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings(ai_quality_provider=None, ai_quality_model=None)],
            article=None)
        resp = client.post("/htmx/articles/10/ai-chat", data={"message": "Hello"})
        assert resp.status_code == 200
        assert "not configured" in resp.text

    def test_chat_disabled_returns_403(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings(ai_chat_enabled=False)],
            article=None)
        resp = client.post("/htmx/articles/10/ai-chat", data={"message": "Hello"})
        assert resp.status_code == 403

    def test_empty_message_returns_400(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings()],
            article=None)
        resp = client.post("/htmx/articles/10/ai-chat", data={"message": "   "})
        assert resp.status_code == 400

    def test_article_not_found_returns_404(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings()],
            article=None)  # _get_article_access returns None
        resp = client.post("/htmx/articles/10/ai-chat", data={"message": "Hello"})
        assert resp.status_code == 404

    def test_successful_chat_returns_html_with_messages(self, client, mock_db):
        # chat=None → endpoint creates new ArticleAiChat (real class, no DB needed)
        self._setup_db(mock_db,
            scalars=[True, make_settings(), None],
            article=make_article())
        with (
            patch("app.services.ai_summary_service._normalize_content",
                  return_value="normalized content"),
            patch("app.services.ai_service.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.services.ai_service.chat_with_article",
                  new=AsyncMock(return_value="AI response")),
        ):
            resp = client.post(
                "/htmx/articles/10/ai-chat",
                data={"message": "What is this about?", "include_article": "on"},
            )
        assert resp.status_code == 200
        assert "What is this about?" in resp.text
        assert "AI response" in resp.text
        assert 'id="chat-area-10"' in resp.text

    def test_include_article_unchecked_passes_none_ctx(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings(), None],
            article=make_article())
        captured = {}
        async def fake_chat(messages, article_content, client, provider, model):
            captured["article_content"] = article_content
            return "answer"

        with (
            patch("app.services.ai_service.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-haiku-4-5"))),
            patch("app.services.ai_service.chat_with_article", side_effect=fake_chat),
        ):
            # include_article field absent → default "" → use_article=False
            resp = client.post("/htmx/articles/10/ai-chat", data={"message": "Hello"})
        assert resp.status_code == 200
        assert captured["article_content"] is None

    def test_trimming_keeps_max_10_messages(self, client, mock_db):
        existing = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"}
            for i in range(10)
        ]
        existing_chat = make_chat(messages=existing)
        self._setup_db(mock_db,
            scalars=[True, make_settings(), existing_chat],
            article=make_article())

        with (
            patch("app.services.ai_service.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.services.ai_service.chat_with_article",
                  new=AsyncMock(return_value="new answer")),
        ):
            resp = client.post("/htmx/articles/10/ai-chat", data={"message": "New question"})

        assert resp.status_code == 200
        # 10 existing + 1 user + 1 AI = 12 → trimmed to 10
        assert len(existing_chat.messages) == 10

    def test_ai_error_shows_error_without_losing_history(self, client, mock_db):
        existing = [
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "prev ans"},
        ]
        self._setup_db(mock_db,
            scalars=[True, make_settings(), make_chat(messages=existing)],
            article=make_article())
        with (
            patch("app.services.ai_service.get_ai_client",
                  new=AsyncMock(return_value=(AsyncMock(), "anthropic", "claude-sonnet-4-6"))),
            patch("app.services.ai_service.chat_with_article",
                  new=AsyncMock(side_effect=Exception("API error"))),
            patch("app.services.ai_summary_service._normalize_content",
                  return_value="content"),
        ):
            resp = client.post(
                "/htmx/articles/10/ai-chat",
                data={"message": "New Q", "include_article": "on"},
            )
        assert resp.status_code == 200
        assert "Chat failed" in resp.text
        assert "prev" in resp.text

    def test_fast_model_tier_used_when_requested(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings(), None],
            article=make_article())
        captured = {}
        async def fake_get_client(user_id, tier, db):
            captured["tier"] = tier
            return (AsyncMock(), "anthropic", "claude-haiku-4-5")

        with (
            patch("app.services.ai_service.get_ai_client", side_effect=fake_get_client),
            patch("app.services.ai_service.chat_with_article",
                  new=AsyncMock(return_value="answer")),
        ):
            resp = client.post(
                "/htmx/articles/10/ai-chat",
                data={"message": "Q", "model_tier": "fast"},
            )
        assert resp.status_code == 200
        assert captured["tier"] == "fast"

    def test_invalid_model_tier_falls_back_to_quality(self, client, mock_db):
        self._setup_db(mock_db,
            scalars=[True, make_settings(), None],
            article=make_article())
        captured = {}
        async def fake_get_client(user_id, tier, db):
            captured["tier"] = tier
            return (AsyncMock(), "anthropic", "claude-sonnet-4-6")

        with (
            patch("app.services.ai_service.get_ai_client", side_effect=fake_get_client),
            patch("app.services.ai_service.chat_with_article",
                  new=AsyncMock(return_value="answer")),
        ):
            resp = client.post(
                "/htmx/articles/10/ai-chat",
                data={"message": "Q", "model_tier": "malicious_value"},
            )
        assert resp.status_code == 200
        assert captured["tier"] == "quality"


# ── DELETE /htmx/articles/{id}/ai-chat ───────────────────────────────────────

class TestHtmxAiChatClear:
    def test_clears_existing_chat(self, client, mock_db):
        existing_chat = make_chat(messages=[
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
        ])
        mock_db.scalar = AsyncMock(return_value=existing_chat)
        resp = client.delete("/htmx/articles/10/ai-chat")
        assert resp.status_code == 200
        assert existing_chat.messages == []
        assert 'id="chat-area-10"' in resp.text

    def test_no_existing_chat_returns_empty_area(self, client, mock_db):
        mock_db.scalar = AsyncMock(return_value=None)
        resp = client.delete("/htmx/articles/10/ai-chat")
        assert resp.status_code == 200
        assert 'id="chat-area-10"' in resp.text
        mock_db.commit.assert_not_called()
