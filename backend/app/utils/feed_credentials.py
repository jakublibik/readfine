"""Moving credentials out of a stored address and into the columns built for them.

The subscribe paths do this as the feed is created; this module holds the part the
one-off backfill of existing rows needs as well. It is deliberately free of models
and of the encryption helpers, so it stays a pure function that can be unit-tested
and a migration can call it without importing half the application (and without
inheriting whatever ``app.utils.crypto`` grows into later).
"""
import hashlib
from typing import NamedTuple

from app.utils.parsing import normalize_url
from app.utils.url_validator import split_url_credentials

# Widths of the columns being written into, from app.models.feed / app.models.article.
_TITLE_MAX = 255
_AUTH_USER_MAX = 255
_URL_MAX = 2048


class FeedCredentialSplit(NamedTuple):
    """New values for one feed whose address carried credentials.

    ``fetch_auth_user`` / ``fetch_auth_pass`` are ``None`` when the auth columns
    should be left alone, which happens when the feed already has credentials from
    the HTTP auth form and the address was merely repeating them. They are never used
    to mean "store NULL": a split either writes both columns or neither.

    ``fetch_auth_pass`` is plaintext. Encrypting it is the caller's job, which is what
    keeps this module free of the crypto helpers.
    """
    feed_url: str
    fetch_auth_user: str | None
    fetch_auth_pass: str | None
    site_url: str | None
    title: str


class ArticleUrlRewrite(NamedTuple):
    """New values for one article whose address carried credentials.

    Scrape feeds build article links with ``urljoin`` against the feed's own address,
    so credentials in it end up in every article too, and in the dedup key derived
    from the address. ``guid`` / ``guid_hash`` are ``None`` when the article's guid is
    something other than its address (an RSS feed supplies its own), leaving the
    dedup key untouched.
    """
    url: str
    guid: str | None
    guid_hash: str | None
    url_normalized: str | None


def plan_feed_credential_split(
    *,
    feed_url: str,
    title: str,
    site_url: str | None,
    fetch_auth_user: str | None,
) -> FeedCredentialSplit | None:
    """What to write for a feed whose address may carry credentials, or None.

    None means there is nothing to do, either because the address holds no userinfo
    or because the username in it will not fit ``feeds.fetch_auth_user``. Refusing the
    second case rather than truncating it keeps a feed authenticating with the wrong
    username out of the database; such a feed keeps its address and stays as it was.
    """
    clean_url, username, password = split_url_credentials(feed_url)
    if clean_url == feed_url:
        return None
    if username is not None and len(username) > _AUTH_USER_MAX:
        return None

    # Credentials typed into the HTTP auth form are the ones the user maintains, and
    # the only ones the edit form can change, so they win over a copy in the address.
    if fetch_auth_user is not None:
        username = password = None

    return FeedCredentialSplit(
        feed_url=clean_url,
        fetch_auth_user=username,
        fetch_auth_pass=password,
        site_url=split_url_credentials(site_url)[0] if site_url else site_url,
        # A feed that arrived without a title of its own was named after its address
        # (see subscribe), which for these feeds means the password is the feed's
        # display name. The stored title is already cut to the column, so that is what
        # the old address has to be compared against.
        title=clean_url[:_TITLE_MAX] if title == feed_url[:_TITLE_MAX] else title,
    )


def plan_article_url_rewrite(*, url: str, guid: str | None) -> ArticleUrlRewrite | None:
    """What to write for an article whose address may carry credentials, or None.

    The dedup key has to move with the address. Leaving ``guid_hash`` and
    ``url_normalized`` pointing at the old form would make the next scrape of the
    feed recompute both from the clean link, match nothing, and import every article
    a second time.
    """
    clean_url, _, _ = split_url_credentials(url)
    if clean_url == url:
        return None
    guid_is_the_url = guid is not None and guid == url[:_URL_MAX]
    return ArticleUrlRewrite(
        url=clean_url[:_URL_MAX],
        guid=clean_url[:_URL_MAX] if guid_is_the_url else None,
        # Hashed before the truncation, because that is the order the scrape fetcher
        # works in: it hashes the link it found and stores a cut-down copy as the guid.
        guid_hash=hashlib.sha256(clean_url.encode()).hexdigest() if guid_is_the_url else None,
        url_normalized=normalize_url(clean_url),
    )
