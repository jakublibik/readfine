"""Tests for web registration flow and email verification."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.utils.form_guard import HONEYPOT_FIELD, issue_form_ts


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(
    email_verified=True,
    token_hash=None,
    expires_at=None,
    email="new@test.com",
):
    u = MagicMock()
    u.id = 42
    u.email = email
    u.display_name = "Test"
    u.is_active = True
    u.email_verified = email_verified
    u.email_verification_token_hash = token_hash
    u.email_verification_expires_at = expires_at
    u.password_hash = "hashed"
    u.session_token_version = 0
    return u


def _make_app_settings(smtp_host=None, registration_enabled=True):
    s = MagicMock()
    s.smtp_host = smtp_host
    s.smtp_from_email = "noreply@test.com" if smtp_host else None
    s.registration_enabled = registration_enabled
    return s


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


@pytest.fixture
def web_client(mock_db):
    """Unauthenticated client for web (HTML) routes with rate limiter reset per test."""
    from app.main import app
    from app.database import get_db
    from app.rate_limit import limiter as _rate_limiter

    # Reset rate limiter storage — @limiter.limit() decorators are bound to this instance
    # at import time, so we must clear its MemoryStorage rather than replace app.state.limiter
    _rate_limiter._storage.reset()

    # Safe default: registration open, no SMTP
    mock_db.execute.return_value = _scalar(_make_app_settings())

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        # /register carries bot traps (see form_guard). Every test that isn't
        # about the traps themselves posts as a human would, with a stamp that
        # is already old enough to clear the minimum fill time.
        original_post = c.post

        def post(url, **kwargs):
            data = kwargs.get("data")
            if url == "/register" and isinstance(data, dict) and "form_ts" not in data:
                kwargs["data"] = {**data, "form_ts": issue_form_ts(time.time() - 60)}
            return original_post(url, **kwargs)

        c.post = post
        yield c
    app.dependency_overrides.clear()


VALID_FORM = {
    "email": "new@test.com",
    "password": "password123",
    "confirm_password": "password123",
    "display_name": "New User",
}


# ── Registration — validation ─────────────────────────────────────────────────

class TestWebRegisterValidation:
    def test_password_too_short_shows_error(self, web_client, mock_db):
        # Open registration + no existing user, so the request reaches password validation.
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        r = web_client.post("/register", data={**VALID_FORM, "password": "short", "confirm_password": "short"})
        assert r.status_code == 422
        assert "8 characters" in r.text

    def test_password_too_short_preserves_email_and_name(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        r = web_client.post("/register", data={**VALID_FORM, "password": "x", "confirm_password": "x"})
        assert "new@test.com" in r.text
        assert "New User" in r.text

    def test_passwords_do_not_match_shows_error(self, web_client, mock_db):
        # Open registration + no existing user, so the request reaches password validation.
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        r = web_client.post("/register", data={**VALID_FORM, "confirm_password": "different"})
        assert r.status_code == 422
        assert "do not match" in r.text

    def test_passwords_do_not_match_preserves_form_data(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        r = web_client.post("/register", data={**VALID_FORM, "confirm_password": "different"})
        assert "new@test.com" in r.text
        assert "New User" in r.text

    def test_empty_display_name_uses_email_prefix(self, web_client, mock_db):
        from unittest.mock import AsyncMock, patch
        # No SMTP → no verification email → direct redirect to /app
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            r = web_client.post("/register", data={**VALID_FORM, "display_name": "   "})
        # Proceeds to /app — no "Display name" error
        assert r.status_code == 302
        assert "display" not in r.text.lower()

    def test_duplicate_email_shows_error(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),  # AppSettings (no SMTP, registration open)
            _scalar(_make_user()),          # duplicate email check → user found
        ])
        r = web_client.post("/register", data=VALID_FORM)
        assert r.status_code == 409
        assert "already registered" in r.text

    def test_duplicate_email_preserves_email(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(_make_user()),
        ])
        r = web_client.post("/register", data=VALID_FORM)
        assert "new@test.com" in r.text

    def test_registration_disabled_returns_403(self, web_client, mock_db):
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings(registration_enabled=False)))
        r = web_client.post("/register", data=VALID_FORM)
        assert r.status_code == 403

    def test_too_long_password_rejected(self, web_client, mock_db):
        # Open registration + no existing user, so the request reaches password validation.
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        r = web_client.post("/register", data={
            **VALID_FORM, "password": "a" * 73, "confirm_password": "a" * 73})
        assert r.status_code == 422
        assert "too long" in r.text

    def test_malformed_email_rejected(self, web_client, mock_db):
        r = web_client.post("/register", data={**VALID_FORM, "email": "notanemail"})
        assert r.status_code == 422
        assert "valid email" in r.text

    def test_email_with_newline_rejected(self, web_client, mock_db):
        # SMTP header injection attempt — must never reach the To: header.
        r = web_client.post("/register", data={**VALID_FORM, "email": "a@b.com\nBcc: evil@x.com"})
        assert r.status_code == 422
        assert "valid email" in r.text

    def test_email_is_stripped_before_use(self, web_client, mock_db):
        # Surrounding whitespace must not block an otherwise-valid address.
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            r = web_client.post("/register", data={**VALID_FORM, "email": "  new@test.com  "})
        assert r.status_code == 302


# ── Registration — success flows ──────────────────────────────────────────────

class TestWebRegisterSuccess:
    def test_no_smtp_sets_session_and_redirects_to_app(self, web_client, mock_db):
        # AppSettings (no SMTP), no existing user
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            r = web_client.post("/register", data=VALID_FORM)
        assert r.status_code == 302
        assert r.headers["location"] == "/app"

    def _added_settings_timezone(self, mock_db):
        from app.models.user import UserSettings
        for call in mock_db.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, UserSettings):
                return obj.timezone
        return None

    def test_browser_timezone_stored_on_registration(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            web_client.post("/register", data={**VALID_FORM, "timezone": "Europe/Prague"})
        assert self._added_settings_timezone(mock_db) == "Europe/Prague"

    def test_invalid_timezone_falls_back_to_utc(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            web_client.post("/register", data={**VALID_FORM, "timezone": "Mars/Olympus"})
        assert self._added_settings_timezone(mock_db) == "UTC"

    def test_missing_timezone_defaults_to_utc(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            web_client.post("/register", data=VALID_FORM)
        assert self._added_settings_timezone(mock_db) == "UTC"

    def test_smtp_configured_sends_email_and_redirects_to_check_email(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings(smtp_host="smtp.test.com")),
            _scalar(None),
        ])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            with patch("app.routers.web.auth.asyncio.to_thread", new_callable=AsyncMock) as mock_send:
                r = web_client.post("/register", data=VALID_FORM)
        assert r.status_code == 302
        assert "/register/check-email" in r.headers["location"]
        mock_send.assert_called_once()

    def test_smtp_configured_no_session_set(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings(smtp_host="smtp.test.com")),
            _scalar(None),
        ])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            with patch("app.routers.web.auth.asyncio.to_thread", new_callable=AsyncMock):
                r = web_client.post("/register", data=VALID_FORM)
        assert r.status_code == 302
        assert "session" not in r.cookies

    def test_smtp_failure_still_redirects_to_check_email(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings(smtp_host="smtp.test.com")),
            _scalar(None),
        ])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            with patch("app.routers.web.auth.asyncio.to_thread",
                       new_callable=AsyncMock, side_effect=Exception("SMTP down")):
                r = web_client.post("/register", data=VALID_FORM)
        assert r.status_code == 302
        assert "/register/check-email" in r.headers["location"]


# ── Email verification ────────────────────────────────────────────────────────

class TestWebEmailVerification:
    def test_valid_token_redirects_to_login_verified(self, web_client, mock_db):
        import hashlib, secrets
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = _make_user(
            email_verified=False,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mock_db.execute = AsyncMock(return_value=_scalar(user))
        r = web_client.get(f"/verify-email?token={token}")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?verified=1"

    def test_valid_token_sets_email_verified(self, web_client, mock_db):
        import hashlib, secrets
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = _make_user(
            email_verified=False,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mock_db.execute = AsyncMock(return_value=_scalar(user))
        web_client.get(f"/verify-email?token={token}")
        assert user.email_verified is True
        assert user.email_verification_token_hash is None
        assert user.email_verification_expires_at is None

    def test_invalid_token_shows_error(self, web_client, mock_db):
        mock_db.execute = AsyncMock(return_value=_scalar(None))
        r = web_client.get("/verify-email?token=invalidtoken")
        assert r.status_code == 200
        assert "invalid or has expired" in r.text

    def test_missing_token_shows_error(self, web_client, mock_db):
        r = web_client.get("/verify-email")
        assert r.status_code == 200
        assert "invalid or has expired" in r.text

    def test_expired_token_shows_error_with_email(self, web_client, mock_db):
        import hashlib, secrets
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = _make_user(
            email_verified=False,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # expired
            email="expired@test.com",
        )
        mock_db.execute = AsyncMock(return_value=_scalar(user))
        r = web_client.get(f"/verify-email?token={token}")
        assert r.status_code == 200
        assert "invalid or has expired" in r.text
        assert "expired@test.com" in r.text


# ── Resend verification ───────────────────────────────────────────────────────

class TestWebResendVerification:
    def test_resend_for_unverified_user_sends_email(self, web_client, mock_db):
        user = _make_user(email_verified=False)
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings(smtp_host="smtp.test.com")),
            _scalar(user),
        ])
        with patch("app.routers.web.auth.asyncio.to_thread", new_callable=AsyncMock) as mock_send:
            r = web_client.post("/resend-verification", data={"email": "new@test.com"})
        assert r.status_code == 302
        assert "/register/check-email" in r.headers["location"]
        mock_send.assert_called_once()

    def test_resend_for_unknown_email_is_silent(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings(smtp_host="smtp.test.com")),
            _scalar(None),  # user not found
        ])
        with patch("app.routers.web.auth.asyncio.to_thread", new_callable=AsyncMock) as mock_send:
            r = web_client.post("/resend-verification", data={"email": "nobody@test.com"})
        assert r.status_code == 302
        mock_send.assert_not_called()

    def test_resend_for_already_verified_user_is_silent(self, web_client, mock_db):
        user = _make_user(email_verified=True)
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings(smtp_host="smtp.test.com")),
            _scalar(user),
        ])
        with patch("app.routers.web.auth.asyncio.to_thread", new_callable=AsyncMock) as mock_send:
            r = web_client.post("/resend-verification", data={"email": "new@test.com"})
        assert r.status_code == 302
        mock_send.assert_not_called()

    def test_resend_without_smtp_is_silent(self, web_client, mock_db):
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings(smtp_host=None)))
        with patch("app.routers.web.auth.asyncio.to_thread", new_callable=AsyncMock) as mock_send:
            r = web_client.post("/resend-verification", data={"email": "new@test.com"})
        assert r.status_code == 302
        mock_send.assert_not_called()


# ── Login — unverified email ──────────────────────────────────────────────────

class TestWebLoginEmailVerified:
    def test_unverified_email_shows_error_with_resend_link(self, web_client, mock_db):
        user = _make_user(email_verified=False)
        mock_db.execute = AsyncMock(return_value=_scalar(user))
        with patch("app.routers.web.auth.verify_password", return_value=True):
            r = web_client.post("/login", data={"email": "new@test.com", "password": "password123"})
        assert r.status_code == 403
        assert "not verified" in r.text.lower()
        assert "/register/check-email" in r.text

    def test_verified_user_can_login(self, web_client, mock_db):
        user = _make_user(email_verified=True)
        mock_db.execute = AsyncMock(return_value=_scalar(user))
        with patch("app.routers.web.auth.verify_password", return_value=True):
            r = web_client.post("/login", data={"email": "new@test.com", "password": "password123"})
        assert r.status_code == 302
        assert r.headers["location"] == "/app"

    def test_login_page_shows_verified_flash(self, web_client, mock_db):
        # GET /login calls _get_app_settings → one db.execute (returns None = open registration)
        mock_db.execute = AsyncMock(return_value=_scalar(None))
        r = web_client.get("/login?verified=1")
        assert r.status_code == 200
        assert "verified" in r.text.lower()


# ── Registration — bot traps ──────────────────────────────────────────────────

class TestRegisterBotTraps:
    """Registration emails an arbitrary address, so it is the one endpoint an
    attacker can use as a mail relay. These guard that door."""

    def test_filled_honeypot_creates_no_account(self, web_client, mock_db):
        r = web_client.post("/register", data={
            **VALID_FORM,
            "form_ts": issue_form_ts(time.time() - 60),
            HONEYPOT_FIELD: "http://spam.example",
        })
        assert r.status_code == 302
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_filled_honeypot_looks_like_success(self, web_client, mock_db):
        # The bot must not be able to tell it was caught.
        r = web_client.post("/register", data={
            **VALID_FORM,
            "form_ts": issue_form_ts(time.time() - 60),
            HONEYPOT_FIELD: "spam",
        })
        assert r.headers["location"].startswith("/register/check-email")

    def test_instant_submit_creates_no_account(self, web_client, mock_db):
        r = web_client.post("/register", data={**VALID_FORM, "form_ts": issue_form_ts()})
        assert r.status_code == 302
        assert r.headers["location"].startswith("/register/check-email")
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_missing_stamp_creates_no_account(self, web_client, mock_db):
        r = web_client.post("/register", data={**VALID_FORM, "form_ts": ""})
        assert r.status_code == 422
        assert "expired" in r.text.lower()
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_forged_stamp_creates_no_account(self, web_client, mock_db):
        forged = f"{int(time.time()) - 60}.bogussignature"
        r = web_client.post("/register", data={**VALID_FORM, "form_ts": forged})
        assert r.status_code == 422
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_expired_stamp_creates_no_account(self, web_client, mock_db):
        from app.utils.form_guard import MAX_FORM_AGE_SECONDS
        old = issue_form_ts(time.time() - MAX_FORM_AGE_SECONDS - 60)
        r = web_client.post("/register", data={**VALID_FORM, "form_ts": old})
        assert r.status_code == 422
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_empty_honeypot_and_aged_stamp_passes(self, web_client, mock_db):
        # No SMTP → straight to /app, proving the traps let a human through.
        mock_db.execute = AsyncMock(side_effect=[_scalar(_make_app_settings()), _scalar(None)])
        with patch("app.auth.security.hash_password", return_value="hashed"):
            r = web_client.post("/register", data={
                **VALID_FORM,
                "form_ts": issue_form_ts(time.time() - 60),
                HONEYPOT_FIELD: "",
            })
        assert r.status_code == 302
        assert r.headers["location"] == "/app"

    def test_register_page_renders_the_traps(self, web_client, mock_db):
        from app.services.app_settings_cache import invalidate_registration_cache
        # The open/closed flag is cached process-wide; another test may have
        # filled it with False, which would render the "closed" page instead.
        invalidate_registration_cache()
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings()))
        r = web_client.get("/register")
        assert r.status_code == 200
        assert 'name="form_ts"' in r.text
        assert f'name="{HONEYPOT_FIELD}"' in r.text


class TestFormGuardUnit:
    def test_stamp_round_trips(self):
        from app.utils.form_guard import form_age_seconds
        age = form_age_seconds(issue_form_ts(time.time() - 30))
        assert age is not None
        assert 29 <= age <= 32

    def test_tampered_timestamp_has_no_age(self):
        # Rewrite the timestamp and keep the original signature — the forgery a bot
        # would actually attempt, backdating the stamp to clear the too_fast check.
        from app.utils.form_guard import form_age_seconds
        payload, _, signature = issue_form_ts(time.time() - 30).partition(".")
        forged = str(int(payload) - 600) + "." + signature
        assert form_age_seconds(forged) is None

    def test_tampered_signature_has_no_age(self):
        # Mutate the *first* signature character, not the last. The signature is 20
        # HMAC-SHA1 bytes in 27 base64url characters, so its final character carries
        # two unused bits and four different characters decode to identical bytes:
        # flipping only that one left the stamp valid in ~7 % of runs, and the test
        # went red for no reason.
        from app.utils.form_guard import form_age_seconds
        payload, _, signature = issue_form_ts(time.time() - 30).partition(".")
        flipped = ("b" if signature[0] != "b" else "c") + signature[1:]
        assert form_age_seconds(payload + "." + flipped) is None

    def test_unsigned_stamp_rejected(self):
        from app.utils.form_guard import form_age_seconds
        assert form_age_seconds(str(int(time.time()) - 30)) is None

    def test_check_form_reasons(self):
        from app.utils.form_guard import check_form
        good = issue_form_ts(time.time() - 60)
        assert check_form("", good) is None
        assert check_form("spam", good) == "honeypot"
        assert check_form("", issue_form_ts()) == "too_fast"
        assert check_form("", "") == "stale"

    def test_honeypot_beats_a_valid_stamp(self):
        from app.utils.form_guard import check_form
        assert check_form("  spam  ", issue_form_ts(time.time() - 60)) == "honeypot"
