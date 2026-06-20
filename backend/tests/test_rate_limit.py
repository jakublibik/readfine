"""Tests for rate limiting: IP extraction, brute-force tracker, login lockout, 429 HTML response."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _make_user(
    email="user@test.com",
    password_hash="hashed",
    is_active=True,
    email_verified=True,
):
    u = MagicMock()
    u.id = 1
    u.email = email
    u.password_hash = password_hash
    u.is_active = is_active
    u.email_verified = email_verified
    u.last_active_at = None
    u.session_token_version = 0
    return u


def _make_app_settings():
    s = MagicMock()
    s.smtp_host = None
    s.registration_enabled = True
    return s


def _make_request(headers: dict) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    req.client = SimpleNamespace(host="1.2.3.4")
    return req


@pytest.fixture(autouse=True)
def _reset_failed_attempts():
    """Clear in-memory brute-force state before each test."""
    from app.rate_limit import _failed_attempts
    _failed_attempts.clear()
    yield
    _failed_attempts.clear()


@pytest.fixture
def login_client(mock_db):
    """Unauthenticated client for login tests with slowapi storage reset."""
    from app.main import app
    from app.database import get_db
    from app.rate_limit import limiter as _limiter

    _limiter._storage.reset()
    mock_db.execute.return_value = _scalar(None)

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── Unit: get_client_ip ───────────────────────────────────────────────────────

def _patch_proxy(trusted_proxy_count=0, trust_cloudflare=False):
    """Patch the proxy-trust config that get_client_ip reads."""
    from app.config import settings
    return patch.multiple(
        settings,
        trusted_proxy_count=trusted_proxy_count,
        trust_cloudflare=trust_cloudflare,
    )


class TestGetClientIp:
    # Default deployment: no proxy in front → forwarding headers are
    # attacker-controlled and MUST be ignored in favour of the real peer.
    def test_default_ignores_cf_connecting_ip(self):
        from app.rate_limit import get_client_ip
        req = _make_request({"CF-Connecting-IP": "10.0.0.1", "X-Forwarded-For": "10.0.0.2"})
        with _patch_proxy():
            assert get_client_ip(req) == "1.2.3.4"

    def test_default_ignores_x_forwarded_for(self):
        from app.rate_limit import get_client_ip
        req = _make_request({"X-Forwarded-For": "10.0.0.5, 192.168.1.1"})
        with _patch_proxy():
            assert get_client_ip(req) == "1.2.3.4"

    def test_no_client_returns_unknown(self):
        from app.rate_limit import get_client_ip
        req = _make_request({})
        req.client = None
        with _patch_proxy():
            assert get_client_ip(req) == "unknown"

    # Cloudflare mode (firewall restricts origin to CF ranges).
    def test_cloudflare_uses_cf_connecting_ip(self):
        from app.rate_limit import get_client_ip
        req = _make_request({"CF-Connecting-IP": "  10.0.0.1  ", "X-Forwarded-For": "9.9.9.9"})
        with _patch_proxy(trust_cloudflare=True):
            assert get_client_ip(req) == "10.0.0.1"

    def test_cloudflare_falls_back_to_peer_when_header_missing(self):
        from app.rate_limit import get_client_ip
        req = _make_request({"X-Forwarded-For": "9.9.9.9"})
        with _patch_proxy(trust_cloudflare=True):
            assert get_client_ip(req) == "1.2.3.4"

    # Reverse-proxy mode: read X-Forwarded-For from the RIGHT, never leftmost.
    def test_one_proxy_takes_rightmost_entry(self):
        from app.rate_limit import get_client_ip
        # spoofed, real-client, (proxy appends its peer = real client at -1)
        req = _make_request({"X-Forwarded-For": "spoofed, 203.0.113.7"})
        with _patch_proxy(trusted_proxy_count=1):
            assert get_client_ip(req) == "203.0.113.7"

    def test_two_proxies_skip_two_hops_from_right(self):
        from app.rate_limit import get_client_ip
        req = _make_request({"X-Forwarded-For": "203.0.113.7, 172.16.0.1, 172.16.0.2"})
        with _patch_proxy(trusted_proxy_count=2):
            assert get_client_ip(req) == "172.16.0.1"

    def test_spoofed_leftmost_is_ignored(self):
        from app.rate_limit import get_client_ip
        # Attacker prepends a fake entry; with 1 trusted proxy it never wins.
        req = _make_request({"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
        with _patch_proxy(trusted_proxy_count=1):
            assert get_client_ip(req) != "1.1.1.1"

    def test_short_chain_falls_back_to_peer(self):
        from app.rate_limit import get_client_ip
        # Expect 2 hops but only 1 entry present → don't trust, use peer.
        req = _make_request({"X-Forwarded-For": "203.0.113.7"})
        with _patch_proxy(trusted_proxy_count=2):
            assert get_client_ip(req) == "1.2.3.4"

    def test_missing_xff_falls_back_to_peer(self):
        from app.rate_limit import get_client_ip
        req = _make_request({})
        with _patch_proxy(trusted_proxy_count=1):
            assert get_client_ip(req) == "1.2.3.4"


# ── Unit: brute-force tracker ─────────────────────────────────────────────────

class TestBruteForceTracker:
    def test_no_lockout_initially(self):
        from app.rate_limit import check_login_lockout
        assert check_login_lockout("1.2.3.4", "a@b.com") is False

    def test_not_locked_below_threshold(self):
        from app.rate_limit import check_login_lockout, record_failed_login, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD - 1):
            record_failed_login("1.2.3.4", "a@b.com")
        assert check_login_lockout("1.2.3.4", "a@b.com") is False

    def test_lockout_triggered_at_threshold(self):
        from app.rate_limit import check_login_lockout, record_failed_login, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD):
            record_failed_login("1.2.3.4", "a@b.com")
        assert check_login_lockout("1.2.3.4", "a@b.com") is True

    def test_record_returns_true_when_lockout_triggered(self):
        from app.rate_limit import record_failed_login, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD - 1):
            record_failed_login("1.2.3.4", "a@b.com")
        result = record_failed_login("1.2.3.4", "a@b.com")
        assert result is True

    def test_record_returns_false_below_threshold(self):
        from app.rate_limit import record_failed_login
        result = record_failed_login("1.2.3.4", "a@b.com")
        assert result is False

    def test_clear_removes_lockout(self):
        from app.rate_limit import check_login_lockout, record_failed_login, clear_failed_logins, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD):
            record_failed_login("1.2.3.4", "a@b.com")
        assert check_login_lockout("1.2.3.4", "a@b.com") is True
        clear_failed_logins("1.2.3.4", "a@b.com")
        assert check_login_lockout("1.2.3.4", "a@b.com") is False

    def test_email_case_insensitive(self):
        from app.rate_limit import check_login_lockout, record_failed_login, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD):
            record_failed_login("1.2.3.4", "User@Example.COM")
        assert check_login_lockout("1.2.3.4", "user@example.com") is True

    def test_different_ips_tracked_independently(self):
        from app.rate_limit import check_login_lockout, record_failed_login, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD):
            record_failed_login("1.2.3.4", "a@b.com")
        assert check_login_lockout("5.6.7.8", "a@b.com") is False

    def test_different_emails_tracked_independently(self):
        from app.rate_limit import check_login_lockout, record_failed_login, _LOCKOUT_THRESHOLD
        for _ in range(_LOCKOUT_THRESHOLD):
            record_failed_login("1.2.3.4", "a@b.com")
        assert check_login_lockout("1.2.3.4", "other@b.com") is False

    def test_lockout_expires(self):
        from app.rate_limit import check_login_lockout, _failed_attempts, _LOCKOUT_THRESHOLD
        key = ("1.2.3.4", "a@b.com")
        _failed_attempts[key] = {
            "count": _LOCKOUT_THRESHOLD,
            "locked_until": time.monotonic() - 1,  # already expired
            "last_attempt": time.monotonic(),
        }
        assert check_login_lockout("1.2.3.4", "a@b.com") is False


# ── Integration: login endpoint ───────────────────────────────────────────────

class TestLoginLockout:
    def _bad_login(self, client, mock_db, email="victim@test.com"):
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(None),  # user not found
        ])
        with patch("app.routers.web.auth.verify_password", return_value=False):
            return client.post("/login", data={"email": email, "password": "wrong"})

    def _good_login(self, client, mock_db, email="victim@test.com"):
        user = _make_user(email=email)
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(user),
        ])
        with patch("app.routers.web.auth.verify_password", return_value=True):
            return client.post("/login", data={"email": email, "password": "correct"})

    def test_failed_login_returns_401(self, login_client, mock_db):
        r = self._bad_login(login_client, mock_db)
        assert r.status_code == 401
        assert "Invalid email or password" in r.text

    def test_unknown_user_runs_dummy_verify(self, login_client, mock_db):
        # Constant-time: unknown email must still trigger a bcrypt verify.
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(None),  # user not found
        ])
        with patch("app.routers.web.auth.dummy_verify_password") as dummy:
            login_client.post("/login", data={"email": "ghost@test.com", "password": "x"})
        dummy.assert_called_once()

    def test_known_user_skips_dummy_verify(self, login_client, mock_db):
        # Real user → real verify_password runs, dummy is not needed.
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(_make_user()),
        ])
        with patch("app.routers.web.auth.dummy_verify_password") as dummy, \
             patch("app.routers.web.auth.verify_password", return_value=False):
            login_client.post("/login", data={"email": "victim@test.com", "password": "x"})
        dummy.assert_not_called()

    def test_lockout_after_threshold_attempts(self, login_client, mock_db):
        from app.rate_limit import _failed_attempts, _LOCKOUT_THRESHOLD
        # Pre-fill to one below threshold so the next bad login triggers lockout
        _failed_attempts[("testclient", "victim@test.com")] = {
            "count": _LOCKOUT_THRESHOLD - 1,
            "locked_until": None,
            "last_attempt": time.monotonic(),
        }
        # This bad login should hit the threshold and lock out
        self._bad_login(login_client, mock_db)
        # Next attempt must be blocked by the lockout check
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings()))
        r = login_client.post("/login", data={"email": "victim@test.com", "password": "x"})
        assert r.status_code == 429
        assert "Too many failed attempts" in r.text

    def test_lockout_message_shown(self, login_client, mock_db):
        from app.rate_limit import _failed_attempts, _LOCKOUT_THRESHOLD
        _failed_attempts[("testclient", "victim@test.com")] = {
            "count": _LOCKOUT_THRESHOLD,
            "locked_until": time.monotonic() + 900,
            "last_attempt": time.monotonic(),
        }
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings()))
        r = login_client.post("/login", data={"email": "victim@test.com", "password": "x"})
        assert r.status_code == 429
        assert "15 minutes" in r.text

    def test_successful_login_clears_failed_counter(self, login_client, mock_db):
        from app.rate_limit import _failed_attempts, _LOCKOUT_THRESHOLD
        # Pre-populate some failures (below lockout threshold)
        _failed_attempts[("testclient", "victim@test.com")] = {
            "count": _LOCKOUT_THRESHOLD - 1,
            "locked_until": None,
            "last_attempt": time.monotonic(),
        }
        self._good_login(login_client, mock_db)
        assert ("testclient", "victim@test.com") not in _failed_attempts

    def test_successful_login_redirects_to_app(self, login_client, mock_db):
        r = self._good_login(login_client, mock_db)
        assert r.status_code == 302
        assert r.headers["location"] == "/app"

    def test_inactive_account_does_not_increment_counter(self, login_client, mock_db):
        from app.rate_limit import _failed_attempts
        user = _make_user(is_active=False)
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(user),
        ])
        with patch("app.routers.web.auth.verify_password", return_value=True):
            login_client.post("/login", data={"email": "victim@test.com", "password": "x"})
        assert ("testclient", "victim@test.com") not in _failed_attempts

    def test_unverified_email_does_not_increment_counter(self, login_client, mock_db):
        from app.rate_limit import _failed_attempts
        user = _make_user(email_verified=False)
        mock_db.execute = AsyncMock(side_effect=[
            _scalar(_make_app_settings()),
            _scalar(user),
        ])
        with patch("app.routers.web.auth.verify_password", return_value=True):
            login_client.post("/login", data={"email": "victim@test.com", "password": "x"})
        assert ("testclient", "victim@test.com") not in _failed_attempts


# ── Integration: slowapi 429 returns HTML ─────────────────────────────────────

class TestSlowapi429Html:
    def test_rate_limit_exceeded_returns_html(self, login_client, mock_db):
        from app.config import settings as cfg
        # Exhaust the slowapi IP limit
        limit = int(cfg.rate_limit_login.split("/")[0])
        for _ in range(limit):
            mock_db.execute = AsyncMock(side_effect=[
                _scalar(_make_app_settings()),
                _scalar(None),
            ])
            with patch("app.routers.web.auth.verify_password", return_value=False):
                login_client.post("/login", data={"email": "x@x.com", "password": "x"})
        # Next request should hit slowapi limit
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings()))
        r = login_client.post("/login", data={"email": "x@x.com", "password": "x"})
        assert r.status_code == 429
        assert "text/html" in r.headers.get("content-type", "")
        assert "429" in r.text
