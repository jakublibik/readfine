"""API tests for /api/v1/articles — service function delegation."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest


def _make_article_item(**kwargs):
    defaults = dict(
        id=1,
        feed_id=1,
        feed_title="Tech Blog",
        url="http://example.com/article",
        title="Test Article",
        author="Author",
        summary="A summary",
        snippet="snippet text",
        published_at=datetime(2024, 1, 15, 10, 0),
        formatted_date="10:00",
        estimated_read_min=3,
        image_url=None,
        is_read=False,
        is_starred=False,
        is_archived=False,
        labels=[],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_article_response(**kwargs):
    defaults = dict(
        id=1,
        feed_id=1,
        feed_title="Tech Blog",
        url="http://example.com/article",
        title="Test Article",
        author="Author",
        content="<p>Content</p>",
        content_source="feed_full",
        readable_content=None,
        readable_status="pending",
        published_at=datetime(2024, 1, 15, 10, 0),
        estimated_read_min=3,
        word_count=100,
        image_url=None,
        is_read=False,
        is_starred=False,
        is_archived=False,
        read_at=None,
        share_token=None,
        labels=[],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestListArticles:
    def test_returns_list(self, client):
        articles = [_make_article_item(), _make_article_item(id=2, title="Second")]
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=articles)):
            response = client.get("/api/v1/articles")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_empty_list(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])):
            response = client.get("/api/v1/articles")
        assert response.status_code == 200
        assert response.json() == []

    def test_passes_unread_only_param(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])) as mock:
            client.get("/api/v1/articles?unread_only=true")
        mock.assert_awaited_once()
        _, kwargs = mock.call_args
        assert kwargs["unread_only"] is True

    def test_passes_starred_only_param(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])) as mock:
            client.get("/api/v1/articles?starred_only=true")
        mock.assert_awaited_once()
        _, kwargs = mock.call_args
        assert kwargs["starred_only"] is True

    def test_passes_feed_id_param(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])) as mock:
            client.get("/api/v1/articles?feed_id=5")
        _, kwargs = mock.call_args
        assert kwargs["feed_id"] == 5

    def test_passes_search_param(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])) as mock:
            client.get("/api/v1/articles?q=python")
        _, kwargs = mock.call_args
        assert kwargs["q"] == "python"

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/articles")
        assert response.status_code == 401

    def test_limit_ge_1(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])):
            response = client.get("/api/v1/articles?limit=0")
        assert response.status_code == 422

    def test_limit_le_200(self, client):
        with patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])):
            response = client.get("/api/v1/articles?limit=201")
        assert response.status_code == 422


class TestGetArticle:
    def test_returns_article(self, client):
        article = _make_article_response()
        with patch("app.routers.api.v1.articles.get_article", new=AsyncMock(return_value=article)):
            response = client.get("/api/v1/articles/1")
        assert response.status_code == 200
        assert response.json()["id"] == 1

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.articles.get_article", new=AsyncMock(return_value=None)):
            response = client.get("/api/v1/articles/999")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/articles/1")
        assert response.status_code == 401


class TestPatchArticle:
    def test_mark_read(self, client):
        article = _make_article_response(is_read=True)
        with patch("app.routers.api.v1.articles.update_article_state", new=AsyncMock(return_value=article)):
            response = client.patch("/api/v1/articles/1", json={"is_read": True})
        assert response.status_code == 200
        assert response.json()["is_read"] is True

    def test_mark_starred(self, client):
        article = _make_article_response(is_starred=True)
        with patch("app.routers.api.v1.articles.update_article_state", new=AsyncMock(return_value=article)):
            response = client.patch("/api/v1/articles/1", json={"is_starred": True})
        assert response.status_code == 200

    def test_mark_archived(self, client):
        article = _make_article_response(is_archived=True)
        with patch("app.routers.api.v1.articles.update_article_state", new=AsyncMock(return_value=article)):
            response = client.patch("/api/v1/articles/1", json={"is_archived": True})
        assert response.status_code == 200

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.articles.update_article_state", new=AsyncMock(return_value=None)):
            response = client.patch("/api/v1/articles/999", json={"is_read": True})
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.patch("/api/v1/articles/1", json={"is_read": True})
        assert response.status_code == 401

    def test_empty_patch_body_accepted(self, client):
        article = _make_article_response()
        with patch("app.routers.api.v1.articles.update_article_state", new=AsyncMock(return_value=article)):
            response = client.patch("/api/v1/articles/1", json={})
        assert response.status_code == 200

    def test_unsave(self, client):
        """The counterpart of POST /save-url: the API can take an article back out
        of Saved, not only put it in."""
        article = _make_article_response(is_saved=False)
        with patch("app.routers.api.v1.articles.update_article_state",
                   new=AsyncMock(return_value=article)) as mock:
            response = client.patch("/api/v1/articles/1", json={"is_saved": False})
        assert response.status_code == 200
        assert mock.call_args.args[2].is_saved is False


class TestSaveUrl:
    """POST /api/v1/articles/save-url — save-by-URL for scripts and integrations."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        # The endpoint shares the web form's 10/minute, and slowapi's storage lives
        # for the whole test process, so without this the class rate-limits itself.
        from app.rate_limit import limiter
        limiter._storage.reset()

    def test_new_article_returns_201(self, client):
        saved = _make_article_response(id=7, feed_id=None, readable_status="pending",
                                       title="example.com/story", is_saved=True)
        with patch("app.routers.api.v1.articles.save_article_by_url",
                   new=AsyncMock(return_value=(SimpleNamespace(id=7), False))), \
             patch("app.routers.api.v1.articles.get_article", new=AsyncMock(return_value=saved)):
            response = client.post("/api/v1/articles/save-url",
                                   json={"url": "https://example.com/story"})
        assert response.status_code == 201
        assert response.json()["id"] == 7
        assert response.json()["readable_status"] == "pending"

    def test_already_known_returns_200(self, client):
        """A link you already have is attached to Saved, not duplicated. That is a
        success with a different status code, not a 409."""
        saved = _make_article_response(id=7, is_saved=True)
        with patch("app.routers.api.v1.articles.save_article_by_url",
                   new=AsyncMock(return_value=(SimpleNamespace(id=7), True))), \
             patch("app.routers.api.v1.articles.get_article", new=AsyncMock(return_value=saved)):
            response = client.post("/api/v1/articles/save-url",
                                   json={"url": "https://example.com/story"})
        assert response.status_code == 200
        assert response.json()["id"] == 7

    def test_unfetchable_url_returns_400(self, client):
        with patch("app.routers.api.v1.articles.save_article_by_url",
                   new=AsyncMock(side_effect=ValueError("URL resolves to a private address"))):
            response = client.post("/api/v1/articles/save-url",
                                   json={"url": "http://127.0.0.1/x"})
        assert response.status_code == 400
        assert "private address" in response.json()["detail"]

    def test_url_is_trimmed(self, client):
        saved = _make_article_response(id=7)
        with patch("app.routers.api.v1.articles.save_article_by_url",
                   new=AsyncMock(return_value=(SimpleNamespace(id=7), False))) as mock, \
             patch("app.routers.api.v1.articles.get_article", new=AsyncMock(return_value=saved)):
            client.post("/api/v1/articles/save-url",
                        json={"url": "  https://example.com/story  "})
        assert mock.call_args.args[0] == "https://example.com/story"

    def test_empty_url_returns_422(self, client):
        response = client.post("/api/v1/articles/save-url", json={"url": "   "})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/articles/save-url",
                                      json={"url": "https://example.com/story"})
        assert response.status_code == 401
