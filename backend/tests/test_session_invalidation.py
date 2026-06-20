"""Tests for session_token_version invalidation across session cookies and JWTs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.dependencies import get_api_user, get_current_user
from app.auth.security import create_access_token


def _user(tv=0):
    return SimpleNamespace(id=1, role="user", is_active=True, session_token_version=tv)


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
            result = await get_current_user(request=request, credentials=None, db=AsyncMock())
        assert result is user

    async def test_stale_tv_rejected(self):
        user = _user(tv=2)
        request = SimpleNamespace(session={"user_id": 1, "tv": 1})  # old session
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            with pytest.raises(HTTPException) as exc:
                await get_current_user(request=request, credentials=None, db=_empty_db())
        assert exc.value.status_code == 401

    async def test_missing_tv_defaults_to_zero(self):
        """Pre-deploy sessions without 'tv' survive while version is still 0."""
        user = _user(tv=0)
        request = SimpleNamespace(session={"user_id": 1})
        with patch("app.auth.dependencies._get_user_by_id", new=AsyncMock(return_value=user)):
            result = await get_current_user(request=request, credentials=None, db=AsyncMock())
        assert result is user
