"""A label goes when the article it sits on is no longer the reader's to open.

The label view lists by ArticleLabel alone, so a label left behind kept an article
in it that the detail, the read toggle and mark-all-read all refused, and the label's
unread badge never cleared. Integration: the rule is a correlated DELETE.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.article import ArticleStateUpdate
from app.services.article import toggle_article_state, update_article_state
from app.services.feed import unsubscribe
from app.services.saved_article_service import unsave_article
from tests.test_saved_articles import pg  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _user(pg):
    u = User(email=f"lbl_{uuid.uuid4().hex[:12]}@test.invalid", password_hash="x", display_name="t")
    pg.add(u)
    await pg.flush()
    return u


async def _article(pg, feed_id):
    u = uuid.uuid4().hex
    a = Article(feed_id=feed_id, guid=u, guid_hash=u, title="T", url=f"https://ex.invalid/{u}",
                url_normalized=f"https://ex.invalid/{u}", content="<p>b</p>",
                readable_status="success", fetched_at=datetime.now(timezone.utc))
    pg.add(a)
    await pg.flush()
    return a


async def _label(pg, user, article):
    lab = Label(user_id=user.id, name=f"L{uuid.uuid4().hex[:6]}", color="#fff")
    pg.add(lab)
    await pg.flush()
    pg.add(ArticleLabel(user_id=user.id, article_id=article.id, label_id=lab.id))
    await pg.flush()


async def _labels(pg, user, article):
    return (await pg.execute(select(ArticleLabel).where(
        ArticleLabel.user_id == user.id, ArticleLabel.article_id == article.id,
    ))).scalars().all()


async def _shared_feed(pg, *users):
    feed = Feed(feed_url=f"https://ex.invalid/{uuid.uuid4().hex}.xml", title="f",
                subscriber_count=len(users))
    pg.add(feed)
    await pg.flush()
    subs = []
    for u in users:
        uf = UserFeed(user_id=u.id, feed_id=feed.id)
        pg.add(uf)
        subs.append(uf)
    await pg.flush()
    return feed, subs


@pytest.fixture
def no_commit(pg):
    with patch.object(pg, "commit", AsyncMock()), patch.object(pg, "refresh", AsyncMock()):
        yield


async def test_unsubscribe_drops_labels_on_articles_left_behind(pg, no_commit):
    reader, other = await _user(pg), await _user(pg)
    feed, (uf, _) = await _shared_feed(pg, reader, other)
    article = await _article(pg, feed.id)
    await _label(pg, reader, article)

    await unsubscribe(reader, uf.id, pg)
    assert await _labels(pg, reader, article) == []


async def test_unsubscribe_keeps_labels_on_starred_articles(pg, no_commit):
    reader, other = await _user(pg), await _user(pg)
    feed, (uf, _) = await _shared_feed(pg, reader, other)
    article = await _article(pg, feed.id)
    pg.add(UserArticleState(user_id=reader.id, article_id=article.id, is_starred=True))
    await _label(pg, reader, article)

    await unsubscribe(reader, uf.id, pg)
    assert len(await _labels(pg, reader, article)) == 1


async def test_unstarring_an_orphan_drops_its_labels(pg, no_commit):
    reader = await _user(pg)
    article = await _article(pg, None)
    pg.add(UserArticleState(user_id=reader.id, article_id=article.id, is_starred=True))
    await _label(pg, reader, article)

    await toggle_article_state(reader, article.id, "is_starred", pg)
    assert await _labels(pg, reader, article) == []


async def test_unstarring_a_subscribed_article_keeps_its_labels(pg, no_commit):
    reader = await _user(pg)
    feed, _ = await _shared_feed(pg, reader)
    article = await _article(pg, feed.id)
    pg.add(UserArticleState(user_id=reader.id, article_id=article.id, is_starred=True))
    await _label(pg, reader, article)

    await update_article_state(reader, article.id, ArticleStateUpdate(is_starred=False), pg)
    assert len(await _labels(pg, reader, article)) == 1


async def test_unsaving_drops_labels_but_not_while_archived(pg, no_commit):
    reader = await _user(pg)
    article = await _article(pg, None)
    pg.add(UserArticleState(user_id=reader.id, article_id=article.id,
                            saved_at=datetime.now(timezone.utc), is_archived=True))
    await _label(pg, reader, article)

    await unsave_article(article.id, reader.id, pg)
    assert len(await _labels(pg, reader, article)) == 1, "still archived, still theirs"

    await update_article_state(reader, article.id, ArticleStateUpdate(is_archived=False), pg)
    assert await _labels(pg, reader, article) == []
