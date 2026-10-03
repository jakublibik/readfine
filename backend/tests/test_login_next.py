"""Sign-in ?next=: a link into the app survives the login, and cannot point off-site."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.utils.next_path import safe_next_path
from tests.test_rate_limit import _make_app_settings, _make_user, _scalar


class TestSafeNextPath:
    @pytest.mark.parametrize("value", [
        "/settings/profile",
        "/app?feed=3",
        "/settings/profile#delete",
    ])
    def test_local_path_passes(self, value):
        assert safe_next_path(value) == value

    @pytest.mark.parametrize("value", [
        None,
        "",
        "settings/profile",
        "https://evil.test/",
        "//evil.test/",
        "/\\evil.test/",
        "/settings\\..\\x",
        "/\t/evil.test/",
        "/settings\r\nLocation: https://evil.test/",
        "javascript:alert(1)",
    ])
    def test_off_site_or_odd_value_refused(self, value):
        assert safe_next_path(value) is None


@pytest.fixture
def login_client(mock_db):
    from app.main import app
    from app.database import get_db
    from app.rate_limit import _failed_attempts, limiter

    limiter._storage.reset()
    _failed_attempts.clear()

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()
    _failed_attempts.clear()


def _login(client, mock_db, **extra):
    mock_db.execute = AsyncMock(side_effect=[
        _scalar(_make_app_settings()),
        _scalar(_make_user()),
    ])
    with patch("app.auth.security.verify_password", return_value=True):
        return client.post("/login", data={"email": "user@test.com", "password": "pw", **extra})


class TestLoginNext:
    def test_login_lands_on_next(self, login_client, mock_db):
        r = _login(login_client, mock_db, next="/settings/profile")
        assert r.status_code == 302
        assert r.headers["location"] == "/settings/profile"

    def test_login_without_next_lands_in_app(self, login_client, mock_db):
        assert _login(login_client, mock_db).headers["location"] == "/app"

    def test_external_next_ignored(self, login_client, mock_db):
        r = _login(login_client, mock_db, next="//evil.test/")
        assert r.headers["location"] == "/app"

    def test_login_page_carries_next_into_form(self, login_client, mock_db):
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings()))
        r = login_client.get("/login?next=/settings/profile")
        assert 'name="next" value="/settings/profile"' in r.text

    def test_login_page_drops_external_next(self, login_client, mock_db):
        mock_db.execute = AsyncMock(return_value=_scalar(_make_app_settings()))
        r = login_client.get("/login?next=https://evil.test/")
        assert 'name="next"' not in r.text


class TestUnauthenticatedRedirect:
    def test_page_link_kept_as_next(self, login_client):
        r = login_client.get("/settings/profile")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/settings/profile"

    def test_query_kept_and_encoded(self, login_client):
        r = login_client.get("/settings/profile?a=1&b=2")
        assert r.headers["location"] == "/login?next=/settings/profile%3Fa%3D1%26b%3D2"

    def test_app_root_gets_plain_login(self, login_client):
        assert login_client.get("/app").headers["location"] == "/login"
