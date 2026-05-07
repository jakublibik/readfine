"""Unit tests for label_service — all DB calls are mocked."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.label import LabelCreate, LabelUpdate
from app.services.label_service import (
    create_label,
    delete_label,
    list_labels,
    update_label,
    assign_label,
    remove_label,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(id=1):
    return SimpleNamespace(id=id)


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock()
    return db


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalars.return_value = iter(value if isinstance(value, list) else [])
    return r


def _make_label(id=1, name="Tech", color="#ff0000", position=0):
    return SimpleNamespace(
        id=id,
        name=name,
        color=color,
        position=position,
        user_id=1,
        created_at=datetime(2024, 1, 1),
    )


# ── list_labels ───────────────────────────────────────────────────────────────

class TestListLabels:
    async def test_returns_label_responses(self):
        user = _make_user()
        db = _make_db()
        label = _make_label()
        result = MagicMock()
        result.scalars.return_value = [label]
        db.execute.return_value = result

        out = await list_labels(user, db)

        assert len(out) == 1
        assert out[0].id == 1
        assert out[0].name == "Tech"

    async def test_empty_list(self):
        user = _make_user()
        db = _make_db()
        result = MagicMock()
        result.scalars.return_value = []
        db.execute.return_value = result

        out = await list_labels(user, db)
        assert out == []

    async def test_queries_by_user_id(self):
        user = _make_user(id=42)
        db = _make_db()
        result = MagicMock()
        result.scalars.return_value = []
        db.execute.return_value = result

        await list_labels(user, db)
        db.execute.assert_called_once()


# ── create_label ──────────────────────────────────────────────────────────────

class TestCreateLabel:
    async def test_creates_and_returns(self):
        user = _make_user()
        db = _make_db()
        label = _make_label(id=5, name="News")

        async def mock_refresh(obj):
            obj.id = 5
            obj.created_at = datetime(2024, 1, 1)

        db.refresh.side_effect = mock_refresh

        payload = LabelCreate(name="News", color="#aabbcc")
        out = await create_label(user, payload, db)

        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

    async def test_commits_to_db(self):
        user = _make_user()
        db = _make_db()

        async def mock_refresh(obj):
            obj.id = 1
            obj.created_at = datetime(2024, 1, 1)

        db.refresh.side_effect = mock_refresh

        payload = LabelCreate(name="Sports")
        await create_label(user, payload, db)
        db.commit.assert_awaited_once()


# ── update_label ──────────────────────────────────────────────────────────────

class TestUpdateLabel:
    async def test_returns_none_when_not_found(self):
        user = _make_user()
        db = _make_db()
        db.execute.return_value = _scalar_result(None)

        result = await update_label(user, label_id=99, payload=LabelUpdate(name="X"), db=db)
        assert result is None

    async def test_updates_fields(self):
        user = _make_user()
        db = _make_db()
        label = _make_label(name="Old")
        db.execute.return_value = _scalar_result(label)

        async def mock_refresh(obj):
            pass

        db.refresh.side_effect = mock_refresh

        result = await update_label(user, label_id=1, payload=LabelUpdate(name="New"), db=db)

        assert label.name == "New"
        db.commit.assert_awaited_once()

    async def test_partial_update_preserves_other_fields(self):
        user = _make_user()
        db = _make_db()
        label = _make_label(name="Tech", color="#111111")
        db.execute.return_value = _scalar_result(label)
        db.refresh.side_effect = AsyncMock()

        await update_label(user, label_id=1, payload=LabelUpdate(color="#222222"), db=db)

        assert label.name == "Tech"
        assert label.color == "#222222"


# ── delete_label ──────────────────────────────────────────────────────────────

class TestDeleteLabel:
    async def test_returns_none_when_not_found(self):
        user = _make_user()
        db = _make_db()
        db.execute.return_value = _scalar_result(None)

        result = await delete_label(user, label_id=99, db=db)
        assert result is None

    async def test_deletes_and_returns_label(self):
        user = _make_user()
        db = _make_db()
        label = _make_label()
        db.execute.return_value = _scalar_result(label)

        result = await delete_label(user, label_id=1, db=db)

        db.delete.assert_awaited_once_with(label)
        db.commit.assert_awaited_once()
        assert result is label


# ── assign_label ──────────────────────────────────────────────────────────────

class TestAssignLabel:
    async def test_returns_false_when_label_not_found(self):
        user = _make_user()
        db = _make_db()
        db.execute.return_value = _scalar_result(None)

        result = await assign_label(user, article_id=1, label_id=99, db=db)
        assert result is False

    async def test_returns_true_when_already_assigned(self):
        user = _make_user()
        db = _make_db()
        existing_al = SimpleNamespace(id=1)
        # Call sequence: label check → article access → existing check
        db.execute.side_effect = [
            _scalar_result(1),           # label exists
            _scalar_result(1),           # article accessible
            _scalar_result(existing_al), # already assigned
        ]

        result = await assign_label(user, article_id=1, label_id=1, db=db)
        assert result is True
        db.add.assert_not_called()

    async def test_creates_new_assignment(self):
        user = _make_user()
        db = _make_db()
        db.execute.side_effect = [
            _scalar_result(1),    # label exists
            _scalar_result(1),    # article accessible
            _scalar_result(None), # not yet assigned
        ]

        result = await assign_label(user, article_id=1, label_id=1, db=db)

        assert result is True
        db.add.assert_called_once()
        db.commit.assert_awaited_once()


# ── remove_label ──────────────────────────────────────────────────────────────

class TestRemoveLabel:
    async def test_returns_true_on_success(self):
        user = _make_user()
        db = _make_db()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        db.execute.return_value = mock_result

        result = await remove_label(user, article_id=1, label_id=1, db=db)
        assert result is True

    async def test_returns_false_when_not_found(self):
        user = _make_user()
        db = _make_db()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        db.execute.return_value = mock_result

        result = await remove_label(user, article_id=1, label_id=99, db=db)
        assert result is False
