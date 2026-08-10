"""Unit tests for save-by-URL: title extraction, dedup rules, finalize guards."""
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from app.services.readable_service import (
    _extract_title,
    apply_readable_result,
    title_from_url,
)
from app.services.saved_article_service import (
    _USABLE_CONTENT_CHARS,
    adopt_resolved_url,
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

    def test_unescapes_double_encoded_entities(self):
        # Vimeo escapes its markup twice, so one decode leaves a visible &#x27; in
        # every saved title.
        html = "<head><title>Here&amp;#x27;s how to add music | Vimeo</title></head>"
        assert _extract_title(html) == "Here's how to add music | Vimeo"

    def test_stops_after_a_second_decode(self):
        html = "<head><title>&amp;amp;amp;lt;b&amp;amp;amp;gt;</title></head>"
        assert _extract_title(html) == "&amp;lt;b&amp;gt;"

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
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("app.services.saved_article_service.finalize_saved_article", AsyncMock()):
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
             patch("app.services.saved_article_service.finalize_saved_article", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()) as task:
            await save_article_by_url("https://example.com/story", SimpleNamespace(id=1), db)
        assert existing.readable_status == "skipped"
        task.assert_not_called()

    async def test_a_dedup_needing_no_extraction_still_gets_the_filter_pass(self):
        """Nothing else will call it: no extraction runs, so neither the import task
        nor the batch worker ever comes back for this article. Without this the pass
        would depend on who saved first, and everyone deduping onto the finished
        article afterwards would silently go without their filters."""
        existing = make_article(id=42, feed_id=7, readable_status="success",
                                readable_content="<p>Full</p>")
        db = make_db([existing])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("app.services.saved_article_service.finalize_saved_article",
                   AsyncMock()) as finalize:
            await save_article_by_url("https://example.com/story", SimpleNamespace(id=1), db)
        finalize.assert_awaited_once()
        assert finalize.await_args.args[:2] == (existing, 1)

    async def test_feed_article_with_nothing_to_show_is_re_extracted(self):
        existing = make_article(id=42, feed_id=7, readable_status="skipped",
                                content="Tiny excerpt.")
        db = make_db([existing])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()) as task:
            await save_article_by_url("https://example.com/story", SimpleNamespace(id=1), db)
        assert existing.readable_status == "pending"
        task.assert_called_once()


# ── save_article_by_url — credentials in the pasted address ───────────────────

class TestSaveUrlCredentials:
    """https://user:pass@host/article must not leave the password in the database.

    An Article row is global — shared with everyone else who saves the same URL — so
    the credentials are used for this one extraction and then dropped.
    """
    URL = "https://reader:s3cret@example.com/story"
    CLEAN = "https://example.com/story"

    async def _save(self, existing=None):
        db = make_db([existing])
        importer = MagicMock(return_value=MagicMock())
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("app.services.saved_article_service._import_saved_bg", importer), \
             patch("asyncio.create_task", MagicMock()):
            article, known = await save_article_by_url(
                self.URL, SimpleNamespace(id=1), db
            )
        return article, known, importer

    async def test_no_column_keeps_the_credentials(self):
        article, _, _ = await self._save()
        assert article.url == self.CLEAN
        assert article.guid == self.CLEAN
        assert article.url_normalized == self.CLEAN
        assert article.guid_hash == hashlib.sha256(self.CLEAN.encode()).hexdigest()
        assert "s3cret" not in article.title
        assert article.title == "example.com/story"

    async def test_extraction_still_gets_them(self):
        """Pasting an authenticated address has to keep working: the credentials
        travel as an explicit auth pair, in memory, for the length of the import."""
        _, _, importer = await self._save()
        assert importer.call_args.args[2:] == (self.CLEAN, "reader", "s3cret")

    async def test_a_copy_on_another_host_is_not_sent_them(self):
        """The match is made on the normalized URL, so the row found can be a copy
        stored under a different address. Credentials given for one host are not
        handed to another."""
        existing = make_article(id=42, feed_id=None, url="https://mirror.example.net/story",
                                readable_status="failed")
        _, known, importer = await self._save(existing)
        assert known is True
        assert importer.call_args.args[2:] == ("https://mirror.example.net/story", None, None)

    async def test_the_same_address_is_sent_them(self):
        existing = make_article(id=42, feed_id=None, url=self.CLEAN, readable_status="failed")
        _, _, importer = await self._save(existing)
        assert importer.call_args.args[2:] == (self.CLEAN, "reader", "s3cret")


