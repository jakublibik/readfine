"""Unit tests for save-by-URL: title extraction, dedup rules, finalize guards."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.readable_service import (
    _extract_title,
    apply_readable_result,
    title_from_url,
)
from app.services.saved_article_service import (
    _USABLE_CONTENT_CHARS,
    _has_usable_content,
    finalize_saved_article,
    save_article_by_url,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_article(**kwargs):
    defaults = {
        "id": 1,
        "feed_id": None,
        "url": "https://example.com/story",
        "title": "example.com/story",
        "content": None,
        "readable_content": None,
        "readable_status": "pending",
        "readable_error": None,
        "readable_retries": 0,
        "readable_next_retry_at": None,
        "readable_failed_at": None,
        "trimmed_at": None,
        "word_count": None,
        "estimated_read_min": None,
        "published_at": None,
        "summary": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_state(**kwargs):
    defaults = {
        "user_id": 1,
        "article_id": 1,
        "saved_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
        "filters_applied_at": None,
        "is_starred": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def swallow_task():
    """Stand-in for asyncio.create_task that closes the coroutine it is handed.

    Without closing it, Python warns that _import_saved_bg was never awaited — the
    background import is deliberately not exercised here.
    """
    def _swallow(coro):
        coro.close()
        return MagicMock()
    return MagicMock(side_effect=_swallow)


def make_db(scalar_results=None):
    """AsyncSession stand-in whose scalar() walks a queued list of results."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    queue = list(scalar_results or [])
    db.scalar = AsyncMock(side_effect=queue if queue else [None])
    return db


# ── _extract_title ────────────────────────────────────────────────────────────

class TestExtractTitle:
    def test_prefers_og_title(self):
        html = (
            '<html><head><meta property="og:title" content="OG Headline">'
            "<title>Tab Title</title></head><body></body></html>"
        )
        assert _extract_title(html) == "OG Headline"

    def test_falls_back_to_title_tag(self):
        assert _extract_title("<html><head><title>Just This</title></head></html>") == "Just This"

    def test_unescapes_entities(self):
        html = "<head><title>Bread &amp; Butter</title></head>"
        assert _extract_title(html) == "Bread & Butter"

    def test_collapses_whitespace(self):
        html = "<head><title>\n  Spread   out\n</title></head>"
        assert _extract_title(html) == "Spread out"

    def test_returns_none_without_a_title(self):
        assert _extract_title("<html><body><p>no head here</p></body></html>") is None

    def test_ignores_an_empty_title(self):
        assert _extract_title("<head><title>   </title></head>") is None

    def test_handles_name_attribute_form(self):
        html = '<head><meta name="og:title" content="By Name"></head>'
        assert _extract_title(html) == "By Name"


# ── title_from_url ────────────────────────────────────────────────────────────

class TestTitleFromUrl:
    def test_host_and_path(self):
        assert title_from_url("https://example.com/news/story") == "example.com/news/story"

    def test_strips_www(self):
        assert title_from_url("https://www.example.com/a") == "example.com/a"

    def test_strips_trailing_slash(self):
        assert title_from_url("https://example.com/a/") == "example.com/a"

    def test_bare_host(self):
        assert title_from_url("https://example.com") == "example.com"


# ── apply_readable_result — the title rule ────────────────────────────────────

