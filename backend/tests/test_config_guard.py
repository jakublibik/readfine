"""Tests for the production insecure-config guard in app.config.Settings.

Security-critical: a deploy running with the .env.example placeholder keys has
forgeable sessions/JWTs and decryptable stored secrets. The guard must refuse to
boot in production (debug=False) and stay out of the way in local dev (debug=True).
"""
import pytest

from app.config import Settings

_GOOD_SECRET = "a" * 64
_GOOD_ENCRYPTION = "b" * 32


def _make(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x/y",
        secret_key=_GOOD_SECRET,
        encryption_key=_GOOD_ENCRYPTION,
    )
    base.update(overrides)
    # _env_file=None so the developer's real .env never influences the test.
    return Settings(_env_file=None, **base)


def test_accepts_strong_keys_in_production():
    s = _make(debug=False)
    assert s.debug is False


def test_rejects_placeholder_secret_key():
    with pytest.raises(ValueError, match="SECRET_KEY"):
        _make(debug=False, secret_key="change-me")


def test_rejects_short_secret_key():
    with pytest.raises(ValueError, match="too short"):
        _make(debug=False, secret_key="a" * 31)


def test_rejects_placeholder_encryption_key():
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        _make(debug=False, encryption_key="changemechangemechangemechangeme")


def test_rejects_placeholder_admin_password():
    with pytest.raises(ValueError, match="FIRST_ADMIN_PASSWORD"):
        _make(debug=False, first_admin_password="change-me")


def test_debug_mode_bypasses_all_checks():
    s = _make(
        debug=True,
        secret_key="change-me",
        encryption_key="changemechangemechangemechangeme",
        first_admin_password="change-me",
    )
    assert s.debug is True
