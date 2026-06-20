"""Tests for self-service account deletion (POST /settings/profile/delete-account)."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

ENDPOINT = "/settings/profile/delete-account"

VALID_FORM = {
    "current_password": "correctpassword",
    "confirm_text": "delete my account",
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def delete_client(mock_db, client):
    """client fixture with cleanup_user_feeds patched to a no-op."""
    with patch(
        "app.routers.web.settings.cleanup_user_feeds",
        new=AsyncMock(),
    ):
        yield client


@pytest.fixture
def delete_admin_client(mock_db, admin_client):
    with patch(
        "app.routers.web.settings.cleanup_user_feeds",
        new=AsyncMock(),
    ):
        yield admin_client


# ── Validation ────────────────────────────────────────────────────────────────

class TestDeleteAccountValidation:

    def test_wrong_confirm_text_shows_error(self, delete_client):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            resp = delete_client.post(ENDPOINT, data={
                "current_password": "correctpassword",
                "confirm_text": "wrong text",
            }, follow_redirects=False)
        assert resp.status_code == 200
        assert "delete my account" in resp.text

    def test_wrong_confirm_text_case_sensitive_accept(self, delete_client):
        """'DELETE MY ACCOUNT' must also be accepted (lowercased)."""
        with patch("app.routers.web.settings.verify_password", return_value=True):
            resp = delete_client.post(ENDPOINT, data={
                "current_password": "correctpassword",
                "confirm_text": "DELETE MY ACCOUNT",
            }, follow_redirects=False)
        assert resp.status_code == 303

    def test_wrong_password_shows_error(self, delete_client):
        with patch("app.routers.web.settings.verify_password", return_value=False):
            resp = delete_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        assert resp.status_code == 200
        assert "Password is incorrect" in resp.text

    def test_wrong_password_does_not_delete(self, delete_client, mock_db):
        with patch("app.routers.web.settings.verify_password", return_value=False):
            delete_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        mock_db.delete.assert_not_called()

    def test_wrong_confirm_does_not_delete(self, delete_client, mock_db):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            delete_client.post(ENDPOINT, data={
                "current_password": "correctpassword",
                "confirm_text": "nope",
            }, follow_redirects=False)
        mock_db.delete.assert_not_called()


# ── Successful deletion ───────────────────────────────────────────────────────

class TestDeleteAccountSuccess:

    def test_redirects_to_login_deleted(self, delete_client):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            resp = delete_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?deleted=1"

    def test_user_row_deleted(self, delete_client, mock_db):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            delete_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        mock_db.delete.assert_called_once()

    def test_db_committed(self, delete_client, mock_db):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            delete_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        mock_db.commit.assert_called()

    def test_cleanup_feeds_called(self, mock_db, client):
        cleanup_mock = AsyncMock()
        with patch("app.routers.web.settings.cleanup_user_feeds", new=cleanup_mock):
            with patch("app.routers.web.settings.verify_password", return_value=True):
                client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        cleanup_mock.assert_called_once()

    def test_redirect_url_contains_deleted_param(self, delete_client):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            resp = delete_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        assert "deleted=1" in resp.headers["location"]


# ── Admin guard ───────────────────────────────────────────────────────────────

class TestDeleteAccountAdminGuard:

    def test_admin_cannot_delete_account(self, delete_admin_client):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            resp = delete_admin_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        assert resp.status_code == 403

    def test_admin_blocked_before_db_delete(self, delete_admin_client, mock_db):
        with patch("app.routers.web.settings.verify_password", return_value=True):
            delete_admin_client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        mock_db.delete.assert_not_called()

    def test_admin_profile_has_no_danger_zone(self, admin_client):
        resp = admin_client.get("/settings/profile")
        assert "Danger zone" not in resp.text


# ── Error handling ────────────────────────────────────────────────────────────

class TestDeleteAccountErrorHandling:

    def test_db_error_returns_500_with_inline_message(self, client, mock_db):
        mock_db.commit.side_effect = Exception("db failure")
        with patch("app.routers.web.settings.cleanup_user_feeds", new=AsyncMock()):
            with patch("app.routers.web.settings.verify_password", return_value=True):
                resp = client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        assert resp.status_code == 500
        assert "server error" in resp.text.lower()

    def test_db_error_rolls_back(self, client, mock_db):
        mock_db.commit.side_effect = Exception("db failure")
        with patch("app.routers.web.settings.cleanup_user_feeds", new=AsyncMock()):
            with patch("app.routers.web.settings.verify_password", return_value=True):
                client.post(ENDPOINT, data=VALID_FORM, follow_redirects=False)
        mock_db.rollback.assert_called_once()