class TestAdoptResolvedUrl:
    def test_strips_credentials_off_a_canonical_link(self):
        """resolve_article_url can return a canonical link read off the page, which
        is the host's own text and may carry userinfo."""
        article = make_article(url="https://example.com/tracker")
        adopt_resolved_url(article, "https://u:p@example.com/real")
        assert article.url == "https://example.com/real"
        assert article.url_normalized == "https://example.com/real"

    def test_a_credentialed_form_of_the_stored_url_is_not_a_rewrite(self):
        article = make_article(url="https://example.com/story")
        adopt_resolved_url(article, "https://u:p@example.com/story")
        assert article.url == "https://example.com/story"

    def test_feed_articles_are_left_alone(self):
        article = make_article(feed_id=7, url="https://example.com/story")
        adopt_resolved_url(article, "https://example.com/elsewhere")
        assert article.url == "https://example.com/story"


# ── finalize_saved_article — the two guards ───────────────────────────────────

class TestFinalizeGuards:
    async def _run(self, article, state, subscription=None):
        # The subscription lookup is the second scalar() and only happens for an
        # article that has a feed, so feedless cases queue nothing for it.
        db = make_db([state] if article.feed_id is None else [state, subscription])
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

    async def test_a_subscriber_does_not_get_the_same_filters_twice(self):
        """The feed fetch already ran them on every subscriber and stamped nothing, so
        filters_applied_at cannot see that pass. Saving such an article (a pasted URL
        that deduped onto it) would apply star/archive/mark-read a second time, after
        the reader had undone them."""
        state = make_state()
        filters, summary = await self._run(
            make_article(feed_id=7, readable_status="success"), state, subscription=99,
        )
        filters.assert_not_called()
        summary.assert_not_called()
        # Not stamped either: nothing ran, and the column would say it did.
        assert state.filters_applied_at is None

    async def test_a_non_subscriber_does(self):
        """The same article reached by dedup from someone else's feed. That fetch ran
        the subscribers' filters, never this user's."""
        state = make_state()
        filters, _ = await self._run(
            make_article(feed_id=7, readable_status="success"), state, subscription=None,
        )
        filters.assert_awaited_once()
        assert state.filters_applied_at is not None

    async def test_skipped_is_terminal_too(self):
        """A full-content feed leaves its articles 'skipped', which is as final as it
        gets: extraction will not run at all, so nothing is pending on it."""
        filters, _ = await self._run(
            make_article(feed_id=7, readable_status="skipped"), make_state(),
            subscription=None,
        )
        filters.assert_awaited_once()


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
from sqlalchemy import select
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


# ── what the list row says while a save is being fetched ─────────────────────

class TestRowStatusMarker:
    """A saved-by-URL row is inserted under the pasted address and only picks up the
    real title when extraction finishes, so it has to say more is coming — and, when
    the page hands over nothing at all, that the address it is showing is all there
    will ever be.

    Rendered rather than asserted on the flag alone: the logic that matters is the
    scoping (feedless articles only), and that lives in the template."""

    def _row(self, **kwargs):
        from app.templating import templates
        defaults = {
            "id": 1, "feed_id": None, "feed_title": None,
            "url": "https://example.com/story", "title": "example.com/story",
            "snippet": None, "published_at": None, "formatted_date": "1 Jan",
            "is_read": False, "is_starred": False, "is_archived": False,
            "body_permanently_empty": False, "readable_active": False,
            "nothing_to_show": False, "ai_score": None, "labels": [],
        }
        defaults.update(kwargs)
        return templates.env.get_template("app/partials/article_row.html").render(
            article=SimpleNamespace(**defaults), request=None,
        )

    def test_extraction_in_flight_shows_a_spinner(self):
        assert "animate-spin" in self._row(readable_active=True)

    def test_a_row_left_with_only_its_address_shows_the_error_bar(self):
        html = self._row(nothing_to_show=True)
        assert "The full text could not be retrieved" in html
        assert "animate-spin" not in html

    def test_a_failed_fetch_that_still_yielded_a_title_is_left_clean(self):
        """A site answering with a consent page still hands over its title and
        description, so that row reads like any other and a bar on it is noise."""
        html = self._row(title="The Real Headline", snippet="The site's own blurb.",
                         nothing_to_show=False)
        assert "could not be retrieved" not in html

    def test_a_finished_row_is_left_clean(self):
        html = self._row(title="The Real Headline")
        assert "animate-spin" not in html
        assert "could not be retrieved" not in html

    def test_a_feed_article_is_never_marked(self):
        """A feed article that fails extraction still shows its feed content, so a
        marker there would flag a row with nothing wrong with it."""
        html = self._row(feed_id=5, feed_title="A feed", nothing_to_show=True)
        assert "could not be retrieved" not in html
        assert "animate-spin" not in self._row(feed_id=5, feed_title="A feed",
                                               readable_active=True)

    def test_both_densities_carry_the_marker(self):
        for density in ("compact", "comfortable", "summary"):
            from app.templating import templates
            html = templates.env.get_template("app/partials/article_row.html").render(
                article=SimpleNamespace(
                    id=1, feed_id=None, feed_title=None, url="https://example.com/s",
                    title="example.com/s", snippet=None, published_at=None,
                    formatted_date="1 Jan", is_read=False, is_starred=False,
                    is_archived=False, body_permanently_empty=False,
                    readable_active=True, nothing_to_show=False, ai_score=None,
                    labels=[],
                ),
                density=density, request=None,
            )
            assert "animate-spin" in html, density


