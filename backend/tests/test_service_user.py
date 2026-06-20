"""Unit tests for seed_first_admin (first-run admin bootstrap)."""
from unittest.mock import AsyncMock, MagicMock

from app.services.user import seed_first_admin


def _result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _make_db(existing_user=None, existing_settings=None):
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    # Two scalar queries: first checks for any user, second checks AppSettings(id=1)
    db.execute = AsyncMock(side_effect=[_result(existing_user), _result(existing_settings)])
    return db


class TestSeedFirstAdmin:
    async def test_creates_admin_when_no_users(self):
        db = _make_db(existing_user=None, existing_settings=None)
        await seed_first_admin(db, "admin@example.com", "secret-pw")

        # Admin user + UserSettings + AppSettings all added, then committed.
        added = [c.args[0] for c in db.add.call_args_list]
        roles = [getattr(o, "role", None) for o in added]
        assert "admin" in roles
        admin = next(o for o in added if getattr(o, "role", None) == "admin")
        assert admin.email == "admin@example.com"
        assert admin.password_hash != "secret-pw"  # hashed, never stored plaintext
        assert admin.password_hash
        db.commit.assert_awaited_once()

    async def test_noop_when_a_user_already_exists(self):
        existing = MagicMock()
        db = _make_db(existing_user=existing)
        await seed_first_admin(db, "admin@example.com", "secret-pw")

        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_skips_app_settings_when_already_present(self):
        db = _make_db(existing_user=None, existing_settings=MagicMock())
        await seed_first_admin(db, "admin@example.com", "secret-pw")

        added = [c.args[0] for c in db.add.call_args_list]
        # admin User + UserSettings added, but no second AppSettings row
        from app.models.settings import AppSettings
        assert not any(isinstance(o, AppSettings) for o in added)
        db.commit.assert_awaited_once()
