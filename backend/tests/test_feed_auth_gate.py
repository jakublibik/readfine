"""Who may write HTTP credentials to a feed, and who may join one that has them.

Credentials live on the shared Feed row and go out on every subscriber's fetch, so
both halves of the rule are about keeping them to one person: a feed with credentials
gains no second subscriber, and a feed with several subscribers takes no credentials.

The two used to be spelled differently in the form and in the API, and the form's
version let one subscriber of a shared private feed rewrite the password for everyone.
Migration 0090 is what made that reachable: it turned public rows whose address carried
credentials private and left their subscribers where they were.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.feed import SharedPrivateFeed, attach_subscriber, may_edit_feed_auth


def _feed(**kwargs):
    defaults = dict(
        id=1, feed_url="https://example.com/feed.xml", site_url="https://example.com",
        title="Example Feed", favicon_url=None, status="active", last_fetched_at=None,
        last_error=None, block_count=0, fetch_error_count=0, subscriber_count=1,
        feed_type="rss", is_private=False, fetch_auth_user=None,
        fetch_auth_pass_encrypted=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestMayEditFeedAuth:
    def test_sole_subscriber_of_a_public_feed_may(self):
        # Setting credentials is what turns a row private, so being public is no bar.
        assert may_edit_feed_auth(_feed(is_private=False, subscriber_count=1)) is True

    def test_sole_subscriber_of_a_private_feed_may(self):
        assert may_edit_feed_auth(_feed(is_private=True, subscriber_count=1)) is True

    def test_shared_private_feed_may_not(self):
        # The bug: the form used to allow this because the feed was private, letting
        # one subscriber rewrite or clear the password the others fetch with.
        assert may_edit_feed_auth(_feed(is_private=True, subscriber_count=3)) is False

    def test_shared_public_feed_may_not(self):
        assert may_edit_feed_auth(_feed(is_private=False, subscriber_count=3)) is False


@pytest.mark.asyncio
class TestAttachSubscriber:
    @staticmethod
    def _db():
        db = AsyncMock()
        db.add = MagicMock()
        return db

    async def test_refuses_a_feed_that_already_carries_credentials(self):
        db = self._db()
        with pytest.raises(SharedPrivateFeed):
            await attach_subscriber(
                _feed(is_private=True, subscriber_count=1),
                SimpleNamespace(id=7), db,
            )
        db.add.assert_not_called()
        db.execute.assert_not_awaited()  # no subscriber_count bumped either

    async def test_allows_a_private_feed_that_has_no_subscriber_yet(self):
        # The ordinary path: subscribing with credentials creates the row, then joins it.
        db = self._db()
        await attach_subscriber(
            _feed(is_private=True, subscriber_count=0), SimpleNamespace(id=7), db,
        )
        db.add.assert_called_once()

    async def test_allows_a_shared_public_feed(self):
        db = self._db()
        await attach_subscriber(
            _feed(is_private=False, subscriber_count=9), SimpleNamespace(id=7), db,
        )
        db.add.assert_called_once()


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalars.return_value.all.return_value = []
    return r


def _user_feed(feed):
    return SimpleNamespace(
        id=1, feed_id=feed.id, folder_id=None, custom_title=None, extract_readable=False,
        unread_count=0, position=0, created_at=datetime(2024, 1, 1), feed=feed,
    )


class TestApiCredentialGate:
    """The API half. The form half is the same function, covered above."""

    @staticmethod
    def _patch(client, mock_db, feed, body):
        mock_db.execute.return_value = _scalar_result(_user_feed(feed))
        mock_db.refresh.side_effect = AsyncMock()
        return client.patch("/api/v1/feeds/1", json=body)

    def test_shared_private_feed_is_refused(self, client, mock_db):
        feed = _feed(is_private=True, subscriber_count=2, fetch_auth_user="alice")
        response = self._patch(client, mock_db, feed, {"fetch_auth_user": "bob"})
        assert response.status_code == 403
        assert feed.fetch_auth_user == "alice"

    def test_a_refused_request_does_not_re_enable_the_feed(self, client, mock_db):
        # The status and error counters were cleared before the check, so a request
        # that went on to be refused had already gone through the motions.
        feed = _feed(
            is_private=True, subscriber_count=2, status="disabled",
            fetch_error_count=5, block_count=3,
        )
        response = self._patch(client, mock_db, feed, {"fetch_auth_user": "bob"})
        assert response.status_code == 403
        assert (feed.status, feed.fetch_error_count, feed.block_count) == ("disabled", 5, 3)

    def test_sole_subscriber_of_a_public_feed_may_set_credentials(self, client, mock_db):
        # Used to be refused as "can only be set on private feeds", while the form
        # allowed it and turned the row private.
        feed = _feed(is_private=False, subscriber_count=1)
        response = self._patch(client, mock_db, feed, {"fetch_auth_user": "bob"})
        assert response.status_code == 200
        assert feed.fetch_auth_user == "bob"

    def test_setting_credentials_turns_the_row_private(self, client, mock_db):
        # Otherwise the row stays in the shared pool and the next subscriber joins it,
        # password included.
        feed = _feed(is_private=False, subscriber_count=1)
        self._patch(client, mock_db, feed, {"fetch_auth_user": "bob"})
        assert feed.is_private is True

    def test_the_password_is_stored_encrypted(self, client, mock_db):
        feed = _feed(is_private=True, subscriber_count=1, fetch_auth_user="bob")
        response = self._patch(client, mock_db, feed, {"fetch_auth_pass": "hunter2"})
        assert response.status_code == 200
        assert feed.fetch_auth_pass_encrypted not in (None, "hunter2")

    def test_an_empty_username_is_rejected(self, client, mock_db):
        feed = _feed(is_private=True, subscriber_count=1, fetch_auth_user="bob")
        response = self._patch(client, mock_db, feed, {"fetch_auth_user": ""})
        assert response.status_code == 400
        assert feed.fetch_auth_user == "bob"
