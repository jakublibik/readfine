"""A filter regex that hits the match timeout switches its filter off.

The timeout bounds one match. Without the switch-off, a pattern that times out
on one article times out on every article, and a test over a few hundred of them
held the event loop, and with it the whole instance, for minutes.

Also here: a score that lands after the reader unsubscribed does not run their
filters, and unsubscribing drops the AI jobs still queued for that feed.
"""
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import ArticleLabel, Label
from app.schemas.filter import FilterUpdate
from app.services import filter_service
from app.services.feed import unsubscribe
from app.services.filter_service import (
    REGEX_TIMEOUT,
    _evaluate,
    apply_filter_retroactively,
    preview_filter_retroactive,
    process_ai_filters_batch,
    test_filter as run_filter_test,
    update_filter,
)
from tests.test_label_access_cleanup import _article, _shared_feed, _user, no_commit  # noqa: F401
from tests.test_saved_articles import pg  # noqa: F401

SLOW = r"(a{1,9}){1,99}b"


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(filter_service, "_REGEX_MATCH_TIMEOUT_S", 0.05)


def _slow_filter():
    return SimpleNamespace(
        id=1, user_id=1, is_active=True, disabled_reason=None, match_operator="AND",
        scope_include=None, scope_except=None, actions=[],
        conditions=[SimpleNamespace(field="title_or_content", operator="regex",
                                    value=SLOW, position=0)],
    )


def test_first_timeout_switches_the_filter_off_and_later_articles_skip_it(short_timeout):
    f = _slow_filter()
    article = SimpleNamespace(id=1, feed_id=10, title="a" * 5000, content="a" * 5000,
                              author="", url="", published_at=None)

    assert _evaluate(f, article) is False
    assert f.is_active is False
    assert f.disabled_reason == REGEX_TIMEOUT

    start = time.monotonic()
    for _ in range(50):
        assert _evaluate(f, article) is False
    assert time.monotonic() - start < 0.05


async def _db_filter(pg, user, value=SLOW, with_label=False):
    f = Filter(user_id=user.id, name="slow", is_active=True)
    f.conditions.append(FilterCondition(field="content", operator="regex", value=value))
    if with_label:
        lab = Label(user_id=user.id, name=f"L{uuid.uuid4().hex[:6]}", color="#ffffff")
        pg.add(lab)
        await pg.flush()
        f.actions.append(FilterAction(action_type="label", action_value=str(lab.id)))
    pg.add(f)
    await pg.flush()
    return f


async def _slow_articles(pg, feed_id, n=3):
    out = []
    for _ in range(n):
        a = await _article(pg, feed_id)
        a.content = "a" * 5000
        out.append(a)
    await pg.flush()
    return out


@pytest.mark.asyncio
async def test_filter_test_stops_at_the_first_timeout(pg, no_commit, short_timeout):
    reader = await _user(pg)
    feed, _ = await _shared_feed(pg, reader)
    await _slow_articles(pg, feed.id)
    f = await _db_filter(pg, reader)

    calls = 0
    real = filter_service.evaluate_filter

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    with patch.object(filter_service, "evaluate_filter", counting):
        result = await run_filter_test(reader.id, f.id, pg)
        assert calls == 1
        assert result.error
        assert f.is_active is False and f.disabled_reason == REGEX_TIMEOUT

        # Switched off: a second test answers without scanning again.
        again = await run_filter_test(reader.id, f.id, pg)
        assert again.error and calls == 1


@pytest.mark.asyncio
async def test_retroactive_apply_does_nothing_after_a_timeout(pg, no_commit, short_timeout):
    reader = await _user(pg)
    feed, _ = await _shared_feed(pg, reader)
    await _slow_articles(pg, feed.id)
    f = await _db_filter(pg, reader)

    preview = await preview_filter_retroactive(reader.id, f.id, pg)
    assert preview["error"] and preview["matched"] == 0
    assert await apply_filter_retroactively(reader.id, f.id, pg) == (0, 0, 0)


@pytest.mark.asyncio
async def test_saving_the_filter_clears_the_reason(pg, no_commit):
    reader = await _user(pg)
    f = await _db_filter(pg, reader)
    f.is_active, f.disabled_reason = False, REGEX_TIMEOUT
    await pg.flush()

    updated = await update_filter(reader.id, f.id, FilterUpdate(is_active=True), pg)
    assert updated.is_active is True
    assert updated.disabled_reason is None


# ── a score that lands after unsubscribing ──────────────────────────────────────

async def _jobs(pg, user, article):
    return (await pg.execute(select(ArticleAiJob).where(
        ArticleAiJob.user_id == user.id, ArticleAiJob.article_id == article.id,
    ))).scalars().all()


@pytest.mark.asyncio
async def test_unsubscribe_drops_pending_jobs_but_not_on_kept_articles(pg, no_commit):
    reader, other = await _user(pg), await _user(pg)
    feed, (uf, _) = await _shared_feed(pg, reader, other)
    dropped, starred = await _article(pg, feed.id), await _article(pg, feed.id)
    pg.add(UserArticleState(user_id=reader.id, article_id=starred.id, is_starred=True))
    for a in (dropped, starred):
        pg.add(ArticleAiJob(article_id=a.id, user_id=reader.id, operation="scoring",
                            status="pending"))
    await pg.flush()

    await unsubscribe(reader, uf.id, pg)
    assert await _jobs(pg, reader, dropped) == []
    assert len(await _jobs(pg, reader, starred)) == 1


@pytest.mark.asyncio
async def test_ai_filters_skip_an_article_the_reader_let_go_of(pg, no_commit):
    reader, other = await _user(pg), await _user(pg)
    feed, _ = await _shared_feed(pg, other)  # the reader no longer subscribes
    article = await _article(pg, feed.id)
    f = await _db_filter(pg, reader, with_label=True)
    f.conditions[0].field, f.conditions[0].operator, f.conditions[0].value = "ai_score", "gte", "10"
    state = UserArticleState(user_id=reader.id, article_id=article.id, ai_score=0.9,
                             ai_filters_applied=False)
    pg.add(state)
    await pg.flush()

    with patch("app.services.ai_jobs.ai_enabled_globally", AsyncMock(return_value=True)):
        await process_ai_filters_batch(pg)

    assert state.ai_filters_applied is True
    labels = (await pg.execute(select(ArticleLabel).where(
        ArticleLabel.user_id == reader.id, ArticleLabel.article_id == article.id,
    ))).scalars().all()
    assert labels == []
