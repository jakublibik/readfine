"""last_active_at follows what a reader does, not what an open tab does on its own.

It used to be written only on login and on GET /app, so an installed PWA or a tab left
open for days made an active reader look gone: the admin table showed a stale date and
the automatic profile's cutoff skipped them."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.user import touch_last_active


def _user(last_active_at):
    return SimpleNamespace(id=1, last_active_at=last_active_at)


class TestTouchLastActive:
    async def test_never_active_is_written(self):
        user, db = _user(None), AsyncMock()
        await touch_last_active(user, db)
        assert user.last_active_at is not None
        db.commit.assert_awaited_once()

    async def test_within_the_hour_is_left_alone(self):
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        user, db = _user(recent), AsyncMock()
        await touch_last_active(user, db)
        assert user.last_active_at == recent
        db.commit.assert_not_awaited()

    async def test_older_than_an_hour_is_written(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        user, db = _user(stale), AsyncMock()
        await touch_last_active(user, db)
        assert user.last_active_at > stale
        db.commit.assert_awaited_once()


class TestWhichRoutesTouch:
    """Reader actions touch it; polling does not, or an idle open tab counts as use."""

    def test_scroll_batch_touches(self, client):
        with patch("app.routers.web.app.articles.touch_last_active", new=AsyncMock()) as touch, \
             patch("app.routers.web.app.articles.mark_articles_read_batch", new=AsyncMock()):
            resp = client.post("/htmx/articles/set-read-batch", json={"ids": [1, 2]})
        assert resp.status_code == 200
        touch.assert_awaited_once()

    def test_api_patch_touches(self, client):
        with patch("app.routers.api.v1.articles.touch_last_active", new=AsyncMock()) as touch, \
             patch("app.routers.api.v1.articles.update_article_state",
                   new=AsyncMock(return_value=None)):
            client.patch("/api/v1/articles/1", json={"is_read": True})
        touch.assert_awaited_once()

    def test_readable_poll_does_not_touch(self, client):
        with patch("app.routers.web.app.articles.touch_last_active", new=AsyncMock()) as touch, \
             patch("app.routers.web.app.articles.get_article", new=AsyncMock(return_value=None)):
            client.get("/htmx/articles/10/readable-poll")
        touch.assert_not_awaited()

    def test_api_list_does_not_touch(self, client):
        with patch("app.routers.api.v1.articles.touch_last_active", new=AsyncMock()) as touch, \
             patch("app.routers.api.v1.articles.list_articles", new=AsyncMock(return_value=[])):
            client.get("/api/v1/articles")
        touch.assert_not_awaited()


class TestJwtTouches:
    """A client signed in with a password (JWT) has no API token whose last use would
    count as activity, so each authenticated call records it (hourly, like the web)."""

    async def test_jwt_request_records_activity(self):
        from fastapi.security import HTTPAuthorizationCredentials

        from app.auth.dependencies import get_api_user
        from app.auth.security import create_access_token

        stale = datetime.now(timezone.utc) - timedelta(days=3)
        user = SimpleNamespace(id=1, role="user", is_active=True, session_token_version=0,
                               last_active_at=stale)
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=create_access_token(1, "user", token_version=0))
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            assert await get_api_user(credentials=creds, db=AsyncMock()) is user
        assert user.last_active_at > stale
