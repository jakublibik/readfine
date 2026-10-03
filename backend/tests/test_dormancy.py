"""Dormant accounts (services/dormancy_service.py) against Postgres.

The rule, the scheduler's gate on it, the warning job, the wake-up that shows the
banner, and the AI work held back for dormant readers. Runs inside a transaction that
is always rolled back; skips if the DB is unreachable. The dev DB may hold real users,
so assertions only look at the accounts a test creates, and the ones the job should
reach first are made older than anything real.
"""
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher.scheduler import _select_due_feeds
from app.models.auth import ApiToken
from app.models.feed import Feed, UserFeed
from app.models.settings import AppSettings
from app.models.user import User, UserCatchupConfig
from app.services import dormancy_service
from app.services.dormancy_service import (
    DormancyPolicy,
    dormant_feeds_sql,
    dormant_user_ids,
    policy_from_settings,
    send_dormancy_warnings,
    warned_users,
)
from app.services.user import FEEDS_RESUMED_SESSION_KEY, record_activity, touch_last_active

OFF = DormancyPolicy()
SILENT = DormancyPolicy(after_days=60)
WARNED = DormancyPolicy(after_days=60, warning_on=True)
LONG_AGO = timedelta(days=-400)
RECENTLY = timedelta(days=-5)


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _feed(pg) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"f-{u[:6]}",
                subscriber_count=1, status="active")
    pg.add(feed)
    await pg.flush()
    return feed


async def _subscribe(pg, user: User, feed: Feed) -> None:
    pg.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await pg.flush()


async def _user(pg, *, active=LONG_AGO, created=LONG_AGO, role="user", is_active=True,
                verified=True, feeds=1, warned=None) -> User:
    """An account whose last activity is *active* from now (None = never active),
    following *feeds* feeds of its own. *warned* stamps the warning that long ago."""
    now = _now()
    u = uuid.uuid4().hex
    user = User(
        email=f"{u}@ex.invalid", password_hash="x", display_name="Reader",
        role=role, is_active=is_active, email_verified=verified,
        last_active_at=None if active is None else now + active,
        pause_warning_sent_at=None if warned is None else now + warned,
    )
    user.created_at = now + created
    pg.add(user)
    await pg.flush()
    for _ in range(feeds):
        await _subscribe(pg, user, await _feed(pg))
    return user


async def _dormant(pg, user: User, policy: DormancyPolicy) -> bool:
    return user.id in await dormant_user_ids(pg, policy, _now())


async def _set_app_settings(pg, **values) -> AppSettings:
    s = await pg.get(AppSettings, 1)
    if s is None:
        s = AppSettings(id=1)
        pg.add(s)
    for k, v in values.items():
        setattr(s, k, v)
    await pg.flush()
    return s


# ── The rule ─────────────────────────────────────────────────────────────────

