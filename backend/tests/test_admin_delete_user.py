"""admin_service.delete_user: which accounts an admin may delete.

The users table offers Delete only on a deactivated or unverified account that is
not an admin; the service holds the same line for a hand-made request.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.admin_service import delete_user

ADMIN_ID = 1


def _db(user):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db.execute = AsyncMock(return_value=result)
    db.delete = AsyncMock()
    return db


def _user(**kw):
    base = dict(id=2, role="user", is_active=False, email_verified=True)
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize("user", [
    None,
    _user(id=ADMIN_ID),
    _user(role="admin"),
    _user(is_active=True, email_verified=True),
])
async def test_refused(user):
    db = _db(user)
    with patch("app.services.feed.cleanup_user_feeds", new=AsyncMock()) as cleanup:
        assert await delete_user(db, 2, admin_id=ADMIN_ID) is False
    cleanup.assert_not_awaited()
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("user", [
    _user(is_active=False, email_verified=True),
    _user(is_active=True, email_verified=False),
])
async def test_deactivated_or_unverified_is_deleted(user):
    db = _db(user)
    with patch("app.services.feed.cleanup_user_feeds", new=AsyncMock()) as cleanup:
        assert await delete_user(db, 2, admin_id=ADMIN_ID) is True
    cleanup.assert_awaited_once_with(2, db)
    db.delete.assert_awaited_once_with(user)
    db.commit.assert_awaited_once()
