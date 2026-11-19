"""AI cost is priced by the model that ran the work, not the one configured today.

The table used to price both periods by the current slot, so moving from Haiku 4.5
to 5.5 cut last month's cost tenfold and reported a falling bill as a rising trend.
The database test uses the real (dev) database in one transaction, always rolled
back, and is skipped when the database is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import AiUsageLog, Article, ArticleAiJob
from app.models.feed import Feed
from app.models.user import User, UserSettings
from app.services.stats_service import _price_usage, get_ai_cost_stats

NOW = datetime.now(timezone.utc)
HAIKU_OLD = "claude-haiku-4-5"    # $1 in / $5 out
HAIKU_NEW = "claude-haiku-5-5"    # $0.10 in / $0.50 out


def test_each_group_is_priced_by_its_own_model():
    usage = _price_usage(
        [
            (HAIKU_OLD, "anthropic", 10, 1_000_000, 0),
            (HAIKU_NEW, "anthropic", 30, 1_000_000, 0),
        ],
        HAIKU_NEW, "anthropic",
    )
    assert usage.count == 40
    assert usage.input_tokens == 2_000_000
    assert usage.cost == 1.10
    assert usage.used_current_model is False


def test_changing_the_slot_leaves_recorded_runs_alone():
    groups = [(HAIKU_OLD, "anthropic", 10, 1_000_000, 200_000)]
    assert (
        _price_usage(groups, HAIKU_OLD, "anthropic").cost
        == _price_usage(groups, "claude-opus-5-5", "anthropic").cost
    )


def test_run_without_a_recorded_model_falls_back_to_the_slot_and_says_so():
    usage = _price_usage([(None, None, 5, 1_000_000, 0)], HAIKU_NEW, "anthropic")
    assert usage.cost == 0.10
    assert usage.used_current_model is True


def test_run_that_spent_nothing_does_not_raise_the_fallback_note():
    usage = _price_usage([(None, None, 5, 0, 0)], HAIKU_NEW, "anthropic")
    assert usage.used_current_model is False


def test_no_model_and_no_fallback_stays_unpriced():
    # Catch-up runs that found no articles: no model, no tokens, no invented price.
    usage = _price_usage([(None, None, 3, 0, 0)], None, None)
    assert usage.count == 3
    assert usage.cost is None


def test_custom_endpoint_stays_unpriced_after_moving_to_a_paid_slot():
    # Its runs must not take on the paid model's rate, whatever name they used.
    usage = _price_usage(
        [
            ("gpt-4o", "custom", 10, 1_000_000, 0),
            (HAIKU_NEW, "anthropic", 10, 1_000_000, 0),
        ],
        HAIKU_NEW, "anthropic",
    )
    assert usage.cost == 0.10
    assert usage.used_custom is True


def test_no_groups_means_no_cost():
    assert _price_usage([], HAIKU_NEW, "anthropic").cost is None


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
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_cheaper_model_shows_as_a_falling_cost(pg):
    """More tokens on the cheaper model: tokens up, cost down, and the trend agrees."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"cost_{u}@test.invalid", password_hash="x", display_name="t")
    pg.add(user)
    await pg.flush()
    pg.add(UserSettings(
        user_id=user.id, ai_fast_provider="anthropic", ai_fast_model=HAIKU_NEW,
        ai_quality_provider="anthropic", ai_quality_model="claude-sonnet-5-5",
    ))
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    pg.add(feed)
    await pg.flush()

    async def job(model: str, tokens: int, age_days: int) -> None:
        g = uuid.uuid4().hex
        article = Article(feed_id=feed.id, guid=g, guid_hash=g, title="t", content="b",
                          fetched_at=NOW, readable_status="skipped")
        pg.add(article)
        await pg.flush()
        pg.add(ArticleAiJob(
            article_id=article.id, user_id=user.id, operation="scoring", status="success",
            provider="anthropic", model=model, input_tokens=tokens, output_tokens=0,
            processed_at=NOW - timedelta(days=age_days),
        ))

    await job(HAIKU_OLD, 1_000_000, 45)   # $1.00 in the previous period
    await job(HAIKU_NEW, 1_500_000, 5)    # $0.15 in this one
    pg.add(AiUsageLog(
        user_id=user.id, operation="preference_generation", model="claude-sonnet-4-6",
        provider="anthropic", input_tokens=1_000_000, output_tokens=0,
        created_at=NOW - timedelta(days=45),
    ))
    pg.add(AiUsageLog(
        user_id=user.id, operation="preference_generation", model="claude-sonnet-5-5",
        provider="anthropic", input_tokens=1_000_000, output_tokens=0,
        created_at=NOW - timedelta(days=5),
    ))
    await pg.flush()

    stats = await get_ai_cost_stats(user.id, pg, days=30)
    rows = {r.operation: r for r in stats.operations}

    assert rows["scoring"].input_tokens == 1_500_000
    assert rows["scoring"].est_cost == 0.15
    assert rows["scoring"].trend_pct == -85.0

    assert rows["preference_generation"].est_cost == 2.0
    assert rows["preference_generation"].trend_pct == pytest.approx(-33.3)

    assert rows["_total_all"].est_cost == 2.15
    assert rows["_total_all"].trend_pct == -46.2

    # Nothing here is priced by the slot: no chat, every run recorded its model.
    assert stats.prices_follow_current_model is False