class TestRule:
    async def test_recently_active_is_awake(self, pg):
        assert not await _dormant(pg, await _user(pg, active=RECENTLY), SILENT)

    async def test_long_inactive_is_dormant(self, pg):
        assert await _dormant(pg, await _user(pg), SILENT)

    async def test_setting_off_nobody_sleeps(self, pg):
        assert not await _dormant(pg, await _user(pg), OFF)

    async def test_deactivated_is_not_counted_as_dormant(self, pg):
        # It keeps nothing fetched either, but has its own status in the admin.
        assert not await _dormant(pg, await _user(pg, is_active=False), SILENT)

    async def test_admin_never_sleeps(self, pg):
        assert not await _dormant(pg, await _user(pg, role="admin"), SILENT)

    async def test_account_without_feeds_never_sleeps(self, pg):
        assert not await _dormant(pg, await _user(pg, feeds=0), SILENT)

    async def test_briefing_keeps_awake(self, pg):
        user = await _user(pg)
        pg.add(UserCatchupConfig(user_id=user.id, name="b", briefing_enabled=True))
        await pg.flush()
        assert not await _dormant(pg, user, SILENT)

    async def test_never_active_counts_from_signup(self, pg):
        assert await _dormant(pg, await _user(pg, active=None), SILENT)
        assert not await _dormant(pg, await _user(pg, active=None, created=RECENTLY), SILENT)

    async def test_fresh_api_token_keeps_awake(self, pg):
        user = await _user(pg)
        pg.add(ApiToken(user_id=user.id, name="sync", token_hash=uuid.uuid4().hex,
                        token_prefix="rf_x", last_used_at=_now() + RECENTLY))
        await pg.flush()
        assert not await _dormant(pg, user, SILENT)

    async def test_revoked_token_does_not_count(self, pg):
        user = await _user(pg)
        pg.add(ApiToken(user_id=user.id, name="old", token_hash=uuid.uuid4().hex,
                        token_prefix="rf_y", last_used_at=_now() + RECENTLY,
                        revoked_at=_now()))
        await pg.flush()
        assert await _dormant(pg, user, SILENT)

    async def test_warning_on_but_not_sent_stays_awake(self, pg):
        assert not await _dormant(pg, await _user(pg), WARNED)

    async def test_warning_under_a_week_stays_awake(self, pg):
        user = await _user(pg, warned=timedelta(days=-3))
        assert not await _dormant(pg, user, WARNED)
        assert user.id in await warned_users(pg, WARNED, _now())

    async def test_warning_over_a_week_sleeps(self, pg):
        user = await _user(pg, warned=timedelta(days=-8))
        assert await _dormant(pg, user, WARNED)
        assert user.id not in await warned_users(pg, WARNED, _now())

    async def test_warning_older_than_activity_does_not_count(self, pg):
        # Came back after the warning, then left again: the old warning is spent.
        user = await _user(pg, active=timedelta(days=-100), warned=timedelta(days=-120))
        assert not await _dormant(pg, user, WARNED)

    def test_warning_needs_smtp(self):
        with_smtp = AppSettings(dormant_after_days=60, dormant_warning_enabled=True, smtp_host="mx")
        without = AppSettings(dormant_after_days=60, dormant_warning_enabled=True, smtp_host=None)
        assert policy_from_settings(with_smtp) == WARNED
        # Checkbox left on after SMTP was removed: sleeps unwarned, like no mail at all.
        assert policy_from_settings(without) == SILENT

    def test_empty_days_is_off(self):
        assert not policy_from_settings(AppSettings(dormant_after_days=None)).enabled


# ── The scheduler's gate ─────────────────────────────────────────────────────

async def _due(pg, policy: DormancyPolicy) -> set[int]:
    feeds = await _select_due_feeds(
        pg, _now(), default_interval=60, min_interval=15, max_interval=360, dormancy=policy,
    )
    return {f.id for f in feeds}


class TestSchedulerGate:
    async def test_shared_feed_with_one_awake_reader_is_fetched(self, pg):
        sleeper = await _user(pg, feeds=0)
        reader = await _user(pg, active=RECENTLY, feeds=0)
        feed = await _feed(pg)
        await _subscribe(pg, sleeper, feed)
        await _subscribe(pg, reader, feed)
        assert feed.id in await _due(pg, SILENT)

    async def test_feed_of_dormant_readers_only_is_skipped(self, pg):
        a, b = await _user(pg, feeds=0), await _user(pg, feeds=0)
        feed = await _feed(pg)
        await _subscribe(pg, a, feed)
        await _subscribe(pg, b, feed)
        assert feed.id not in await _due(pg, SILENT)
        assert feed.id in await _due(pg, OFF)

    async def test_feed_of_deactivated_readers_is_skipped_even_with_setting_off(self, pg):
        user = await _user(pg, active=RECENTLY, is_active=False, feeds=0)
        feed = await _feed(pg)
        await _subscribe(pg, user, feed)
        assert feed.id not in await _due(pg, OFF)

    async def test_admin_marks_only_feeds_that_would_be_fetched(self, pg):
        user = await _user(pg, feeds=0)
        live, paused = await _feed(pg), await _feed(pg)
        paused.status = "paused"
        await _subscribe(pg, user, live)
        await _subscribe(pg, user, paused)
        marked = set((await pg.scalars(
            select(Feed.id).where(dormant_feeds_sql(SILENT, _now()))
        )).all())
        assert live.id in marked
        assert paused.id not in marked


# ── Coming back ──────────────────────────────────────────────────────────────

