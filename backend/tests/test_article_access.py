"""Tests for client-driven state-write access guards (#13):
filter_accessible_article_ids + mark_articles_read_batch."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.article import filter_accessible_article_ids, mark_articles_read_batch


def _make_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


class TestFilterAccessibleArticleIds:
    async def test_returns_only_accessible_subset(self):
        db = _make_db()
        result = MagicMock()
        result.all.return_value = [(1,), (3,)]  # 2 was inaccessible
        db.execute.return_value = result

        out = await filter_accessible_article_ids(5, [1, 2, 3], db)
        assert out == [1, 3]

    async def test_empty_input_skips_query(self):
        db = _make_db()
        out = await filter_accessible_article_ids(5, [], db)
        assert out == []
        db.execute.assert_not_called()


class TestMarkArticlesReadBatch:
    async def test_skips_upsert_when_none_accessible(self):
        db = _make_db()
        user = SimpleNamespace(id=5)
        with patch("app.services.article.filter_accessible_article_ids",
                   new=AsyncMock(return_value=[])):
            await mark_articles_read_batch(user, [1, 2, 3], db)
        # Nothing accessible → no upsert, no commit.
        db.execute.assert_not_called()
        db.commit.assert_not_called()

    async def test_upserts_only_accessible_ids(self):
        db = _make_db()
        user = SimpleNamespace(id=5)
        with (
            patch("app.services.article.filter_accessible_article_ids",
                  new=AsyncMock(return_value=[1, 3])),
            patch("app.services.article._recalculate_unread_counts", new=AsyncMock()),
        ):
            await mark_articles_read_batch(user, [1, 2, 3], db)
        db.execute.assert_awaited_once()
        db.commit.assert_awaited_once()