class TestApplyReadableResultTitle:
    def test_sets_title_on_feedless_article(self):
        article = make_article(title="example.com/story")
        apply_readable_result(article, "<p>Body</p>", None, None, title="Real Headline")
        assert article.title == "Real Headline"

    def test_leaves_feed_article_title_alone(self):
        article = make_article(feed_id=7, title="From The Feed")
        apply_readable_result(article, "<p>Body</p>", None, None, title="Page Title")
        assert article.title == "From The Feed"

    def test_sets_title_even_when_extraction_failed(self):
        """A downloaded page that yielded no body still identifies itself."""
        article = make_article()
        apply_readable_result(article, None, "No content", 404, title="Real Headline")
        assert article.title == "Real Headline"
        assert article.readable_status == "failed"

    def test_does_not_depend_on_the_placeholder_value(self):
        """The guard is feed_id alone — no 'does the title still look like the URL' probe."""
        article = make_article(title="An Older Title")
        apply_readable_result(article, "<p>Body</p>", None, None, title="Fresh Title")
        assert article.title == "Fresh Title"

    def test_no_title_leaves_it_unchanged(self):
        article = make_article(title="example.com/story")
        apply_readable_result(article, "<p>Body</p>", None, None, title=None)
        assert article.title == "example.com/story"


# ── _has_usable_content ───────────────────────────────────────────────────────

class TestHasUsableContent:
    def test_readable_content_counts(self):
        assert _has_usable_content(make_article(readable_content="<p>Full text</p>")) is True

    def test_long_feed_content_counts(self):
        assert _has_usable_content(make_article(content="x" * (_USABLE_CONTENT_CHARS + 1))) is True

    def test_short_excerpt_does_not(self):
        assert _has_usable_content(make_article(content="Two sentences only.")) is False

    def test_nothing_at_all(self):
        assert _has_usable_content(make_article()) is False


# ── save_article_by_url ───────────────────────────────────────────────────────

class TestSaveArticleByUrl:
    async def test_rejects_a_url_the_validator_refuses(self):
        db = make_db()
        with patch(
            "app.utils.url_validator.async_validate_feed_url",
            AsyncMock(side_effect=ValueError("URL resolves to a private address")),
        ):
            with pytest.raises(ValueError):
                await save_article_by_url("http://127.0.0.1/x", SimpleNamespace(id=1), db)
        db.add.assert_not_called()

    async def test_attaches_to_an_existing_article(self):
        existing = make_article(id=42, feed_id=7, readable_status="success",
                                readable_content="<p>Full</p>")
        db = make_db([existing])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()):
            article, already_known = await save_article_by_url(
                "https://example.com/story", SimpleNamespace(id=1), db
            )
        assert already_known is True
        assert article is existing
        db.add.assert_not_called()
        assert existing.readable_status == "success"  # untouched

    async def test_trimmed_match_is_ignored_so_a_fresh_article_is_created(self):
        """A trimmed stub is hidden by list_articles, so attaching to it would save
        into a black hole. The lookup filters it out, and we fall through to insert."""
        db = make_db([None])  # the query excludes trimmed rows → no match
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()):
            article, already_known = await save_article_by_url(
                "https://example.com/story", SimpleNamespace(id=1), db
            )
        assert already_known is False
        db.add.assert_called_once()
        assert article.feed_id is None
        assert article.readable_status == "pending"

    async def test_new_article_gets_the_worker_buffer(self):
        db = make_db([None])
        before = datetime.now(timezone.utc)
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()):
            article, _ = await save_article_by_url(
                "https://example.com/story", SimpleNamespace(id=1), db
            )
        assert article.readable_next_retry_at > before + timedelta(minutes=1)

    async def test_feedless_match_without_full_text_is_re_extracted(self):
        existing = make_article(id=42, feed_id=None, readable_status="failed",
                                readable_retries=3, readable_error="boom")
        db = make_db([existing])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()) as task:
            await save_article_by_url("https://example.com/story", SimpleNamespace(id=1), db)
        assert existing.readable_status == "pending"
        assert existing.readable_retries == 0  # backoff attempts restored
        assert existing.readable_error is None
        assert existing.readable_next_retry_at is not None
        task.assert_called_once()

    async def test_feed_article_with_usable_content_is_left_alone(self):
        """Article rows are global: re-extracting one the user merely deduped onto
        would flip on a spinner for every subscriber and can replace their feed
        content with an error banner."""
        existing = make_article(
            id=42, feed_id=7, readable_status="skipped",
            content="x" * (_USABLE_CONTENT_CHARS + 50),
        )
        db = make_db([existing])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()) as task:
            await save_article_by_url("https://example.com/story", SimpleNamespace(id=1), db)
        assert existing.readable_status == "skipped"
        task.assert_not_called()

    async def test_feed_article_with_nothing_to_show_is_re_extracted(self):
        existing = make_article(id=42, feed_id=7, readable_status="skipped",
                                content="Tiny excerpt.")
        db = make_db([existing])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()) as task:
            await save_article_by_url("https://example.com/story", SimpleNamespace(id=1), db)
        assert existing.readable_status == "pending"
        task.assert_called_once()