@pytest.mark.real_dormancy
class TestWakeUp:
    async def test_dormant_with_a_paused_feed_wakes_with_banner(self, pg):
        await _set_app_settings(pg, dormant_after_days=60, dormant_warning_enabled=False)
        user = await _user(pg)
        assert await record_activity(user, pg) is True
        assert user.last_active_at > _now() - timedelta(minutes=1)
        assert not await _dormant(pg, user, SILENT)

    async def test_only_shared_feeds_means_no_banner(self, pg):
        await _set_app_settings(pg, dormant_after_days=60, dormant_warning_enabled=False)
        sleeper = await _user(pg, feeds=0)
        reader = await _user(pg, active=RECENTLY, feeds=0)
        feed = await _feed(pg)
        await _subscribe(pg, sleeper, feed)
        await _subscribe(pg, reader, feed)
        assert await record_activity(sleeper, pg) is False

    async def test_feed_that_stood_still_anyway_means_no_banner(self, pg):
        # Its only feed of its own was disabled: the sleep stopped nothing.
        await _set_app_settings(pg, dormant_after_days=60, dormant_warning_enabled=False)
        user = await _user(pg, feeds=0)
        feed = await _feed(pg)
        feed.status = "disabled"
        await _subscribe(pg, user, feed)
        assert await record_activity(user, pg) is False

    async def test_awake_account_gets_no_banner(self, pg):
        await _set_app_settings(pg, dormant_after_days=60, dormant_warning_enabled=False)
        user = await _user(pg, active=timedelta(days=-20))
        assert await record_activity(user, pg) is False

    async def test_open_tab_without_login_still_gets_the_banner(self, pg):
        # A sliding session outlives the absence: the first thing the reader does in
        # the open tab records the wake-up, no login involved.
        await _set_app_settings(pg, dormant_after_days=60, dormant_warning_enabled=False)
        user = await _user(pg)
        session: dict = {}
        await touch_last_active(user, pg, session)
        assert session.get(FEEDS_RESUMED_SESSION_KEY) is True


# ── The warning job ──────────────────────────────────────────────────────────

def _smtp(**extra) -> dict:
    return dict(dormant_after_days=60, dormant_warning_enabled=True,
                smtp_host="mx.ex.invalid", smtp_from_email="rf@ex.invalid", **extra)


def _sent_to(send, user: User) -> int:
    return sum(1 for call in send.call_args_list if call.args[1] == user.email)


