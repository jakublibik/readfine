"""Shared pytest fixtures for Readfine test suite."""
# ── IMPORTANT: env vars must be set BEFORE any app module is imported ─────────
import os
os.environ["ALLOWED_HOSTS"] = '["testserver","localhost","127.0.0.1"]'

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


# ── Mock objects ──────────────────────────────────────────────────────────────

def make_mock_user(id: int = 1, role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        email=f"user{id}@test.com",
        display_name=f"Test User {id}",
        role=role,
        is_active=True,
        created_at=datetime(2024, 1, 1),
        password_hash="dummy",
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
