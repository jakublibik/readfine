"""Bearer auth with an API token: last_used_at is written at most hourly."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.security import HTTPAuthorizationCredentials

from app.auth.dependencies import _auth_by_bearer
from tests.conftest import make_mock_db, make_scalar_result


def _db_with(api_token):
    user = SimpleNamespace(id=1, role="user", settings=None)
    db = make_mock_db()
    db.execute.side_effect = [make_scalar_result(api_token), make_scalar_result(user)]
    return db


async def _auth(db):
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="rf_not_a_jwt")
    return await _auth_by_bearer(creds, db)


async def test_first_use_is_recorded():
    token = MagicMock(user_id=1, last_used_at=None)
    db = _db_with(token)
    assert await _auth(db) is not None
    assert token.last_used_at is not None
    db.commit.assert_awaited_once()


async def test_use_within_the_hour_writes_nothing():
    # A sync client polling every minute must not commit on every request.
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    token = MagicMock(user_id=1, last_used_at=recent)
    db = _db_with(token)
    assert await _auth(db) is not None
    assert token.last_used_at == recent
    db.commit.assert_not_awaited()


async def test_use_after_the_hour_is_recorded():
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    token = MagicMock(user_id=1, last_used_at=old)
    db = _db_with(token)
    assert await _auth(db) is not None
    assert token.last_used_at > old
    db.commit.assert_awaited_once()