# ── finalize_saved_article — the two guards ───────────────────────────────────

class TestFinalizeGuards:
    async def _run(self, article, state):
        db = make_db([state])
        filters = AsyncMock()
        summary = AsyncMock()
        with patch("app.services.filter_service.apply_filters_to_saved_article", filters), \
             patch("app.services.ai_pipeline_service.maybe_enqueue_starred_summary", summary):
            await finalize_saved_article(article, 1, db)
        return filters, summary

    async def test_no_op_while_extraction_is_still_pending(self):
        """apply_readable_result leaves a transient failure at 'pending' with a
        backoff, so 'the call returned' is not 'extraction is done'."""
        filters, summary = await self._run(make_article(readable_status="pending"), make_state())
        filters.assert_not_called()
        summary.assert_not_called()

    async def test_runs_on_success(self):
        state = make_state()
        filters, summary = await self._run(make_article(readable_status="success"), state)
        filters.assert_awaited_once()
        summary.assert_awaited_once()
        assert state.filters_applied_at is not None

    async def test_runs_on_terminal_failure(self):
        state = make_state()
        filters, _ = await self._run(make_article(readable_status="failed"), state)
        filters.assert_awaited_once()
        assert state.filters_applied_at is not None

    async def test_second_terminal_state_does_not_re_apply_filters(self):
        """The article ends 'failed', a second user's save re-extracts it, and this
        time it succeeds. The first user's star/archive actions must not fire again."""
        state = make_state(filters_applied_at=datetime(2026, 8, 4, tzinfo=timezone.utc))
        filters, summary = await self._run(make_article(readable_status="success"), state)
        filters.assert_not_called()
        summary.assert_not_called()

    async def test_no_op_when_the_user_never_saved_it(self):
        filters, _ = await self._run(
            make_article(readable_status="success"), make_state(saved_at=None)
        )
        filters.assert_not_called()

    async def test_no_op_without_a_state_row(self):
        filters, _ = await self._run(make_article(readable_status="success"), None)
        filters.assert_not_called()


# ── finalize_for_all_savers — the batch worker's entry point ──────────────────

class TestFinalizeForAllSavers:
    async def test_fans_out_over_every_saver(self):
        """Articles are global and filters are per-user, so the worker — which has no
        idea whose import task died — has to run the pass for each saver."""
        from app.services.saved_article_service import finalize_for_all_savers

        article = make_article(readable_status="success")
        db = make_db()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [1, 2]
        db.execute = AsyncMock(return_value=result)

        seen = []

        async def _fake_finalize(art, user_id, _db):
            seen.append(user_id)

        with patch(
            "app.services.saved_article_service.finalize_saved_article", _fake_finalize
        ):
            await finalize_for_all_savers(article, db)

        assert seen == [1, 2]

    async def test_no_savers_is_a_no_op(self):
        from app.services.saved_article_service import finalize_for_all_savers

        db = make_db()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        with patch(
            "app.services.saved_article_service.finalize_saved_article", AsyncMock()
        ) as fin:
            await finalize_for_all_savers(make_article(), db)
        fin.assert_not_called()


# ── apply_filters_to_saved_article — scoring stays off ────────────────────────

