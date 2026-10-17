"""Tests for session_token_version invalidation across session cookies and JWTs."""
import json
from base64 import b64encode
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from itsdangerous import TimestampSigner

from app.auth.dependencies import get_api_user, get_current_user
from app.auth.security import create_access_token
from app.config import settings


def _user(tv=0):
    # Active just now, so a JWT request has no activity to record.
    return SimpleNamespace(id=1, role="user", is_active=True, session_token_version=tv,
                           last_active_at=datetime.now(timezone.utc))


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _empty_db():
    """DB whose ApiToken lookup returns nothing (forces fall-through to 401)."""
    db = AsyncMock()
    db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=None)
    )
    return db


# ── JWT branch (get_api_user) ─────────────────────────────────────────────────

class TestApiUserTokenVersion:
    async def test_matching_tv_accepted(self):
        user = _user(tv=3)
        token = create_access_token(1, "user", token_version=3)
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            result = await get_api_user(credentials=_creds(token), db=AsyncMock())
        assert result is user

    async def test_stale_tv_rejected(self):
        user = _user(tv=5)  # password changed → version bumped to 5
        token = create_access_token(1, "user", token_version=3)  # old token
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            with pytest.raises(HTTPException) as exc:
                await get_api_user(credentials=_creds(token), db=_empty_db())
        assert exc.value.status_code == 401


# ── Session branch (get_current_user) ─────────────────────────────────────────

class TestSessionTokenVersion:
    async def test_matching_tv_accepted(self):
        user = _user(tv=2)
        request = SimpleNamespace(session={"user_id": 1, "tv": 2})
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            result = await get_current_user(request=request, db=AsyncMock())
        assert result is user

    async def test_stale_tv_rejected(self):
        user = _user(tv=2)
        request = SimpleNamespace(session={"user_id": 1, "tv": 1})  # old session
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            with pytest.raises(HTTPException) as exc:
                await get_current_user(request=request, db=_empty_db())
        assert exc.value.status_code == 401
        assert request.session == {}

    async def test_unknown_user_clears_session(self):
        request = SimpleNamespace(session={"user_id": 99, "tv": 0})
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=None)):
            with pytest.raises(HTTPException):
                await get_current_user(request=request, db=_empty_db())
        assert request.session == {}

    def test_stale_session_does_not_loop_between_app_and_login(self, unauth_client):
        """/app used to send a dead session to /login, which saw user_id and sent it
        back to /app (ERR_TOO_MANY_REDIRECTS)."""
        signer = TimestampSigner(settings.secret_key)
        payload = b64encode(json.dumps({"user_id": 1, "tv": 1}).encode())
        # The domain httpx files response cookies under, so the server's delete lands.
        unauth_client.cookies.set("session", signer.sign(payload).decode(), domain="testserver.local")
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=_user(tv=2))):
            resp = unauth_client.get("/app", follow_redirects=False)
            assert resp.status_code == 302 and resp.headers["location"] == "/login"
            resp = unauth_client.get("/login", follow_redirects=False)
        assert resp.status_code == 200

    async def test_missing_tv_defaults_to_zero(self):
        """Pre-deploy sessions without 'tv' survive while version is still 0."""
        user = _user(tv=0)
        request = SimpleNamespace(session={"user_id": 1})
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            result = await get_current_user(request=request, db=AsyncMock())
        assert result is user


# ── Bearer on the web (H1-01) ─────────────────────────────────────────────────

class TestBearerNotAcceptedOnWeb:
    """A valid JWT works on the API and nowhere else: on the web it used to stand in
    for a session, admin panel and token minting included."""

    def _admin_jwt(self):
        return {"Authorization": f"Bearer {create_access_token(1, 'admin', token_version=0)}"}

    def test_admin_panel_refuses_bearer(self, unauth_client):
        admin = SimpleNamespace(id=1, role="admin", is_active=True, session_token_version=0,
                                last_active_at=datetime.now(timezone.utc), settings=None)
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=admin)):
            for path in ("/admin/users", "/settings/tokens"):
                resp = unauth_client.get(path, headers=self._admin_jwt(), follow_redirects=False)
                assert resp.status_code in (302, 303, 401), path

    def test_api_still_accepts_bearer(self, unauth_client):
        admin = SimpleNamespace(id=1, role="admin", is_active=True, session_token_version=0,
                                last_active_at=datetime.now(timezone.utc), settings=None,
                                email="a@example.com", name="A", created_at=datetime.now(timezone.utc))
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=admin)):
            resp = unauth_client.get("/api/v1/auth/me", headers=self._admin_jwt())
        assert resp.status_code != 401
