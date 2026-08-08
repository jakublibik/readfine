"""Credentials written into a feed's address must end up in the encrypted columns.

Covers the surgery itself (split_url_credentials), the rule that reads the columns
back (feed_auth), the two subscribe paths, and the pure function the 0090 backfill is
built on. The behaviour these guard is easy to break quietly: a feed that stops
authenticating just starts answering 401 somewhere in a scheduled fetch.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.crypto import encrypt, feed_auth
from app.utils.feed_credentials import plan_article_url_rewrite, plan_feed_credential_split
from app.utils.parsing import normalize_url
from app.utils.url_validator import split_url_credentials


class TestSplitUrlCredentials:
    def test_user_and_password_come_out_of_the_address(self):
        assert split_url_credentials("https://bob:hunter2@example.com/feed") == (
            "https://example.com/feed", "bob", "hunter2"
        )

    def test_address_without_credentials_is_returned_unchanged(self):
        url = "https://example.com/feed?api_key=secret"
        clean, user, password = split_url_credentials(url)
        assert clean is url and user is None and password is None

    def test_at_sign_outside_the_host_is_not_a_credential(self):
        url = "https://example.com/feed?email=a@b.com"
        assert split_url_credentials(url) == (url, None, None)

    def test_percent_encoding_is_decoded(self):
        # httpx decodes userinfo before building the Authorization header, so the
        # decoded form is what the host has been seeing all along.
        assert split_url_credentials("https://bob%40mail:p%3Aw@example.com/f") == (
            "https://example.com/f", "bob@mail", "p:w"
        )

    def test_username_without_password_pairs_with_an_empty_one(self):
        assert split_url_credentials("https://bob@example.com/f") == (
            "https://example.com/f", "bob", ""
        )

    def test_password_without_username_pairs_with_an_empty_one(self):
        assert split_url_credentials("https://:hunter2@example.com/f") == (
            "https://example.com/f", "", "hunter2"
        )

    def test_bare_at_sign_is_not_a_credential(self):
        assert split_url_credentials("https://@example.com/f") == (
            "https://example.com/f", None, None
        )

    def test_port_survives(self):
        assert split_url_credentials("https://bob:pw@example.com:8443/f")[0] == (
            "https://example.com:8443/f"
        )

    def test_ipv6_host_survives(self):
        assert split_url_credentials("https://bob:pw@[2001:db8::1]:8443/f")[0] == (
            "https://[2001:db8::1]:8443/f"
        )

    def test_query_and_fragment_survive(self):
        assert split_url_credentials("https://bob:pw@example.com/f?a=1#top")[0] == (
            "https://example.com/f?a=1#top"
        )


class TestFeedAuth:
    """Both columns present means credentials, and present means non-NULL.

    The distinction is what carries an address of the form https://user@host/feed,
    whose empty password httpx has been sending all along.
    """

    def test_both_columns_yield_the_pair(self):
        assert feed_auth("bob", encrypt("hunter2")) == ("bob", "hunter2")

    def test_empty_password_still_counts_as_credentials(self):
        assert feed_auth("bob", encrypt("")) == ("bob", "")

    def test_empty_username_still_counts_as_credentials(self):
        assert feed_auth("", encrypt("hunter2")) == ("", "hunter2")

    def test_missing_password_column_yields_nothing(self):
        assert feed_auth("bob", None) is None

    def test_missing_username_column_yields_nothing(self):
        assert feed_auth(None, encrypt("hunter2")) is None

    def test_undecryptable_password_yields_nothing_rather_than_raising(self, caplog):
        # A rotated or corrupted ENCRYPTION_KEY must not take down the fetch around it.
        assert feed_auth("bob", "not-a-fernet-token", context="feed 7") is None
        assert "feed 7" in caplog.text


class TestNormalizeUrlDropsCredentials:
    def test_credentials_do_not_reach_the_dedup_key(self):
        assert normalize_url("https://bob:pw@example.com/a") == "https://example.com/a"

    def test_cleaned_and_credentialed_addresses_normalize_alike(self):
        assert normalize_url("https://bob:pw@example.com/a") == normalize_url(
            "https://example.com/a"
        )


class TestPlanFeedCredentialSplit:
    """The pure function the 0090 backfill loops over."""

    def _plan(self, feed_url, title=None, site_url=None, fetch_auth_user=None):
        return plan_feed_credential_split(
            feed_url=feed_url,
            title=title if title is not None else "Example",
            site_url=site_url,
            fetch_auth_user=fetch_auth_user,
        )

    def test_address_without_credentials_needs_no_work(self):
        assert self._plan("https://example.com/feed") is None

    def test_credentials_move_into_the_columns(self):
        plan = self._plan("https://bob:hunter2@example.com/feed")
        assert plan.feed_url == "https://example.com/feed"
        assert (plan.fetch_auth_user, plan.fetch_auth_pass) == ("bob", "hunter2")

    def test_existing_form_credentials_win_and_the_address_is_still_cleaned(self):
        # The auth columns are the ones the edit form maintains; the address was only
        # ever a copy, so it goes without overwriting them.
        plan = self._plan("https://bob:old@example.com/feed", fetch_auth_user="alice")
        assert plan.feed_url == "https://example.com/feed"
        assert plan.fetch_auth_user is None and plan.fetch_auth_pass is None

    def test_a_title_that_is_the_address_is_cleaned_too(self):
        url = "https://bob:hunter2@example.com/feed"
        assert self._plan(url, title=url).title == "https://example.com/feed"

    def test_a_truncated_title_is_recognised_as_the_address(self):
        # subscribe stores title[:255], so a feed with a long address never has a
        # title equal to the whole thing. Comparing against the full URL would leave
        # the password sitting in the feed's display name.
        url = "https://bob:hunter2@example.com/feed?p=" + "x" * 300
        plan = self._plan(url, title=url[:255])
        assert "hunter2" not in plan.title
        assert plan.title == plan.feed_url[:255]

    def test_a_real_title_is_left_alone(self):
        plan = self._plan("https://bob:pw@example.com/feed", title="Bob's blog")
        assert plan.title == "Bob's blog"

    def test_site_url_is_cleaned(self):
        plan = self._plan(
            "https://bob:pw@example.com/feed", site_url="https://bob:pw@example.com/"
        )
        assert plan.site_url == "https://example.com/"

    def test_absent_site_url_stays_absent(self):
        assert self._plan("https://bob:pw@example.com/feed").site_url is None

    def test_username_too_long_for_the_column_is_refused(self):
        # Truncating would store a username that authenticates as nobody. The feed
        # keeps its address instead.
        assert self._plan(f"https://{'b' * 256}:pw@example.com/feed") is None


class TestPlanArticleUrlRewrite:
    def test_address_without_credentials_needs_no_work(self):
        assert plan_article_url_rewrite(url="https://example.com/a", guid=None) is None

    def test_dedup_key_moves_with_the_address(self):
        # A scrape article's guid is its address. Cleaning one without the other
        # would make the next scrape re-import the whole feed.
        import hashlib

        url = "https://bob:pw@example.com/a"
        rewrite = plan_article_url_rewrite(url=url, guid=url)
        assert rewrite.url == "https://example.com/a"
        assert rewrite.guid == "https://example.com/a"
        assert rewrite.guid_hash == hashlib.sha256(b"https://example.com/a").hexdigest()
        assert rewrite.url_normalized == "https://example.com/a"

    def test_a_guid_of_its_own_is_left_alone(self):
        # An RSS feed supplies its own guid, which has nothing to do with the address.
        rewrite = plan_article_url_rewrite(
            url="https://bob:pw@example.com/a", guid="tag:example.com,2026:1"
        )
        assert rewrite.url == "https://example.com/a"
        assert rewrite.guid is None and rewrite.guid_hash is None


def _subscribe_db(existing_feed=None, private_dupe=False):
    """A db double for subscribe(): no folder, no limit, no matching feed row."""
    db = AsyncMock()
    added = []

    async def _execute(stmt, *a, **kw):
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing_feed
        result.scalar.return_value = 0
        result.__iter__ = lambda self: iter([])
        return result

    db.execute = AsyncMock(side_effect=_execute)
    db.scalar = AsyncMock(return_value=1 if private_dupe else None)
    db.add = added.append
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.added = added
    return db


@pytest.fixture
def _no_network():
    """Neutralise everything subscribe() does besides building the row."""
    parsed = SimpleNamespace(feed={"title": "Example", "link": "https://example.com"}, entries=[])
    with (
        patch("app.services.feed.async_validate_feed_url", new=AsyncMock()),
        patch("app.services.feed.fetch_and_parse_url",
              new=AsyncMock(return_value=(parsed, None))) as fetch,
        patch("app.services.feed.is_full_content_feed", return_value=False),
        patch("app.services.feed.asyncio.create_task"),
    ):
        yield fetch


class TestSubscribeSplitsCredentials:
    async def _subscribe(self, url, db=None, **kwargs):
        from app.services.feed import subscribe
        from tests.conftest import make_mock_user

        db = db or _subscribe_db()
        kwargs.setdefault("fetch_auth_user", None)
        kwargs.setdefault("fetch_auth_pass", None)
        await subscribe(
            user=make_mock_user(role="admin"), url=url, folder_id=None,
            custom_title=None, db=db, trigger_initial_fetch=False, **kwargs,
        )
        from app.models.feed import Feed
        return next(o for o in db.added if isinstance(o, Feed))

    async def test_address_is_stored_without_the_credentials(self, _no_network):
        feed = await self._subscribe("https://bob:hunter2@example.com/feed")
        assert feed.feed_url == "https://example.com/feed"

    async def test_credentials_are_stored_encrypted(self, _no_network):
        feed = await self._subscribe("https://bob:hunter2@example.com/feed")
        assert feed.fetch_auth_user == "bob"
        assert feed.fetch_auth_pass_encrypted != "hunter2"
        assert feed_auth(feed.fetch_auth_user, feed.fetch_auth_pass_encrypted) == (
            "bob", "hunter2"
        )

    async def test_the_row_is_private(self, _no_network):
        # Otherwise the feed joins the shared pool and everyone else on the instance
        # fetches it with this subscriber's password.
        feed = await self._subscribe("https://bob:hunter2@example.com/feed")
        assert feed.is_private is True

    async def test_the_feed_is_fetched_with_the_credentials(self, _no_network):
        await self._subscribe("https://bob:hunter2@example.com/feed")
        assert _no_network.await_args.kwargs["auth"] == ("bob", "hunter2")

    async def test_a_feed_with_no_title_is_not_named_after_its_password(self, _no_network):
        with patch("app.services.feed.fetch_and_parse_url",
                   new=AsyncMock(return_value=(SimpleNamespace(feed={}, entries=[]), None))):
            feed = await self._subscribe("https://bob:hunter2@example.com/feed")
        assert "hunter2" not in feed.title

    async def test_empty_password_survives_the_move(self, _no_network):
        # https://user@host/feed sends Basic auth with an empty password; the pair has
        # to read back the same way or the feed starts answering 401.
        feed = await self._subscribe("https://bob@example.com/feed")
        assert feed_auth(feed.fetch_auth_user, feed.fetch_auth_pass_encrypted) == ("bob", "")

    async def test_form_credentials_win_over_the_address(self, _no_network):
        feed = await self._subscribe(
            "https://bob:hunter2@example.com/feed",
            fetch_auth_user="alice", fetch_auth_pass="s3cret",
        )
        assert feed.feed_url == "https://example.com/feed"
        assert feed_auth(feed.fetch_auth_user, feed.fetch_auth_pass_encrypted) == (
            "alice", "s3cret"
        )

    async def test_a_plain_feed_is_unaffected(self, _no_network):
        feed = await self._subscribe("https://example.com/feed")
        assert feed.feed_url == "https://example.com/feed"
        assert feed.is_private is False
        assert feed.fetch_auth_user is None and feed.fetch_auth_pass_encrypted is None

    async def test_an_overlong_username_is_refused(self, _no_network):
        from app.services.feed import subscribe
        from tests.conftest import make_mock_user

        with pytest.raises(ValueError, match="too long"):
            await subscribe(
                user=make_mock_user(role="admin"),
                url=f"https://{'b' * 256}:pw@example.com/feed",
                folder_id=None, custom_title=None,
                fetch_auth_user=None, fetch_auth_pass=None, db=_subscribe_db(),
            )

    async def test_subscribing_twice_is_refused(self, _no_network):
        # The row is private, so the shared-row lookup never sees it. Without the
        # private lookup the same address added twice becomes two feeds.
        from app.services.feed import AlreadySubscribed

        with pytest.raises(AlreadySubscribed):
            await self._subscribe(
                "https://bob:hunter2@example.com/feed", db=_subscribe_db(private_dupe=True)
            )

    async def test_the_duplicate_lookup_is_skipped_without_credentials(self, _no_network):
        db = _subscribe_db(private_dupe=True)
        await self._subscribe("https://example.com/feed", db=db)
        assert not db.scalar.called


class TestSubscribeScrapeSplitsCredentials:
    async def _subscribe(self, url, db=None):
        from app.services.feed import subscribe_scrape
        from app.models.feed import Feed
        from tests.conftest import make_mock_user

        db = db or _subscribe_db()
        with (
            patch("app.services.feed.async_validate_feed_url", new=AsyncMock()),
            patch("app.fetcher.scrape.fetch_page_html",
                  new=AsyncMock(return_value="<html/>")) as fetch,
            patch("app.fetcher.scrape.extract_article_links",
                  return_value=[("https://example.com/a", "A", None, None)]),
            patch("app.services.feed.asyncio.create_task"),
            patch("app.services.feed._initial_fetch_scrape", new=MagicMock()),
        ):
            await subscribe_scrape(
                user=make_mock_user(role="admin"), url=url, selector="article a",
                title="Example", folder_id=None, db=db,
            )
        return next(o for o in db.added if isinstance(o, Feed)), fetch

    async def test_address_and_site_url_are_stored_without_the_credentials(self):
        feed, _ = await self._subscribe("https://bob:hunter2@example.com/news")
        assert feed.feed_url == "https://example.com/news"
        assert feed.site_url == "https://example.com/news"

    async def test_credentials_are_stored_encrypted_and_the_row_is_private(self):
        feed, _ = await self._subscribe("https://bob:hunter2@example.com/news")
        assert feed.is_private is True
        assert feed_auth(feed.fetch_auth_user, feed.fetch_auth_pass_encrypted) == (
            "bob", "hunter2"
        )

    async def test_the_selector_check_fetches_with_the_credentials(self):
        # Otherwise saving a scrape feed behind a login fails on a page the feed
        # itself would go on to scrape fine.
        _, fetch = await self._subscribe("https://bob:hunter2@example.com/news")
        assert fetch.await_args.args[0] == "https://example.com/news"
        assert fetch.await_args.kwargs["auth"] == ("bob", "hunter2")

    async def test_a_plain_scrape_feed_stays_public(self):
        feed, _ = await self._subscribe("https://example.com/news")
        assert feed.is_private is False
        assert feed.fetch_auth_user is None


class TestScrapeFetchUsesStoredCredentials:
    async def test_the_stored_pair_reaches_the_fetch(self):
        from app.fetcher.scrape import fetch_scrape_feed

        feed = SimpleNamespace(
            id=1, feed_url="https://example.com/news", block_count=0, is_private=True,
            fetch_auth_user="bob", fetch_auth_pass_encrypted=encrypt("hunter2"),
            type_config={"article_links_selector": "article a"},
        )
        db = AsyncMock()
        db.add = MagicMock()
        with (
            patch("app.fetcher.scrape.async_validate_feed_url", new=AsyncMock()),
            patch("app.fetcher.scrape.fetch_url_page") as fetch,
            patch("app.fetcher.scrape.extract_article_links", return_value=[]),
        ):
            fetch.return_value = SimpleNamespace(text="<html/>", permanent_url=None)
            await fetch_scrape_feed(feed, db)

        assert fetch.call_args.args[1] == ("bob", "hunter2")