# ── what the article says between attempts ───────────────────────────────────

class TestAwaitingRetryNotice:
    """An extraction that fails on something transient leaves the article at
    'pending' with a backoff of up to two hours running. That is not a verdict, and
    the reader used to present it as one (or, with nothing to show, say nothing at
    all and offer no way to act)."""

    def _content(self, **kwargs):
        from app.templating import templates
        defaults = {
            "id": 1, "feed_id": None, "url": "https://example.com/story",
            "readable_status": "pending", "readable_error": "Timeout after 15s",
            "readable_active": False, "readable_content": None, "content": None,
            "summary": None,
        }
        defaults.update(kwargs)
        return templates.env.get_template("app/partials/article_content.html").render(
            article=SimpleNamespace(**defaults), request=None,
        )

    def test_waiting_says_another_attempt_is_coming(self):
        html = self._content()
        assert "Another attempt is scheduled" in html

    def test_waiting_offers_a_manual_retry(self):
        """The only action in this branch: the alternative is waiting out the backoff
        for an attempt the reader could make now."""
        assert "extract-readable" in self._content()

    def test_waiting_does_not_pass_itself_off_as_final(self):
        html = self._content(summary="The site's own blurb about the article.")
        assert "Another attempt is scheduled" in html
        assert "could not be retrieved" not in html

    def test_a_final_failure_still_gives_the_reason(self):
        html = self._content(readable_status="failed",
                             readable_error="HTTP 403 Forbidden")
        assert "Another attempt is scheduled" not in html
        assert "403" in html

    def test_a_first_attempt_in_flight_is_not_a_retry(self):
        """'pending' with the first attempt still running is the spinner's state, not
        this one."""
        assert "Another attempt is scheduled" not in self._content(readable_active=True)


# ── is_saved through the state-update path (API) ──────────────────────────────

class TestIsSavedThroughStateUpdate:
    """PATCH /api/v1/articles/{id} carries is_saved, the counterpart of save-url.

    Worth an integration test rather than a mocked one: the payload's boolean has to
    land in saved_at, which is a timestamp, and clearing it is what makes a feedless
    article purgeable and inaccessible again.
    """

    async def _article(self, pg, *, saved: bool):
        u = uuid.uuid4().hex
        user = User(email=f"sv_{u[:12]}@test.invalid", password_hash="x", display_name="t")
        pg.add(user)
        await pg.flush()
        article = ArticleModel(
            feed_id=None, guid=u, guid_hash=u, title="ex.invalid/story",
            url=f"https://ex.invalid/{u}", url_normalized=f"https://ex.invalid/{u}",
            readable_status="success", readable_content="<p>b</p>",
            fetched_at=datetime.now(timezone.utc),
        )
        pg.add(article)
        await pg.flush()
        pg.add(UASModel(
            user_id=user.id, article_id=article.id,
            saved_at=datetime.now(timezone.utc) if saved else None,
        ))
        await pg.flush()
        return user, article

    async def _patch(self, pg, user, article, **fields):
        from app.schemas.article import ArticleStateUpdate
        from app.services.article import update_article_state

        # commit() is left real: update_article_state refreshes the state row right
        # after it, and a stubbed commit would have that refresh read the unwritten
        # row back from the database and undo what the test just set. The fixture's
        # outer transaction is rolled back either way.
        return await update_article_state(
            user, article.id, ArticleStateUpdate(**fields), pg
        )

    async def _state(self, pg, user, article):
        return await pg.scalar(
            select(UASModel).where(
                UASModel.user_id == user.id, UASModel.article_id == article.id
            )
        )

    async def test_unsaving_clears_saved_at(self, pg):
        user, article = await self._article(pg, saved=True)
        response = await self._patch(pg, user, article, is_saved=False)
        assert response.is_saved is False
        assert (await self._state(pg, user, article)).saved_at is None

    async def test_saving_stamps_saved_at(self, pg):
        """A starred article the user pins to Saved: reachable already, so this is
        the flag on its own, with no fetch behind it."""
        user, article = await self._article(pg, saved=False)
        state = await self._state(pg, user, article)
        state.is_starred = True  # keeps the article reachable while saved_at is NULL
        await pg.flush()

        response = await self._patch(pg, user, article, is_saved=True)
        assert response.is_saved is True
        assert (await self._state(pg, user, article)).saved_at is not None

    async def test_a_payload_without_is_saved_leaves_it_alone(self, pg):
        user, article = await self._article(pg, saved=True)
        await self._patch(pg, user, article, is_read=True)
        assert (await self._state(pg, user, article)).saved_at is not None