class TestSavedFiltersNeverScore:
    async def test_labelling_a_saved_article_does_not_enqueue_scoring(self):
        """The feed path enqueues scoring for labelled articles. The saved path must
        not: saved articles are never scored."""
        from app.services.filter_service import apply_filters_to_saved_article

        action = SimpleNamespace(action_type="label", action_value="1")
        flt = SimpleNamespace(
            id=1, conditions=[SimpleNamespace(field="title", operator="contains",
                                              value="story", position=0)],
            actions=[action], match_operator="AND", is_active=True, stop_on_match=False,
            scope_include=None, scope_except=None, user_id=1,
        )
        result = MagicMock()
        result.scalars.return_value.all.return_value = [flt]
        db = make_db()
        db.execute = AsyncMock(return_value=result)

        enqueue = AsyncMock()
        with patch("app.services.filter_service._execute_actions", AsyncMock()), \
             patch("app.services.ai_scoring_service.enqueue_scoring_job", enqueue):
            await apply_filters_to_saved_article(
                make_article(title="A story"), user_id=1, db=db
            )

        enqueue.assert_not_called()


# ── dedup ordering (integration: the rule lives in SQL) ───────────────────────

import uuid

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article as ArticleModel, UserArticleState as UASModel
from app.models.feed import Feed, UserFeed
from app.models.user import User


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


class TestDedupPicksDeterministically:
    """The same article routinely exists in several feeds, so the lookup can match
    more than one row. Without an explicit order the choice was whatever the planner
    returned first."""

    async def _fixtures(self, pg):
        u = uuid.uuid4().hex[:12]
        user = User(email=f"dd_{u}@test.invalid", password_hash="x", display_name="t")
        pg.add(user)
        await pg.flush()
        url = f"https://ex.invalid/{u}/story"
        return user, url

    async def _feed(self, pg, user=None):
        u = uuid.uuid4().hex
        f = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"f-{u[:6]}",
                 subscriber_count=1 if user else 0)
        pg.add(f)
        await pg.flush()
        if user:
            pg.add(UserFeed(user_id=user.id, feed_id=f.id))
            await pg.flush()
        return f

    async def _copy(self, pg, feed, url, *, status="success"):
        u = uuid.uuid4().hex
        a = ArticleModel(feed_id=feed.id, guid=u, guid_hash=u, title="T",
                         url=url, url_normalized=url, content="<p>b</p>",
                         readable_status=status,
                         published_at=datetime.now(timezone.utc),
                         fetched_at=datetime.now(timezone.utc))
        pg.add(a)
        await pg.flush()
        return a

    async def _save(self, pg, user, url):
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()), \
             patch.object(pg, "commit", AsyncMock()):
            article, known = await save_article_by_url(url, user, pg)
        return article, known

    async def test_prefers_a_copy_the_user_subscribes_to(self, pg):
        user, url = await self._fixtures(pg)
        foreign = await self._copy(pg, await self._feed(pg, None), url)
        mine = await self._copy(pg, await self._feed(pg, user), url)

        article, known = await self._save(pg, user, url)
        assert known is True
        assert article.id == mine.id, "attached to a feed the user does not follow"
        assert article.id != foreign.id

    async def test_prefers_an_already_extracted_copy(self, pg):
        user, url = await self._fixtures(pg)
        await self._copy(pg, await self._feed(pg, user), url, status="failed")
        good = await self._copy(pg, await self._feed(pg, user), url, status="success")

        article, _ = await self._save(pg, user, url)
        assert article.id == good.id

    async def test_trimmed_copy_is_skipped_even_when_subscribed(self, pg):
        """The stub is hidden by list_articles, so attaching to it would save into a
        black hole — it must lose to any usable row."""
        user, url = await self._fixtures(pg)
        stub = await self._copy(pg, await self._feed(pg, user), url)
        stub.trimmed_at = datetime.now(timezone.utc)
        usable = await self._copy(pg, await self._feed(pg, user), url)
        await pg.flush()

        article, _ = await self._save(pg, user, url)
        assert article.id == usable.id
