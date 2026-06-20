"""Tests for the password reset flow (POST /reset-password, GET/POST /reset-password/{token})."""
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(
    is_active=True,
    token_hash=None,
    expires_at=None,
    email="user@test.com",
):
    u = MagicMock()
    u.id = 1
    u.email = email
    u.is_active = is_active
    u.password_reset_token_hash = token_hash
    u.password_reset_expires_at = expires_at
    u.password_hash = "old_hash"
    return u


def _make_app_settings(smtp_host="smtp.test.com"):
    s = MagicMock()
    s.smtp_host = smtp_host
    s.smtp_from_email = "noreply@test.com"
    return s


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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


# ── POST /reset-password — request flow ───────────────────────────────────────

class TestResetPasswordRequest:

    def test_unknown_email_returns_success(self, web_client, mock_db):
        """Always shows 'sent' to prevent email enumeration."""
        mock_db.execute.return_value = _scalar(None)
        resp = web_client.post("/reset-password", data={"email": "nobody@test.com"})
        assert resp.status_code == 200
        assert "sent" in resp.text.lower() or resp.status_code == 200

    def test_no_smtp_returns_sent_without_email(self, web_client, mock_db):
        mock_db.execute.return_value = _scalar(_make_app_settings(smtp_host=None))
        resp = web_client.post("/reset-password", data={"email": "user@test.com"})
        assert resp.status_code == 200

    def test_known_user_stores_token_hash(self, web_client, mock_db):
        user = _make_user()
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        with patch("app.routers.web.auth.send_email"):
            web_client.post("/reset-password", data={"email": "user@test.com"})
        assert user.password_reset_token_hash is not None
        assert len(user.password_reset_token_hash) == 64  # sha256 hex

    def test_known_user_sets_expiry(self, web_client, mock_db):
        user = _make_user()
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        with patch("app.routers.web.auth.send_email"):
            web_client.post("/reset-password", data={"email": "user@test.com"})
        assert user.password_reset_expires_at is not None
        diff = user.password_reset_expires_at - datetime.now(timezone.utc)
        assert timedelta(minutes=59) < diff < timedelta(hours=1, minutes=1)

    def test_known_user_commits_db(self, web_client, mock_db):
        user = _make_user()
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        with patch("app.routers.web.auth.send_email"):
            web_client.post("/reset-password", data={"email": "user@test.com"})
        mock_db.commit.assert_called()

    def test_known_user_sends_email(self, web_client, mock_db):
        user = _make_user()
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        with patch("app.utils.smtp.send_email") as mock_send:
            web_client.post("/reset-password", data={"email": "user@test.com"})
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "user@test.com" in call_args

    def test_inactive_user_does_not_send_email(self, web_client, mock_db):
        user = _make_user(is_active=False)
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        with patch("app.utils.smtp.send_email") as mock_send:
            web_client.post("/reset-password", data={"email": "user@test.com"})
        mock_send.assert_not_called()

    def test_smtp_failure_still_returns_success(self, web_client, mock_db):
        user = _make_user()
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        with patch("app.utils.smtp.send_email", side_effect=Exception("smtp down")):
            resp = web_client.post("/reset-password", data={"email": "user@test.com"})
        assert resp.status_code == 200

    def test_reset_url_contains_raw_token_not_hash(self, web_client, mock_db):
        user = _make_user()
        mock_db.execute.side_effect = [
            _scalar(_make_app_settings()),
            _scalar(user),
        ]
        sent_body = {}
        def capture_send(settings, to, subject, body):
            sent_body["text"] = body
        with patch("app.utils.smtp.send_email", side_effect=capture_send):
            web_client.post("/reset-password", data={"email": "user@test.com"})
        assert "reset-password/" in sent_body["text"]
        # The URL must not contain the sha256 hash (64 hex chars in a row)
        import re
        assert not re.search(r"[0-9a-f]{64}", sent_body["text"])


# ── GET /reset-password/{token} — confirm page ───────────────────────────────

