"""Unit tests for the public registration_enabled in-process cache."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.app_settings_cache import (
    get_registration_enabled,
    invalidate_registration_cache,
)


def _db_returning(value):
    db = AsyncMock()
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    db.execute = AsyncMock(return_value=r)
    return db


@pytest.fixture(autouse=True)
def _reset_cache():
    # Module-level cache leaks across tests; clear before and after each.
    invalidate_registration_cache()
    yield
    invalidate_registration_cache()


class TestRegistrationCache:
    async def test_loads_once_then_serves_from_cache(self):
        db = _db_returning(True)
        assert await get_registration_enabled(db) is True
        assert await get_registration_enabled(db) is True
        db.execute.assert_awaited_once()  # second call hit the cache, not the DB

    async def test_missing_row_is_false(self):
        db = _db_returning(None)
        assert await get_registration_enabled(db) is False

    async def test_invalidation_forces_reload(self):
        db = _db_returning(False)
        assert await get_registration_enabled(db) is False

        invalidate_registration_cache()

        db2 = _db_returning(True)
        assert await get_registration_enabled(db2) is True
        db2.execute.assert_awaited_once()
