"""Where an account came from (users.signup_source).

Kept only while public page counting is on, and only as a domain, a utm tag or one of
direct / internal / invite. The value makes a round trip through a hidden form field,
so what comes back is checked again before it is stored.
"""
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.requests import Request

from app.config import settings as app_settings
from app.models.user import User
from app.services import traffic_service as ts
from app.utils.form_guard import issue_form_ts


@pytest.fixture(autouse=True)
def counting_on():
    ts.discard_state()
    ts.set_enabled(True)
    yield
    ts.discard_state()
    ts.set_enabled(False)


def make_request(path="/register", query="", referer=None):
    headers = [(b"referer", referer.encode())] if referer else []
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": headers, "client": ("203.0.113.9", 51234),
        "server": ("readfine.test", 443),
    })


# ── Working out the source ───────────────────────────────────────────────────

class TestSignupSourceFor:
    def test_foreign_referrer_is_its_domain(self):
        req = make_request(referer="https://www.news.ycombinator.com/item?id=1")
        assert ts.signup_source_for(req) == "news.ycombinator.com"

    def test_no_referrer_is_direct(self):
        assert ts.signup_source_for(make_request()) == "direct"

    def test_own_site_is_internal_not_direct(self):
        req = make_request(referer="https://readfine.test/features")
        assert ts.signup_source_for(req) == "internal"

    def test_src_from_the_landing_wins_over_the_referrer(self):
        req = make_request(query="src=reddit.com", referer="https://readfine.test/")
        assert ts.signup_source_for(req) == "reddit.com"

    def test_utm_tag(self):
        req = make_request(query="utm_source=Newsletter")
        assert ts.signup_source_for(req) == "utm:newsletter"

    def test_nothing_while_counting_is_off(self):
        ts.set_enabled(False)
        req = make_request(referer="https://news.ycombinator.com/")
        assert ts.signup_source_for(req) is None

    def test_page_counts_still_fold_own_site_into_direct(self):
        req = make_request(referer="https://readfine.test/features")
        assert ts._source_for(req) == "direct"


class TestCleanSignupSource:
    @pytest.mark.parametrize("value, expected", [
        ("news.ycombinator.com", "news.ycombinator.com"),
        ("  Reddit.COM ", "reddit.com"),
        ("utm:newsletter", "utm:newsletter"),
        ("direct", "direct"),
        ("https://evil.example/path", None),
        ("example.com/path", None),
        ("<script>", None),
        ("a" * 81, None),
        ("", None),
        (None, None),
    ])
    def test_shape(self, value, expected):
        assert ts.clean_signup_source(value) == expected


# ── Registration ─────────────────────────────────────────────────────────────

def _scalar(value):
    r = SimpleNamespace(scalar_one_or_none=lambda: value)
    return r


def _app_settings():
    return SimpleNamespace(smtp_host=None, smtp_from_email=None, registration_enabled=True)


FORM = {
    "email": "new@test.com",
    "password": "password123",
    "confirm_password": "password123",
}


@pytest.fixture
def web_client(mock_db):
    from app.database import get_db
    from app.main import app
    from app.rate_limit import limiter

    limiter._storage.reset()

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


def _post(client, **data):
    return client.post("/register", data={**FORM, "form_ts": issue_form_ts(time.time() - 60), **data})


def _added_user(mock_db) -> User:
    return next(c.args[0] for c in mock_db.add.call_args_list if isinstance(c.args[0], User))


