"""API tests for POST /api/v1/auth/token and GET /api/v1/auth/me."""
from unittest.mock import MagicMock, patch

import pytest


def _make_db_user(active=True, password_hash="dummy"):
    user = MagicMock()
    user.id = 1
    user.role = "user"
    user.email = "test@test.com"
    user.display_name = "Test User"
    user.is_active = active
    user.password_hash = password_hash
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

        with patch("app.routers.api.v1.auth.verify_password", return_value=True):
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

        with patch("app.routers.api.v1.auth.verify_password", return_value=False):
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

        with patch("app.routers.api.v1.auth.verify_password", return_value=True):
            response = client.post(
                "/api/v1/auth/token",
                json={"email": "test@test.com", "password": "password123"},
            )

        assert response.status_code == 403

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