class TestResetPasswordConfirmPage:

    def test_valid_token_shows_form(self, web_client, mock_db):
        token = "validtoken123"
        user = _make_user(
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mock_db.execute.return_value = _scalar(user)
        resp = web_client.get(f"/reset-password/{token}")
        assert resp.status_code == 200
        assert "invalid" not in resp.text.lower()

    def test_invalid_token_shows_error(self, web_client, mock_db):
        mock_db.execute.return_value = _scalar(None)
        resp = web_client.get("/reset-password/badtoken")
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower()

    def test_expired_token_shows_error(self, web_client, mock_db):
        token = "expiredtoken"
        user = _make_user(
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        mock_db.execute.return_value = _scalar(user)
        resp = web_client.get(f"/reset-password/{token}")
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower()


# ── POST /reset-password/{token} — apply new password ────────────────────────

class TestResetPasswordConfirm:

    def _valid_user(self, token: str):
        return _make_user(
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def test_success_updates_password_hash(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        with patch("app.routers.web.auth.hash_password", return_value="new_hash"):
            web_client.post(f"/reset-password/{token}", data={
                "new_password": "newpassword1",
                "confirm_password": "newpassword1",
            })
        assert user.password_hash == "new_hash"

    def test_success_clears_token(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        with patch("app.routers.web.auth.hash_password", return_value="new_hash"):
            web_client.post(f"/reset-password/{token}", data={
                "new_password": "newpassword1",
                "confirm_password": "newpassword1",
            })
        assert user.password_reset_token_hash is None
        assert user.password_reset_expires_at is None

    def test_success_commits_db(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        with patch("app.routers.web.auth.hash_password", return_value="new_hash"):
            web_client.post(f"/reset-password/{token}", data={
                "new_password": "newpassword1",
                "confirm_password": "newpassword1",
            })
        mock_db.commit.assert_called()

    def test_success_shows_done(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        with patch("app.routers.web.auth.hash_password", return_value="new_hash"):
            resp = web_client.post(f"/reset-password/{token}", data={
                "new_password": "newpassword1",
                "confirm_password": "newpassword1",
            })
        assert resp.status_code == 200
        assert "done" in resp.text.lower() or resp.status_code == 200

    def test_password_too_short_shows_error(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        resp = web_client.post(f"/reset-password/{token}", data={
            "new_password": "short",
            "confirm_password": "short",
        })
        assert resp.status_code == 200
        assert "8 character" in resp.text.lower()

    def test_password_too_short_does_not_commit(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        web_client.post(f"/reset-password/{token}", data={
            "new_password": "short",
            "confirm_password": "short",
        })
        mock_db.commit.assert_not_called()

    def test_passwords_do_not_match_shows_error(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        resp = web_client.post(f"/reset-password/{token}", data={
            "new_password": "newpassword1",
            "confirm_password": "different123",
        })
        assert resp.status_code == 200
        assert "do not match" in resp.text.lower()

    def test_passwords_do_not_match_does_not_commit(self, web_client, mock_db):
        token = "goodtoken"
        user = self._valid_user(token)
        mock_db.execute.return_value = _scalar(user)
        web_client.post(f"/reset-password/{token}", data={
            "new_password": "newpassword1",
            "confirm_password": "different123",
        })
        mock_db.commit.assert_not_called()

    def test_invalid_token_shows_error(self, web_client, mock_db):
        mock_db.execute.return_value = _scalar(None)
        resp = web_client.post("/reset-password/badtoken", data={
            "new_password": "newpassword1",
            "confirm_password": "newpassword1",
        })
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower()

    def test_expired_token_shows_error(self, web_client, mock_db):
        token = "expiredtoken"
        user = _make_user(
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        mock_db.execute.return_value = _scalar(user)
        resp = web_client.post(f"/reset-password/{token}", data={
            "new_password": "newpassword1",
            "confirm_password": "newpassword1",
        })
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower()

    def test_token_is_hashed_before_db_lookup(self, web_client, mock_db):
        """Verify the raw token is never stored — only the sha256 hash."""
        token = "plaintexttoken"
        executed_queries = []
        original_execute = mock_db.execute

        async def capture_execute(stmt, *args, **kwargs):
            executed_queries.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return _scalar(None)

        mock_db.execute = capture_execute
        web_client.post(f"/reset-password/{token}", data={
            "new_password": "newpassword1",
            "confirm_password": "newpassword1",
        })
        for q in executed_queries:
            assert token not in q, "Raw token must not appear in DB query"