class TestSavedArticleSurvivesFeedDeletion:
    """A saved article can belong to a feed: pasting a URL that is already in the
    database attaches to that row instead of duplicating it. The reader who saved it
    need not subscribe to that feed, so unsubscribing the last subscriber must not
    take the article with it — the same protection starring has always given."""

    async def _feed_with_article(self, pg, subscriber):
        u = uuid.uuid4().hex
        feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="f", subscriber_count=1)
        pg.add(feed)
        await pg.flush()
        uf = UserFeed(user_id=subscriber.id, feed_id=feed.id)
        pg.add(uf)
        article = ArticleModel(
            feed_id=feed.id, guid=u, guid_hash=u, title="T",
            url=f"https://ex.invalid/{u}/story", url_normalized=f"https://ex.invalid/{u}/story",
            content="<p>b</p>", readable_status="success",
            published_at=datetime.now(timezone.utc), fetched_at=datetime.now(timezone.utc),
        )
        pg.add(article)
        await pg.flush()
        return feed, uf, article

    async def _user(self, pg, tag):
        user = User(email=f"{tag}_{uuid.uuid4().hex[:12]}@test.invalid",
                    password_hash="x", display_name="t")
        pg.add(user)
        await pg.flush()
        return user

    async def _unsubscribe(self, pg, user, uf):
        from app.services.feed import unsubscribe
        with patch.object(pg, "commit", AsyncMock()):
            await unsubscribe(user, uf.id, pg)
        await pg.flush()

    async def test_article_saved_by_another_user_survives(self, pg):
        subscriber = await self._user(pg, "sub")
        saver = await self._user(pg, "saver")
        _feed, uf, article = await self._feed_with_article(pg, subscriber)
        pg.add(UASModel(user_id=saver.id, article_id=article.id,
                        saved_at=datetime.now(timezone.utc)))
        await pg.flush()
        article_id = article.id

        await self._unsubscribe(pg, subscriber, uf)

        survivor = await pg.scalar(
            select(ArticleModel).where(ArticleModel.id == article_id)
        )
        assert survivor is not None, "a saved article was deleted with its feed"
        assert survivor.feed_id is None, "survivor must be detached from the deleted feed"

    async def test_unsubscriber_keeps_their_own_saved_state(self, pg):
        """Saving is per-user state on the article, so the row that carries it must
        outlive the subscription the article arrived through."""
        user = await self._user(pg, "both")
        _feed, uf, article = await self._feed_with_article(pg, user)
        pg.add(UASModel(user_id=user.id, article_id=article.id,
                        saved_at=datetime.now(timezone.utc)))
        await pg.flush()
        article_id = article.id

        await self._unsubscribe(pg, user, uf)

        state = await pg.scalar(
            select(UASModel).where(
                UASModel.user_id == user.id, UASModel.article_id == article_id
            )
        )
        assert state is not None and state.saved_at is not None

    async def test_unsaved_article_is_still_deleted(self, pg):
        """The protection is saved_at, not "has any state row" — an ordinary read
        article still goes when its last subscriber leaves."""
        user = await self._user(pg, "plain")
        _feed, uf, article = await self._feed_with_article(pg, user)
        pg.add(UASModel(user_id=user.id, article_id=article.id, is_read=True))
        await pg.flush()
        article_id = article.id

        await self._unsubscribe(pg, user, uf)

        assert await pg.scalar(
            select(ArticleModel).where(ArticleModel.id == article_id)
        ) is None


