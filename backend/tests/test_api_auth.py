"""API tests for POST /api/v1/auth/token and GET /api/v1/auth/me."""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_login_limits():
    from app.rate_limit import _failed_attempts, limiter

    limiter._storage.reset()
    _failed_attempts.clear()
    yield
    _failed_attempts.clear()


def _make_db_user(active=True, password_hash="dummy", verified=True):
    user = MagicMock()
    user.id = 1
    user.role = "user"
    user.email = "test@test.com"
    user.display_name = "Test User"
    user.is_active = active
    user.email_verified = verified
    user.password_hash = password_hash
    user.session_token_version = 0
    user.created_at = None
    return user


def _mock_db_execute(mock_db, db_user):
    result = MagicMock()
    result.scalar_one_or_none.return_value = db_user
    mock_db.execute.return_value = result


class TestGetToken:
    def test_valid_credentials_returns_token(self, client, mock_db):
        db_user = _make_db_user()
        _mock_db_execute(mock_db, db_user)

        with patch("app.auth.security.verify_password", return_value=True):
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "password123"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_wrong_password_returns_401(self, client, mock_db):
        db_user = _make_db_user()
        _mock_db_execute(mock_db, db_user)

        with patch("app.auth.security.verify_password", return_value=False):
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "wrongpass"},
            )

        assert response.status_code == 401

    def test_user_not_found_returns_401(self, client, mock_db):
        _mock_db_execute(mock_db, None)

        response = client.post(
            "/api/v1/auth/token",
            json={"email": "nobody@test.com", "password": "password123"},
        )

        assert response.status_code == 401

    def test_inactive_user_returns_403(self, client, mock_db):
        db_user = _make_db_user(active=False)
        _mock_db_execute(mock_db, db_user)

        with patch("app.auth.security.verify_password", return_value=True):
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "password123"},
            )

        assert response.status_code == 403

    def test_unverified_user_gets_no_token(self, client, mock_db):
        # Web login refuses an unverified account; the API must not hand it a token.
        _mock_db_execute(mock_db, _make_db_user(verified=False))

        with patch("app.auth.security.verify_password", return_value=True):
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "password123"},
            )

        assert response.status_code == 403
        assert "access_token" not in response.json()

    def test_locked_out_pair_is_refused_before_password_check(self, client, mock_db):
        from app.rate_limit import _LOCKOUT_THRESHOLD, record_failed_login

        # TestClient connects from "testclient"; lock that pair as web login would.
        for _ in range(_LOCKOUT_THRESHOLD):
            record_failed_login("testclient", "test@test.com")
        _mock_db_execute(mock_db, _make_db_user())

        with patch("app.auth.security.verify_password", return_value=True) as verify:
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "password123"},
            )

        assert response.status_code == 429
        verify.assert_not_called()

    def test_failed_attempt_counts_toward_lockout(self, client, mock_db):
        from app.rate_limit import _failed_attempts

        _mock_db_execute(mock_db, _make_db_user())

        with patch("app.auth.security.verify_password", return_value=False):
            client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "wrongpass"},
            )

        assert _failed_attempts[("testclient", "test@test.com")]["count"] == 1

    def test_successful_login_clears_failed_attempts(self, client, mock_db):
        from app.rate_limit import _failed_attempts, record_failed_login

        record_failed_login("testclient", "test@test.com")
        _mock_db_execute(mock_db, _make_db_user())

        with patch("app.auth.security.verify_password", return_value=True):
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "password123"},
            )

        assert response.status_code == 200
        assert ("testclient", "test@test.com") not in _failed_attempts

    def test_missing_email_returns_422(self, client, mock_db):
        response = client.post(
            "/api/v1/auth/token",
            json={"password": "password123"},
        )
        assert response.status_code == 422

    def test_missing_password_returns_422(self, client, mock_db):
        response = client.post(
            "/api/v1/auth/token",
            json={"email": "test@test.com"},
        )
        assert response.status_code == 422

    def test_invalid_email_format_returns_422(self, client, mock_db):
        response = client.post(
            "/api/v1/auth/token",
            json={"email": "not-an-email", "password": "password123"},
        )
        assert response.status_code == 422


class TestGetMe:
    def test_authenticated_returns_user_info(self, client):
        # client fixture overrides get_api_user → MOCK_USER
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer fake-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["role"] == "user"

    def test_no_auth_returns_401(self, unauth_client):
        response = unauth_client.get("/api/v1/auth/me")
        assert response.status_code == 401
