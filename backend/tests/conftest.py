"""Shared pytest fixtures for Readfine test suite."""
# ── IMPORTANT: env vars must be set BEFORE any app module is imported ─────────
import os
os.environ["ALLOWED_HOSTS"] = '["testserver","localhost","127.0.0.1"]'

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# ── Null lifespan: no DB/scheduler startup ────────────────────────────────────

@asynccontextmanager
async def _null_lifespan(app):
    yield


def _apply_null_lifespan():
    from app.main import app as _app
    _app.router.lifespan_context = _null_lifespan


_apply_null_lifespan()


# ── Outbound HTTP ─────────────────────────────────────────────────────────────

@contextmanager
def mock_httpx_client(handler):
    """Patch the httpx.Client used by _resolve_response to use a MockTransport, so
    tests exercise the REAL redirect/304/error handling instead of mocking it out.

    Everything that fetches an outside URL goes through that one client, so this
    covers the readable extraction path as well as the feed fetcher.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    with patch("app.utils.url_validator.httpx.Client", factory):
        yield


@contextmanager
def allowed_private_ai_hosts(entries: str):
    """Set AI_ALLOWED_PRIVATE_HOSTS to *entries* for the duration of the block.

    Not a plain patch.object on the parsed set: that is a cached_property, so it
    lives in the instance dict and setting it would go through pydantic's
    __setattr__, which knows no such field. Setting the string the property is
    built from and dropping the cache is both simpler and closer to what an
    operator actually changes.
    """
    from app.config import settings

    previous = settings.ai_allowed_private_hosts
    settings.ai_allowed_private_hosts = entries
    settings.__dict__.pop("allowed_private_endpoints", None)
    try:
        yield
    finally:
        settings.ai_allowed_private_hosts = previous
        settings.__dict__.pop("allowed_private_endpoints", None)


# ── Mock objects ──────────────────────────────────────────────────────────────

def db_unreachable(exc: Exception) -> None:
    """Called by integration fixtures when the test DB can't be reached. Fails rather
    than skips: a green run with the integration tests quietly left out looks like a
    pass. Running only the unit tests means picking their files."""
    pytest.fail(f"Integration DB unreachable: {exc}", pytrace=False)


# ── Integration DB reachability ───────────────────────────────────────────────
#
# Each integration fixture connects on its own. With Postgres down, a refused
# connection still takes about two seconds on Windows, which over ~500 tests is a
# quarter of an hour before the failures show. So probe once per session and fail
# those tests at setup instead. They are recognised by a fixture that calls
# db_unreachable; tests that don't touch the DB run as usual.

_db_reachable: bool | None = None
_db_fixture_cache: dict[object, bool] = {}


def _probe_db() -> bool:
    import socket
    from sqlalchemy.engine import make_url
    from app.config import settings

    url = make_url(settings.database_url)
    try:
        socket.create_connection((url.host or "localhost", url.port or 5432), timeout=5).close()
    except OSError:
        return False
    return True


def _uses_db(fixturedef) -> bool:
    import inspect

    func = fixturedef.func
    if func not in _db_fixture_cache:
        try:
            source = inspect.getsource(inspect.unwrap(func))
        except (OSError, TypeError):
            source = ""
        _db_fixture_cache[func] = "db_unreachable(" in source
    return _db_fixture_cache[func]


def pytest_runtest_setup(item):
    global _db_reachable
    defs = getattr(item, "_fixtureinfo", None)
    if defs is None:
        return
    if not any(_uses_db(d) for ds in defs.name2fixturedefs.values() for d in ds):
        return
    if _db_reachable is None:
        _db_reachable = _probe_db()
    if not _db_reachable:
        pytest.fail("Integration DB unreachable (probed once this session). Is Postgres running?",
                    pytrace=False)


def make_mock_user(id: int = 1, role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        email=f"user{id}@test.com",
        display_name=f"Test User {id}",
        role=role,
        is_active=True,
        created_at=datetime(2024, 1, 1),
        password_hash="dummy",
        # Far ahead, so touch_last_active never writes: the mock is shared across tests
        # and a write would both mutate it and add a commit the route tests do not expect.
        last_active_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )


MOCK_USER = make_mock_user(id=1, role="user")
MOCK_ADMIN = make_mock_user(id=2, role="admin")


def make_mock_db() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    session.execute = AsyncMock()
    return session


def make_scalar_result(value):
    """Return a mock that behaves like a SQLAlchemy scalar result."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    result.scalars.return_value.all.return_value = value if isinstance(value, list) else []
    result.one_or_none.return_value = value
    result.rowcount = 1 if value else 0
    return result


# ── Dormancy ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _dormancy_off(request):
    """Dormancy off unless a test asks for the real policy (``real_dormancy`` marker).

    Recording activity and queueing AI work both read the dormancy settings first. A
    mock DB cannot answer that query, and the tests built on one are about something
    else. test_dormancy.py covers the rule itself against Postgres.
    """
    if request.node.get_closest_marker("real_dormancy"):
        yield
        return
    from app.services.dormancy_service import DormancyPolicy
    with patch("app.services.dormancy_service.load_policy",
               new=AsyncMock(return_value=DormancyPolicy())):
        yield


# ── Client fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    return make_mock_db()


@pytest.fixture
def client(mock_db):
    """Authenticated client (regular user) with mocked DB."""
    from app.main import app
    from app.auth.dependencies import get_api_user, get_current_user
    from app.database import get_db

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_api_user] = lambda: MOCK_USER
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(mock_db):
    """Authenticated client (admin) with mocked DB."""
    from app.main import app
    from app.auth.dependencies import get_api_user, get_current_user
    from app.database import get_db

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_api_user] = lambda: MOCK_ADMIN
    app.dependency_overrides[get_current_user] = lambda: MOCK_ADMIN
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client(mock_db):
    """Client with NO get_api_user override → 401 for protected endpoints."""
    from app.main import app
    from app.database import get_db

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()
