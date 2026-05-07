"""API tests for /api/v1/feeds."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


def _make_feed(id=1, **kwargs):
    defaults = dict(
        id=id,
        feed_url="https://example.com/feed.xml",
        site_url="https://example.com",
        title="Example Feed",
        favicon_url=None,
        status="active",
        last_fetched_at=None,
        last_error=None,
        subscriber_count=1,
        feed_type="rss",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_user_feed(id=1, feed_id=1, **kwargs):
    defaults = dict(
        id=id,
        feed_id=feed_id,
        folder_id=None,
        custom_title=None,
        extract_readable=False,
        unread_count=0,
        position=0,
        created_at=datetime(2024, 1, 1),
        feed=_make_feed(id=feed_id),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _scalar_result(value, one=False):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalar_one.return_value = value
    r.scalars.return_value.all.return_value = value if isinstance(value, list) else []
    return r


class TestListFeeds:
    def test_returns_list(self, client):
        feeds = [_make_user_feed(id=1), _make_user_feed(id=2, feed_id=2)]
        with patch("app.routers.api.v1.feeds.list_user_feeds", new=AsyncMock(return_value=feeds)):
            response = client.get("/api/v1/feeds")
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_empty_list(self, client):
        with patch("app.routers.api.v1.feeds.list_user_feeds", new=AsyncMock(return_value=[])):
            response = client.get("/api/v1/feeds")
        assert response.status_code == 200
        assert response.json() == []

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/feeds")
        assert response.status_code == 401


class TestSubscribeFeed:
    def test_valid_url_returns_201(self, client, mock_db):
        user_feed = _make_user_feed()
        # subscribe() returns UserFeed, then db.execute returns it again
        mock_result = _scalar_result(user_feed)
        mock_result.scalar_one.return_value = user_feed
        mock_db.execute.return_value = mock_result

        with patch("app.routers.api.v1.feeds.subscribe", new=AsyncMock(return_value=user_feed)):
            response = client.post(
                "/api/v1/feeds",
                json={"url": "https://example.com/feed.xml"},
            )
        assert response.status_code == 201

    def test_already_subscribed_returns_409(self, client, mock_db):
        with patch("app.routers.api.v1.feeds.subscribe", side_effect=ValueError("already subscribed")):
            response = client.post(
                "/api/v1/feeds",
                json={"url": "https://example.com/feed.xml"},
            )
        assert response.status_code == 409

    def test_invalid_url_returns_400(self, client, mock_db):
        with patch("app.routers.api.v1.feeds.subscribe", side_effect=ValueError("SSRF blocked")):
            response = client.post(
                "/api/v1/feeds",
                json={"url": "http://192.168.1.1/feed.xml"},
            )
        assert response.status_code == 400

    def test_empty_url_returns_422(self, client, mock_db):
        response = client.post("/api/v1/feeds", json={"url": ""})
        assert response.status_code == 422

    def test_missing_url_returns_422(self, client, mock_db):
        response = client.post("/api/v1/feeds", json={})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/feeds", json={"url": "https://example.com/feed.xml"})
        assert response.status_code == 401


class TestGetFeed:
    def test_own_feed_returns_200(self, client, mock_db):
        user_feed = _make_user_feed()
        mock_db.execute.return_value = _scalar_result(user_feed)

        response = client.get("/api/v1/feeds/1")
        assert response.status_code == 200

    def test_not_found_returns_404(self, client, mock_db):
        mock_db.execute.return_value = _scalar_result(None)
        response = client.get("/api/v1/feeds/99")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/feeds/1")
        assert response.status_code == 401


class TestUpdateFeed:
    def test_not_found_returns_404(self, client, mock_db):
        mock_db.execute.return_value = _scalar_result(None)
        response = client.patch("/api/v1/feeds/99", json={"custom_title": "New"})
        assert response.status_code == 404

    def test_updates_custom_title(self, client, mock_db):
        user_feed = _make_user_feed()
        mock_db.execute.return_value = _scalar_result(user_feed)

        async def set_attrs(obj):
            pass

        mock_db.refresh.side_effect = set_attrs

        response = client.patch("/api/v1/feeds/1", json={"custom_title": "My Blog"})
        assert response.status_code == 200
        assert user_feed.custom_title == "My Blog"

    def test_updates_extract_readable(self, client, mock_db):
        user_feed = _make_user_feed()
        mock_db.execute.return_value = _scalar_result(user_feed)
        mock_db.refresh.side_effect = AsyncMock()

        response = client.patch("/api/v1/feeds/1", json={"extract_readable": True})
        assert response.status_code == 200
        assert user_feed.extract_readable is True

    def test_requires_auth(self, unauth_client):
        response = unauth_client.patch("/api/v1/feeds/1", json={"custom_title": "X"})
        assert response.status_code == 401


class TestUnsubscribeFeed:
    def test_unsubscribes_successfully(self, client, mock_db):
        with patch("app.routers.api.v1.feeds.unsubscribe", new=AsyncMock(return_value=None)):
            response = client.delete("/api/v1/feeds/1")
        assert response.status_code == 204

    def test_not_found_returns_404(self, client, mock_db):
        with patch("app.routers.api.v1.feeds.unsubscribe", side_effect=ValueError("not found")):
            response = client.delete("/api/v1/feeds/99")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.delete("/api/v1/feeds/1")
        assert response.status_code == 401
