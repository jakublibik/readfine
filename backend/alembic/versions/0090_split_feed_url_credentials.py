"""Move HTTP credentials out of feed addresses and into the encrypted columns

A feed can authenticate two ways, and one of them stored the password in the clear.
The HTTP auth form encrypts it into feeds.fetch_auth_pass_encrypted and marks the row
private; credentials written into the address (https://user:pass@host/feed) were kept
verbatim in feeds.feed_url. That put the password in the database and in every backup,
left the row public so the shared pool sent one subscriber's credentials on everyone's
behalf, could make it the feed's display name, and put it in an OPML export. For a
scrape feed it also reached every article, because article links are built with
urljoin against the feed's own address.

The subscribe paths now split credentials off as the feed is created. This moves the
rows that already exist.

Two things worth knowing about the shape of this migration:

* The unique indexes on feeds are partial (WHERE is_private = false, see 0037) and
  every row touched here becomes private, so a cleaned address already held by a
  public row is not a conflict and nothing has to be merged.
* Scrape articles carry the address in guid / guid_hash / url_normalized as well.
  Cleaning only articles.url would leave the dedup key pointing at the old form, and
  the next scrape would import the whole feed a second time.

Revision ID: 0090
Revises: 0089
Create Date: 2026-08-08
"""
import base64
import logging

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet

from app.config import settings
from app.utils.feed_credentials import plan_article_url_rewrite, plan_feed_credential_split
from app.utils.url_validator import split_url_credentials

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _fernet() -> Fernet:
    """The same Fernet app.utils.crypto builds, spelled out rather than imported.

    A migration has to keep doing what it did on the day it was written. Calling the
    application's helper would let a later change there (a new token format, key
    rotation) silently rewrite the history of this one.
    """
    key = settings.encryption_key.encode()
    if len(key) == 32:  # raw 32-byte key; a pre-encoded Fernet key is 44 chars
        key = base64.urlsafe_b64encode(key)
    try:
        return Fernet(key)
    except Exception as exc:  # noqa: BLE001 - turned into a message an operator can act on
        raise RuntimeError(
            "ENCRYPTION_KEY is missing or invalid, so feed passwords cannot be "
            "encrypted. Set it to the value the application uses and run the "
            "migration again."
        ) from exc


def upgrade() -> None:
    bind = op.get_bind()

    # LIKE is only a prefilter, and a deliberately loose one: it also matches an "@"
    # in a path or query (?email=a@b). plan_feed_credential_split parses the address
    # and decides.
    feeds = bind.execute(sa.text(
        "SELECT id, feed_url, title, site_url, fetch_auth_user, feed_type "
        "FROM feeds WHERE feed_url LIKE '%@%'"
    )).fetchall()
    if not feeds:
        return

    fernet = None
    for feed in feeds:
        plan = plan_feed_credential_split(
            feed_url=feed.feed_url,
            title=feed.title,
            site_url=feed.site_url,
            fetch_auth_user=feed.fetch_auth_user,
        )
        if plan is None:
            # Either the "@" was in the path or query and there is nothing to do, or
            # the username will not fit feeds.fetch_auth_user. Only the second is
            # worth a line in the log: that feed keeps its address and stays as it was.
            if split_url_credentials(feed.feed_url)[0] != feed.feed_url:
                logger.warning(
                    "feed %d left as it was: the username in its address does not fit "
                    "the column", feed.id
                )
            continue

        values = {
            "id": feed.id,
            "feed_url": plan.feed_url,
            "site_url": plan.site_url,
            "title": plan.title,
        }
        columns = "feed_url = :feed_url, site_url = :site_url, title = :title, is_private = TRUE"
        if plan.fetch_auth_user is not None:
            fernet = fernet or _fernet()
            columns += (
                ", fetch_auth_user = :fetch_auth_user"
                ", fetch_auth_pass_encrypted = :fetch_auth_pass_encrypted"
            )
            values["fetch_auth_user"] = plan.fetch_auth_user
            values["fetch_auth_pass_encrypted"] = fernet.encrypt(
                (plan.fetch_auth_pass or "").encode()
            ).decode()

        bind.execute(sa.text(f"UPDATE feeds SET {columns} WHERE id = :id"), values)

        if feed.feed_type == "scrape":
            _rewrite_scrape_articles(bind, feed.id)

        logger.info("feed %d: credentials moved out of its address", feed.id)


def _rewrite_scrape_articles(bind, feed_id: int) -> None:
    """Clean the addresses a scrape feed's articles inherited, dedup key included."""
    articles = bind.execute(
        sa.text("SELECT id, url, guid FROM articles WHERE feed_id = :fid AND url LIKE '%@%'"),
        {"fid": feed_id},
    ).fetchall()

    for article in articles:
        rewrite = plan_article_url_rewrite(url=article.url, guid=article.guid)
        if rewrite is None:
            continue
        columns = "url = :url, url_normalized = :url_normalized"
        values = {
            "id": article.id,
            "url": rewrite.url,
            "url_normalized": rewrite.url_normalized,
        }
        if rewrite.guid is not None:
            columns += ", guid = :guid, guid_hash = :guid_hash"
            values["guid"] = rewrite.guid
            values["guid_hash"] = rewrite.guid_hash
        # Its own savepoint: the cleaned address can collide with an article the feed
        # already has under (feed_id, guid_hash), and one such article is not a reason
        # to abandon the rest. The old row keeps its address and the next scrape
        # skips it as a duplicate.
        try:
            with bind.begin_nested():
                bind.execute(sa.text(f"UPDATE articles SET {columns} WHERE id = :id"), values)
        except sa.exc.IntegrityError:
            logger.warning(
                "article %d left as it was: its cleaned address is already taken in "
                "feed %d", article.id, feed_id
            )


def downgrade() -> None:
    """Deliberately empty.

    The upgrade is not lost information (the credentials are in the auth columns and
    the fetchers read them from there), and writing plaintext passwords back into
    feed_url is not something worth offering as a rollback.
    """
