"""Tests for the email-change verification flow and password-reset version bump."""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _make_app_settings(smtp_host="smtp.test.com"):
    s = MagicMock()
    s.smtp_host = smtp_host
    s.smtp_from_email = "noreply@test.com"
    return s


def _make_user(**kwargs):
    u = MagicMock()
    u.id = 1
    u.email = "old@test.com"
    u.password_hash = "old_hash"
    u.session_token_version = 0
    u.pending_email = None
    u.pending_email_token_hash = None
    u.pending_email_expires_at = None
    for k, v in kwargs.items():
        setattr(u, k, v)
    return u


@pytest.fixture
def web_client(mock_db):
    from app.main import app
    from app.database import get_db
    from app.rate_limit import limiter as _rate_limiter

    _rate_limiter._storage.reset()
    mock_db.execute.return_value = _scalar(None)

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── GET /verify-email-change ──────────────────────────────────────────────────

class TestVerifyEmailChange:
    def test_valid_token_switches_email(self, web_client, mock_db):
        token = "goodtoken"
        user = _make_user(
            pending_email="new@test.com",
            pending_email_token_hash=_token_hash(token),
            pending_email_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        # 1st execute: lookup by token hash → user; 2nd: taken check → None
        mock_db.execute.side_effect = [_scalar(user), _scalar(None)]
        resp = web_client.get(f"/verify-email-change?token={token}")
        assert resp.status_code == 200
        assert user.email == "new@test.com"
        assert user.pending_email is None
        assert user.pending_email_token_hash is None
        mock_db.commit.assert_called()

    def test_invalid_token_rejected(self, web_client, mock_db):
        mock_db.execute.return_value = _scalar(None)
        resp = web_client.get("/verify-email-change?token=bad")
        assert resp.status_code == 400

    def test_missing_token_rejected(self, web_client, mock_db):
        resp = web_client.get("/verify-email-change")
        assert resp.status_code == 400

    def test_expired_token_rejected(self, web_client, mock_db):
        token = "expiredtoken"
        user = _make_user(
            pending_email="new@test.com",
            pending_email_token_hash=_token_hash(token),
            pending_email_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        mock_db.execute.return_value = _scalar(user)
        resp = web_client.get(f"/verify-email-change?token={token}")
        assert resp.status_code == 400
        assert user.email == "old@test.com"  # unchanged

    def test_taken_email_rejected_and_cleared(self, web_client, mock_db):
        token = "goodtoken"
        user = _make_user(
            pending_email="taken@test.com",
            pending_email_token_hash=_token_hash(token),
            pending_email_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        other = _make_user(id=2, email="taken@test.com")
        mock_db.execute.side_effect = [_scalar(user), _scalar(other)]
        resp = web_client.get(f"/verify-email-change?token={token}")
        assert resp.status_code == 400
        assert user.email == "old@test.com"  # not switched
        assert user.pending_email is None     # pending cleared


# ── POST /settings/profile/email — pending flow ───────────────────────────────

class TestSettingsProfileEmailPending:
    @pytest.fixture
    def auth_client(self, mock_db):
        from app.main import app
        from app.database import get_db
        from app.auth.dependencies import get_current_user
        from app.rate_limit import limiter as _rate_limiter

        _rate_limiter._storage.reset()
        self.user = _make_user()

        async def override_get_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c
        app.dependency_overrides.clear()

    def test_smtp_configured_sets_pending_not_email(self, auth_client, mock_db):
        # execute: existing-email check → None ; scalar: AppSettings with SMTP
        mock_db.execute.return_value = _scalar(None)
        mock_db.scalar = AsyncMock(return_value=_make_app_settings())
        with patch("app.routers.web.settings.profile.verify_password", return_value=True), \
             patch("app.routers.web.settings.profile.send_email"):
            resp = auth_client.post("/settings/profile/email", data={
                "email": "new@test.com",
                "current_password": "pw",
            })
        assert resp.status_code == 200
        assert self.user.email == "old@test.com"          # NOT changed yet
        assert self.user.pending_email == "new@test.com"  # pending set
        assert self.user.pending_email_token_hash is not None
        mock_db.commit.assert_called()

    def test_malformed_email_rejected(self, auth_client, mock_db):
        resp = auth_client.post("/settings/profile/email", data={
            "email": "notanemail",
            "current_password": "pw",
        })
        assert resp.status_code == 200
        assert "valid email" in resp.text
        assert self.user.email == "old@test.com"      # unchanged
        assert self.user.pending_email is None

    def test_email_with_newline_rejected(self, auth_client, mock_db):
        # SMTP header injection attempt — the new address is sent as a To: header.
        resp = auth_client.post("/settings/profile/email", data={
            "email": "a@b.com\nBcc: evil@x.com",
            "current_password": "pw",
        })
        assert resp.status_code == 200
        assert "valid email" in resp.text
        assert self.user.email == "old@test.com"
        assert self.user.pending_email is None

    def test_no_smtp_changes_email_immediately(self, auth_client, mock_db):
        mock_db.execute.return_value = _scalar(None)
        mock_db.scalar = AsyncMock(return_value=_make_app_settings(smtp_host=None))
        with patch("app.routers.web.settings.profile.verify_password", return_value=True):
            resp = auth_client.post("/settings/profile/email", data={
                "email": "new@test.com",
                "current_password": "pw",
            })
        assert resp.status_code == 200
        assert self.user.email == "new@test.com"


# ── Password reset bumps session_token_version ────────────────────────────────

class TestResetBumpsTokenVersion:
    def test_reset_confirm_increments_version(self, web_client, mock_db):
        token = "resettoken"
        user = _make_user(
            password_reset_token_hash=_token_hash(token),
            password_reset_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            session_token_version=3,
        )
        mock_db.execute.return_value = _scalar(user)
        with patch("app.routers.web.auth.hash_password", return_value="new_hash"):
            web_client.post(f"/reset-password/{token}", data={
                "new_password": "newpassword1",
                "confirm_password": "newpassword1",
            })
        assert user.session_token_version == 4