# ── the Retry button — the third door into a terminal state ───────────────────

class TestRetryFinalizesSavedArticle:
    """A saved article's post-extraction pass has three entry points: the import
    task, the batch worker, and the Retry button in the article panel.

    Retry is reachable exactly when the other two cannot come back: a transient
    failure leaves the article 'pending' with a backoff, which is not terminal, so
    the import task correctly finalizes nothing — and once Retry succeeds the status
    is 'success', which process_pending_readable never selects. If Retry did not
    finalize, that article's filters would never run at all.
    """

    def _request(self):
        return Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "path": "/htmx/articles/1/extract-readable", "query_string": b"",
            "headers": [], "scheme": "http", "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234), "root_path": "", "app": None, "state": {},
        })

    def _db(self, article):
        db = MagicMock()
        row = MagicMock()
        row.first.return_value = (article, None, None)
        db.execute = AsyncMock(return_value=row)
        db.commit = AsyncMock()
        return db

    async def _retry(self, article):
        """Drive the route past a successful extraction, reporting what it stored with."""
        from app.rate_limit import limiter
        from app.routers.web.app.articles import htmx_extract_readable
        from app.services.readable_service import ReadableResult

        result = ReadableResult(content="<p>Body</p>", title="Real Headline")
        store = AsyncMock()
        was_enabled = limiter.enabled
        limiter.enabled = False
        try:
            with (
                patch("app.services.readable_service.extract_readable_with_title",
                      return_value=result),
                patch("app.services.readable_service.store_saved_extraction", store),
                patch("app.routers.web.app.articles.get_article",
                      new=AsyncMock(return_value=None)),
            ):
                await htmx_extract_readable(
                    article.id, self._request(),
                    user=SimpleNamespace(id=1), db=self._db(article),
                )
        finally:
            limiter.enabled = was_enabled
        return store

    async def test_a_saved_article_goes_through_the_shared_helper(self):
        store = await self._retry(make_article(feed_id=None, readable_status="pending"))
        store.assert_awaited_once()

    async def test_a_feed_article_does_not(self):
        """Feed articles have no per-saver pass here; the batch worker runs the AI
        pipeline for them instead, and finalizing only on this path would make the
        two disagree."""
        store = await self._retry(make_article(feed_id=5, readable_status="pending"))
        store.assert_not_called()


# ── addresses too long for the columns that hold them ─────────────────────────

class TestOverlongUrls:
    """An address past 2048 characters used to be stored cut down while the fetch
    still used the whole thing, so the article extracted fine and looked normal with
    a stored link that goes nowhere. That link is what "Open original" offers and what
    Retry re-fetches, so the row was quietly broken in the two places that matter.
    """

    def _long(self, extra: int = 100) -> str:
        base = "https://example.com/"
        return base + "a" * (2048 - len(base) + extra)

    async def test_a_url_that_will_not_fit_is_refused(self):
        db = make_db()
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()):
            with pytest.raises(ValueError, match="too long"):
                await save_article_by_url(self._long(), SimpleNamespace(id=1), db)
        db.add.assert_not_called()

    async def test_one_that_just_fits_is_saved_whole(self):
        """The stored URL, the guid and the hash must all describe one address."""
        url = self._long(extra=0)
        db = make_db([None])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()):
            article, _ = await save_article_by_url(url, SimpleNamespace(id=1), db)
        assert article.url == url and article.guid == url
        assert article.guid_hash == hashlib.sha256(url.encode()).hexdigest()

    async def test_the_length_is_measured_after_credentials_come_off(self):
        """They are split off before anything is stored, so they are not what makes
        an address too long to store."""
        url = self._long(extra=0)
        with_creds = url.replace("https://", "https://user:pw@", 1)
        db = make_db([None])
        with patch("app.utils.url_validator.async_validate_feed_url", AsyncMock()), \
             patch("asyncio.create_task", swallow_task()):
            article, _ = await save_article_by_url(with_creds, SimpleNamespace(id=1), db)
        assert article.url == url

    def test_an_overlong_resolved_address_is_not_adopted(self):
        """It is the host's own text (a canonical link), and the address already saved
        works, so a cut-down replacement would be a straight downgrade."""
        article = make_article(feed_id=None, url="https://example.com/story")
        adopt_resolved_url(article, self._long())
        assert article.url == "https://example.com/story"