class TestRegistration:
    def test_form_carries_the_source(self, web_client):
        with patch("app.routers.web.auth.get_registration_enabled", new=AsyncMock(return_value=True)):
            r = web_client.get("/register", headers={"referer": "https://lobste.rs/s/abc"})
        assert 'name="signup_source" value="lobste.rs"' in r.text

    def test_form_has_no_field_while_counting_is_off(self, web_client):
        ts.set_enabled(False)
        with patch("app.routers.web.auth.get_registration_enabled", new=AsyncMock(return_value=True)):
            r = web_client.get("/register", headers={"referer": "https://lobste.rs/s/abc"})
        assert 'name="signup_source"' not in r.text

    def test_stored_on_the_account(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_app_settings()), _scalar(None)])
        _post(web_client, signup_source="lobste.rs")
        assert _added_user(mock_db).signup_source == "lobste.rs"

    def test_tampered_value_is_dropped(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_app_settings()), _scalar(None)])
        _post(web_client, signup_source="https://lobste.rs/s/abc?ref=me")
        assert _added_user(mock_db).signup_source is None

    def test_nothing_stored_while_counting_is_off(self, web_client, mock_db):
        ts.set_enabled(False)
        mock_db.execute = AsyncMock(side_effect=[_scalar(_app_settings()), _scalar(None)])
        _post(web_client, signup_source="lobste.rs")
        assert _added_user(mock_db).signup_source is None

    def test_invitation_is_invite(self, web_client, mock_db):
        inv = SimpleNamespace(id=7, email=None, used_at=None, used_by=None)
        mock_db.execute = AsyncMock(side_effect=[_scalar(_app_settings()), _scalar(None),
                                                    MagicMock(rowcount=1)])  # invitation claim
        with patch("app.routers.web.auth._get_valid_invitation", new=AsyncMock(return_value=inv)):
            _post(web_client, invite_token="tok", signup_source="lobste.rs")
        assert _added_user(mock_db).signup_source == "invite"

    def test_kept_through_a_form_error(self, web_client, mock_db):
        mock_db.execute = AsyncMock(side_effect=[_scalar(_app_settings()), _scalar(None)])
        r = _post(web_client, signup_source="lobste.rs", confirm_password="different")
        assert r.status_code == 422
        assert 'name="signup_source" value="lobste.rs"' in r.text

    def test_kept_through_the_duplicate_race(self, web_client, mock_db):
        from sqlalchemy.exc import IntegrityError
        mock_db.execute = AsyncMock(side_effect=[_scalar(_app_settings()), _scalar(None)])
        mock_db.commit = AsyncMock(side_effect=IntegrityError("x", {}, Exception()))
        r = _post(web_client, signup_source="lobste.rs")
        assert r.status_code == 409
        assert 'name="signup_source" value="lobste.rs"' in r.text


# ── Switching counting off ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def test_switching_counting_off_clears_every_source(pg):
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name="x",
                signup_source="lobste.rs")
    pg.add(user)
    await pg.flush()
    with patch.object(ts, "flush", new=AsyncMock()):
        await ts.apply_enabled(pg, False)
    assert await pg.scalar(select(User.signup_source).where(User.id == user.id)) is None


async def test_funnel_names_top_sources_and_folds_the_rest():
    rows = [SimpleNamespace(source=s, n=n) for s, n in
            [("a.com", 9), ("b.com", 8), (None, 7), ("c.com", 6), ("d.com", 5),
             ("e.com", 4), ("f.com", 3), ("g.com", 2)]]
    out = ts._top_signup_sources(rows)
    assert [o["source"] for o in out] == [
        "a.com", "b.com", "c.com", "d.com", "e.com", "other", "not recorded"]
    assert out[-2]["count"] == 5 and out[-1]["count"] == 7


class TestLanding:
    def _landing_ctx(self, web_client, **headers):
        from starlette.responses import HTMLResponse
        seen = {}

        def capture(request, name, ctx=None, **kw):
            seen.update(ctx or {})
            return HTMLResponse("")

        with patch("app.routers.web.auth.get_registration_enabled", new=AsyncMock(return_value=True)), \
             patch("app.routers.web.auth.templates.TemplateResponse", side_effect=capture):
            web_client.get("/", headers=headers)
        return seen

    def test_register_link_carries_the_referrer(self, web_client):
        ctx = self._landing_ctx(web_client, referer="https://news.ycombinator.com/item?id=1")
        assert ctx["register_url"] == "/register?src=news.ycombinator.com"

    def test_plain_link_while_counting_is_off(self, web_client):
        ts.set_enabled(False)
        ctx = self._landing_ctx(web_client, referer="https://news.ycombinator.com/")
        assert ctx["register_url"] == "/register"