class TestWarningJob:
    async def test_warns_once(self, pg):
        await _set_app_settings(pg, **_smtp())
        user = await _user(pg)
        with patch("app.utils.smtp.send_email") as send:
            await send_dormancy_warnings(pg)
            await send_dormancy_warnings(pg)
        assert _sent_to(send, user) == 1
        assert user.pause_warning_sent_at is not None

    async def test_warns_a_week_before_the_limit(self, pg):
        await _set_app_settings(pg, **_smtp())
        due = await _user(pg, active=timedelta(days=-54))
        early = await _user(pg, active=timedelta(days=-52))
        with patch("app.utils.smtp.send_email") as send:
            await send_dormancy_warnings(pg)
        assert _sent_to(send, due) == 1
        assert _sent_to(send, early) == 0

    async def test_nothing_with_the_warning_off(self, pg):
        await _set_app_settings(pg, **_smtp() | {"dormant_warning_enabled": False})
        user = await _user(pg)
        with patch("app.utils.smtp.send_email") as send:
            await send_dormancy_warnings(pg)
        assert _sent_to(send, user) == 0

    async def test_skips_unverified_and_feedless_accounts(self, pg):
        await _set_app_settings(pg, **_smtp())
        unverified = await _user(pg, verified=False)
        feedless = await _user(pg, feeds=0)
        with patch("app.utils.smtp.send_email") as send:
            await send_dormancy_warnings(pg)
        assert _sent_to(send, unverified) == 0
        assert _sent_to(send, feedless) == 0

    async def test_warns_readers_of_shared_feeds_too(self, pg):
        await _set_app_settings(pg, **_smtp())
        sleeper = await _user(pg, feeds=0)
        reader = await _user(pg, active=RECENTLY, feeds=0)
        feed = await _feed(pg)
        await _subscribe(pg, sleeper, feed)
        await _subscribe(pg, reader, feed)
        with patch("app.utils.smtp.send_email") as send:
            await send_dormancy_warnings(pg)
        assert _sent_to(send, sleeper) == 1

    async def test_warns_again_after_coming_back_and_leaving(self, pg):
        await _set_app_settings(pg, **_smtp())
        user = await _user(pg, active=timedelta(days=-100), warned=timedelta(days=-120))
        with patch("app.utils.smtp.send_email") as send:
            await send_dormancy_warnings(pg)
        assert _sent_to(send, user) == 1

    async def test_caps_a_run(self, pg):
        await _set_app_settings(pg, **_smtp())
        # Older than any real account in the dev DB, so they come first.
        users = [await _user(pg, active=None, created=timedelta(days=-9000 + i)) for i in range(3)]
        with patch("app.utils.smtp.send_email"), \
             patch.object(dormancy_service, "WARNING_BATCH_LIMIT", 2):
            await send_dormancy_warnings(pg)
        assert [u.pause_warning_sent_at is not None for u in users] == [True, True, False]

    async def test_refused_recipient_is_stamped(self, pg):
        await _set_app_settings(pg, **_smtp())
        user = await _user(pg, active=None, created=timedelta(days=-9000))
        refused = smtplib.SMTPRecipientsRefused({user.email: (550, b"no such user")})
        with patch("app.utils.smtp.send_email", side_effect=refused):
            await send_dormancy_warnings(pg)
        assert user.pause_warning_sent_at is not None

    async def test_temporary_refusal_is_retried_not_stamped(self, pg):
        # Greylisting answers 4xx with the same exception; the rest of the run goes on.
        await _set_app_settings(pg, **_smtp())
        first = await _user(pg, active=None, created=timedelta(days=-9000))
        second = await _user(pg, active=None, created=timedelta(days=-8999))
        deferred = smtplib.SMTPRecipientsRefused({first.email: (450, b"try again later")})

        def send(_s, to, *_a):
            if to == first.email:
                raise deferred

        with patch("app.utils.smtp.send_email", side_effect=send):
            await send_dormancy_warnings(pg)
        assert first.pause_warning_sent_at is None
        assert second.pause_warning_sent_at is not None

    async def test_connection_failure_stamps_nothing(self, pg):
        await _set_app_settings(pg, **_smtp())
        user = await _user(pg, active=None, created=timedelta(days=-9000))
        with patch("app.utils.smtp.send_email",
                   side_effect=smtplib.SMTPServerDisconnected("gone")) as send:
            assert await send_dormancy_warnings(pg) == 0
        assert send.call_count == 1
        assert user.pause_warning_sent_at is None

    def test_email_links_only_with_public_url(self):
        _, with_url = dormancy_service.warning_email("Ann", 60, "October 6, 2026", "https://rf.test/")
        _, without = dormancy_service.warning_email("Ann", 60, "October 6, 2026", None)
        assert "https://rf.test/login" in with_url
        assert "http" not in without


# ── AI work held back ────────────────────────────────────────────────────────

class TestAiJobs:
    """A dormant reader of a shared feed gets its articles, but no AI work on them."""

    async def test_scoring_not_queued_for_dormant_reader(self):
        from app.services.ai_scoring_service import enqueue_scoring_job
        from tests.test_ai_pipeline import (
            make_article, make_execute_result, make_mock_db, make_settings, make_user_feed,
        )
        for dormant, expected in ((True, False), (False, True)):
            db = make_mock_db()
            db.scalar = AsyncMock(side_effect=[True, make_settings(), make_user_feed(), None])
            db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
            with patch.object(dormancy_service, "is_user_dormant", new=AsyncMock(return_value=dormant)):
                assert await enqueue_scoring_job(make_article(), user_id=1, db=db) is expected

    async def test_summary_not_queued_for_dormant_reader(self):
        from app.services.ai_summary_service import enqueue_summary_job
        from tests.test_ai_pipeline import (
            make_article, make_execute_result, make_mock_db, make_settings, make_user_feed,
        )
        for dormant, expected in ((True, False), (False, True)):
            db = make_mock_db()
            db.scalar = AsyncMock(side_effect=[True, make_settings(), make_user_feed()])
            db.execute = AsyncMock(return_value=make_execute_result(rowcount=1))
            with patch.object(dormancy_service, "is_user_dormant", new=AsyncMock(return_value=dormant)):
                article = make_article(content="word " * 500)
                assert await enqueue_summary_job(article, user_id=1, db=db) is expected

    @pytest.mark.real_dormancy
    async def test_is_user_dormant_reads_the_stored_setting(self, pg):
        await _set_app_settings(pg, dormant_after_days=60, dormant_warning_enabled=False)
        assert await dormancy_service.is_user_dormant((await _user(pg)).id, pg)
        assert not await dormancy_service.is_user_dormant((await _user(pg, active=RECENTLY)).id, pg)
