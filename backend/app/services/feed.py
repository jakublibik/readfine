"""Feed subscription service: subscribe, unsubscribe, list."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import feedparser
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.fetcher.rss import fetch_and_parse_url, fetch_feed, is_full_content_feed
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, Folder, UserFeed
from app.models.settings import AppSettings
from app.models.user import User
from app.services.article import permanently_kept_exists, permanently_kept_predicate
from app.services.readable_service import sample_feed_content
from app.services.scope_cleanup import ScopeCleanupResult, strip_scope_references
from app.utils.crypto import auth_pair, encrypt
from app.utils.url_validator import async_validate_feed_url, split_url_credentials

logger = logging.getLogger(__name__)

# Recent articles sampled to decide whether a feed already delivers whole articles,
# when subscribing to one that is already in the database. Smaller than the fetcher's
# own sample (readable_service._FULL_CONTENT_SAMPLE): the answer is needed while the
# user waits, and getting it wrong costs them one checkbox in Settings → Feeds.
_SUBSCRIBE_SAMPLE = 5


class FeedSubscriptionError(ValueError):
    """Base for subscribe errors.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    working; the typed subclasses let callers map the failure to the right HTTP
    status (or import-loop action) without string-matching the message.
    """


class FeedLimitReached(FeedSubscriptionError):
    """The user is already at their feed-subscription cap."""

    def __init__(self, max_feeds: int):
        self.max_feeds = max_feeds
        super().__init__(f"Feed limit reached ({max_feeds})")


class AlreadySubscribed(FeedSubscriptionError):
    """The user already subscribes to this feed."""

    def __init__(self, message: str = "Already subscribed to this feed"):
        super().__init__(message)


# Feed IDs for which an initial fetch task is already running.
# Prevents duplicate concurrent fetches when multiple users subscribe simultaneously.
_initial_fetch_in_progress: set[int] = set()

# Short-lived cache of a fetched+parsed feed, shared between the "Test feed" step
# and Subscribe so that adding a feed costs a single network request. Without it,
# test + subscribe + initial fetch are three requests within seconds, which trips
# rate-limited sites (e.g. Reddit) into a 429. Public/no-auth feeds only; keyed by
# the (normalized) feed URL. In-process cache — fine for the single-process deploy,
# same as _initial_fetch_in_progress.
_FEED_PREVIEW_TTL = 120.0  # seconds
# url → (expiry, parsed, permanent_url). The third element is the address the URL
# permanently redirects to, so subscribe() can create the feed row on the real
# address without spending a second request to discover it.
_feed_preview_cache: dict[str, tuple[float, feedparser.FeedParserDict, str | None]] = {}


def cache_feed_preview(
    url: str, parsed: feedparser.FeedParserDict, permanent_url: str | None = None
) -> None:
    """Store a successful public-feed parse for brief reuse by subscribe()."""
    now = time.monotonic()
    for stale in [k for k, (exp, _, _) in _feed_preview_cache.items() if exp <= now]:
        _feed_preview_cache.pop(stale, None)
    _feed_preview_cache[url] = (now + _FEED_PREVIEW_TTL, parsed, permanent_url)


def _live_preview(url: str) -> tuple[feedparser.FeedParserDict, str | None] | None:
    """Return the still-fresh cache entry for *url*, evicting an expired one."""
    entry = _feed_preview_cache.get(url)
    if entry is None:
        return None
    expiry, parsed, permanent_url = entry
    if expiry <= time.monotonic():
        _feed_preview_cache.pop(url, None)
        return None
    return parsed, permanent_url


def get_cached_feed_preview(url: str) -> feedparser.FeedParserDict | None:
    """Return a still-fresh cached parse for *url*, else None (evicting if expired)."""
    entry = _live_preview(url)
    return entry[0] if entry else None


def get_cached_permanent_url(url: str) -> str | None:
    """Return the permanent redirect target recorded with *url*'s cached parse."""
    entry = _live_preview(url)
    return entry[1] if entry else None


async def _raise_if_already_subscribed_private(
    db: AsyncSession,
    user: User,
    url: str,
    fetch_auth_user: str | None,
    selector: str | None = None,
) -> None:
    """Refuse a second subscription to a feed the user already has credentials for.

    A feed carrying credentials gets a row of its own, which is what keeps one
    subscriber's password off everyone else's fetches. That also puts it out of reach
    of the shared-row lookup, the one that would otherwise notice the user is already
    subscribed, so without this the same address added twice would quietly become two
    feeds: two rows in the sidebar and two fetches an hour for one feed.

    Scoped to rows this user already subscribes to, so two people using the same
    credentialed address still get a row each rather than silently sharing one.
    """
    if fetch_auth_user is None:
        return
    stmt = (
        select(UserFeed.id)
        .join(Feed, Feed.id == UserFeed.feed_id)
        .where(
            UserFeed.user_id == user.id,
            Feed.feed_url == url,
            Feed.is_private == True,  # noqa: E712
            Feed.fetch_auth_user == fetch_auth_user,
        )
        .limit(1)
    )
    if selector is not None:
        stmt = stmt.where(Feed.type_config["article_links_selector"].astext == selector)
    if await db.scalar(stmt) is not None:
        raise AlreadySubscribed()


async def subscribe(
    user: User,
    url: str,
    folder_id: int | None,
    custom_title: str | None,
    fetch_auth_user: str | None,
    fetch_auth_pass: str | None,
    db: AsyncSession,
    is_private: bool = False,
    trigger_initial_fetch: bool = True,
    import_mode: str = "recent",
    import_limit: int = 500,
    fetch_interval_min: int | None = None,
) -> UserFeed:
    """
    Subscribe a user to a feed URL.

    Public feeds (no auth) are shared: if the feed already exists in DB, the
    existing row is reused. Private feeds always get a dedicated row.

    Credentials written into the address (``https://user:pass@host/feed``) are moved
    into the auth columns before anything else happens, so the password is encrypted
    like any other, never reaches ``feeds.feed_url`` (and from there the backups, the
    admin screens, the feed's own display name and an OPML export), and the row is
    recognised as private rather than shared with everyone else on the instance.
    """
    url, url_auth_user, url_auth_pass = split_url_credentials(url)
    if url_auth_user is not None and not fetch_auth_user and not fetch_auth_pass:
        # Only when the form left both fields empty: pairing a typed username with a
        # password out of the address would authenticate as neither.
        fetch_auth_user, fetch_auth_pass = url_auth_user, url_auth_pass
    if fetch_auth_user and len(fetch_auth_user) > 255:
        raise ValueError("Username is too long (max 255 characters)")

    is_private = is_private or bool(fetch_auth_user or fetch_auth_pass)

    # SSRF protection
    await async_validate_feed_url(url)

    # Validate folder ownership
    if folder_id is not None:
        folder_result = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
        )
        if not folder_result.scalar_one_or_none():
            raise ValueError("Folder not found")

    # Check subscription limit (admins are exempt)
    if user.role != "admin":
        app_settings_result = await db.execute(
            select(AppSettings.max_feeds_per_user).where(AppSettings.id == 1)
        )
        max_feeds = app_settings_result.scalar_one_or_none() or 200
        count_result = await db.execute(
            select(func.count(UserFeed.id)).where(UserFeed.user_id == user.id)
        )
        if (count_result.scalar() or 0) >= max_feeds:
            raise FeedLimitReached(max_feeds)

    feed: Feed | None = None
    parsed = None
    # Both halves or nothing, the same rule feed_auth reads them back under.
    auth = auth_pair(fetch_auth_user, fetch_auth_pass)

    async def _existing_public_feed(feed_url: str) -> Feed | None:
        """The shared public feed row at *feed_url*, if any (raises if subscribed)."""
        if is_private:
            return None
        existing = await db.execute(
            select(Feed).where(Feed.feed_url == feed_url, Feed.is_private == False)
        )
        found = existing.scalar_one_or_none()
        if found:
            already = await db.execute(
                select(UserFeed).where(
                    UserFeed.user_id == user.id,
                    UserFeed.feed_id == found.id,
                )
            )
            if already.scalar_one_or_none():
                raise AlreadySubscribed()
        return found

    await _raise_if_already_subscribed_private(db, user, url, fetch_auth_user)
    feed = await _existing_public_feed(url)

    if feed is None:
        # Reuse a recent Test-step parse if available so the whole add flow is a
        # single network request (avoids tripping rate limits like Reddit's). Only
        # public feeds are cached; private/auth feeds always fetch fresh.
        parsed = None if is_private else get_cached_feed_preview(url)
        permanent_url = None if is_private else get_cached_permanent_url(url)
        if parsed is None:
            parsed, permanent_url = await fetch_and_parse_url(url, auth=auth)

        # Create the row on the address the host actually serves. Storing the URL the
        # user typed would make every later poll walk the same redirect chain, and on
        # an OPML re-import it would create a second row for a feed we already have.
        if permanent_url and permanent_url != url:
            url = permanent_url
            await _raise_if_already_subscribed_private(db, user, url, fetch_auth_user)
            feed = await _existing_public_feed(url)

    if feed is None:
        title = (
            custom_title
            or parsed.feed.get("title")
            or url
        )
        site_url = parsed.feed.get("link")

        feed = Feed(
            feed_url=url,
            is_private=is_private,
            fetch_auth_user=fetch_auth_user if is_private else None,
            # Non-NULL rather than truthy: an address of the form
            # https://user@host/feed authenticates with an empty password, and the
            # pair only survives the move if both columns are written. See
            # app.utils.crypto.feed_auth, which reads them back under the same rule.
            fetch_auth_pass_encrypted=(
                encrypt(fetch_auth_pass) if is_private and fetch_auth_pass is not None else None
            ),
            title=title[:255],
            site_url=site_url[:2048] if site_url else None,
            subscriber_count=0,
            fetch_interval_min=fetch_interval_min,
        )
        db.add(feed)
        await db.flush()  # get feed.id

    await db.execute(
        update(Feed).where(Feed.id == feed.id).values(subscriber_count=Feed.subscriber_count + 1)
    )

    # Determine whether readable extraction makes sense for this feed
    if parsed is not None:
        # New feed: check entries we just fetched
        extract_readable = not is_full_content_feed(parsed)
    else:
        # Existing feed: ask the same measurement the fetcher's auto-disable uses, so
        # the two cannot disagree about the same feed. A smaller sample, because this
        # answer is needed now and the decision is undone by one checkbox, where
        # auto-disable turns extraction off for every subscriber at once.
        sample = await sample_feed_content(feed.id, db, limit=_SUBSCRIBE_SAMPLE)
        extract_readable = not sample.is_full_content

    user_feed = UserFeed(
        user_id=user.id,
        feed_id=feed.id,
        folder_id=folder_id,
        custom_title=custom_title[:255] if custom_title else None,
        extract_readable=extract_readable,
    )
    db.add(user_feed)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AlreadySubscribed()
    await db.refresh(user_feed)
    user_feed.feed = feed

    # Kick off initial fetch in the background (skip if already running for this feed).
    # Mark in-progress synchronously here, before spawning the task: if .add() lived
    # inside _initial_fetch it would only run once the task is scheduled, so two
    # concurrent subscribes to the same new feed could both pass the guard and fetch
    # it twice. (Downstream dedup makes that safe, just wasteful.)
    if trigger_initial_fetch and feed.id not in _initial_fetch_in_progress:
        _initial_fetch_in_progress.add(feed.id)
        # Reuse the parse we already have (new public feed) so the initial import
        # doesn't re-download — one fetch for the whole subscribe.
        asyncio.create_task(_initial_fetch(feed.id, import_mode, import_limit, prefetched=parsed))

    return user_feed


async def _initial_fetch(
    feed_id: int,
    import_mode: str = "recent",
    import_limit: int = 500,
    prefetched: feedparser.FeedParserDict | None = None,
) -> None:
    """Run an immediate fetch for a newly subscribed feed.

    import_mode "recent" (default): import only articles published within the retention
    horizon (published_cutoff), no count limit. import_mode "latest": no time cutoff,
    import up to import_limit newest articles (e.g. pulling a full archive feed).

    The caller (subscribe) already added feed_id to _initial_fetch_in_progress;
    this only owns the discard.
    """
    try:
        import app.database as db_module
        if db_module.async_session_factory is None:
            return
        async with db_module.async_session_factory() as session:
            feed = await session.get(Feed, feed_id)
            if not feed:
                return
            # Scheduler may have already fetched this feed while we were queued
            if feed.last_fetched_at is not None:
                return
            published_cutoff = None
            initial_limit: int | None = None
            if import_mode == "latest":
                initial_limit = import_limit
            else:
                # Recent: bound the import to the retention horizon so we don't pull
                # (and run readable/scoring on) articles the purge would soon remove.
                days = (await session.execute(
                    select(AppSettings.default_purge_after_days).where(AppSettings.id == 1)
                )).scalar_one_or_none()
                if days:
                    published_cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            await fetch_feed(
                feed, session, initial_limit=initial_limit, published_cutoff=published_cutoff,
                prefetched=prefetched,
            )
    finally:
        _initial_fetch_in_progress.discard(feed_id)


async def subscribe_scrape(
    user: User,
    url: str,
    selector: str,
    title: str,
    folder_id: int | None,
    db: AsyncSession,
    fetch_interval_min: int | None = None,
    validate_selector: bool = True,
) -> UserFeed:
    """Subscribe a user to a scrape-type feed (URL + CSS selector pair).

    With validate_selector=False the live page fetch + selector check is skipped
    (used by OPML import to restore a previously-working scrape feed even when the
    page is momentarily unreachable); the background initial fetch still runs.

    A page behind HTTP credentials is scraped by writing them into its address, the
    only way they can be given for a scrape feed. They are moved into the auth columns
    here, as they are for RSS: left in the address they would be copied by ``urljoin``
    into the address of every article the page links to.
    """
    url, auth_user, auth_pass = split_url_credentials(url)
    auth = auth_pair(auth_user, auth_pass)
    is_private = auth is not None
    if auth_user and len(auth_user) > 255:
        raise ValueError("Username is too long (max 255 characters)")

    await async_validate_feed_url(url)

    if folder_id is not None:
        folder_result = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
        )
        if not folder_result.scalar_one_or_none():
            raise ValueError("Folder not found")

    if user.role != "admin":
        app_settings_result = await db.execute(
            select(AppSettings.max_feeds_per_user).where(AppSettings.id == 1)
        )
        max_feeds = app_settings_result.scalar_one_or_none() or 200
        count_result = await db.execute(
            select(func.count(UserFeed.id)).where(UserFeed.user_id == user.id)
        )
        if (count_result.scalar() or 0) >= max_feeds:
            raise FeedLimitReached(max_feeds)

    selector = selector.strip()
    if not selector:
        raise ValueError("CSS selector is required")
    if len(selector) > 500:
        raise ValueError("CSS selector is too long (max 500 characters)")

    # Validate selector against the live page before saving
    if validate_selector:
        from app.fetcher.scrape import extract_article_links, fetch_page_html
        try:
            html = await fetch_page_html(url, auth=auth)
        except Exception as exc:
            raise ValueError(f"Could not fetch the page: {exc}") from exc
        links = extract_article_links(html, selector, url)
        if not links:
            raise ValueError(
                f"CSS selector '{selector}' matched no article links on the page. "
                "Use the Preview button to test your selector before saving."
            )

    await _raise_if_already_subscribed_private(db, user, url, auth_user, selector=selector)

    # Share public scrape feeds with matching URL + selector. A feed with credentials
    # is never shared, so it skips the lookup and always gets a row of its own.
    feed = None if is_private else (await db.execute(
        select(Feed).where(
            Feed.feed_url == url,
            Feed.feed_type == "scrape",
            Feed.is_private == False,
            Feed.type_config["article_links_selector"].astext == selector,
        )
    )).scalar_one_or_none()
    is_new_feed = feed is None

    if feed:
        already = await db.execute(
            select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed.id)
        )
        if already.scalar_one_or_none():
            raise AlreadySubscribed(f"Already subscribed to this URL with the same CSS selector ({selector})")
    else:
        feed = Feed(
            feed_url=url[:2048],
            feed_type="scrape",
            is_private=is_private,
            fetch_auth_user=auth_user,
            # See subscribe(): non-NULL, not truthy, so an empty password survives.
            fetch_auth_pass_encrypted=encrypt(auth_pass) if auth_pass is not None else None,
            title=title[:255],
            site_url=url[:2048],
            type_config={"article_links_selector": selector},
            subscriber_count=0,
            fetch_interval_min=fetch_interval_min,
        )
        db.add(feed)
        await db.flush()

    await db.execute(
        update(Feed).where(Feed.id == feed.id).values(subscriber_count=Feed.subscriber_count + 1)
    )

    user_feed = UserFeed(
        user_id=user.id,
        feed_id=feed.id,
        folder_id=folder_id,
        extract_readable=True,
    )
    db.add(user_feed)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AlreadySubscribed(f"Already subscribed to this URL with the same CSS selector ({selector})")
    await db.refresh(user_feed)

    # Mark in-progress synchronously before spawning (see subscribe() for why).
    if is_new_feed and feed.id not in _initial_fetch_in_progress:
        _initial_fetch_in_progress.add(feed.id)
        asyncio.create_task(_initial_fetch_scrape(feed.id))

    return user_feed


async def _initial_fetch_scrape(feed_id: int) -> None:
    """Run an immediate scrape for a newly subscribed scrape feed.

    The caller (subscribe_scrape) already added feed_id to
    _initial_fetch_in_progress; this only owns the discard.
    """
    try:
        import app.database as db_module
        from app.fetcher.scrape import fetch_scrape_feed
        if db_module.async_session_factory is None:
            return
        async with db_module.async_session_factory() as session:
            feed = await session.get(Feed, feed_id)
            if not feed or feed.last_fetched_at is not None:
                return
            await fetch_scrape_feed(feed, session)
    finally:
        _initial_fetch_in_progress.discard(feed_id)


async def unsubscribe(user: User, user_feed_id: int, db: AsyncSession) -> ScopeCleanupResult:
    """Remove a user's subscription with full lifecycle cleanup.

    1. Deletes UserArticleState rows for articles the user does not keep for good.
    2. Deletes the UserFeed row.
    3. Decrements subscriber_count on the Feed.
    4. If subscriber_count reaches 0: deletes orphan articles (kept for good by
       nobody) and the Feed itself if no articles remain.
    5. Strips the feed from the user's filter/catchup/briefing scopes.

    "Kept for good" is ``permanently_kept_predicate`` — starred, archived or saved
    by URL. Saving belongs there for the same reason starring does, and an article
    can be saved by a user who never subscribed to the feed it came in through
    (a paste that deduped onto it), so both the row-level delete and the
    article-level one have to ask the shared question rather than their own.

    Returns the scope-cleanup report (filters deactivated / briefings disabled).
    """
    result = await db.execute(
        select(UserFeed).where(UserFeed.id == user_feed_id, UserFeed.user_id == user.id)
    )
    user_feed = result.scalar_one_or_none()
    if not user_feed:
        raise ValueError("Subscription not found")

    feed_id = user_feed.feed_id

    # 1. Drop this user's state for the feed's articles, except what they keep for good
    article_ids_subq = select(Article.id).where(Article.feed_id == feed_id).scalar_subquery()
    await db.execute(
        delete(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id.in_(article_ids_subq),
            ~permanently_kept_predicate(),
        )
    )

    # 2. Delete the subscription
    await db.delete(user_feed)

    # 3. Atomically decrement subscriber_count (floor 0)
    await db.execute(
        update(Feed)
        .where(Feed.id == feed_id)
        .values(subscriber_count=func.greatest(Feed.subscriber_count - 1, 0))
    )
    result = await db.execute(select(Feed).where(Feed.id == feed_id))
    feed = result.scalar_one_or_none()

    if feed:
        # 4. If no subscribers left: orphan surviving articles, delete the rest, delete the feed
        if feed.subscriber_count == 0:
            kept_by_someone = permanently_kept_exists()
            # Surviving articles (kept for good by someone): detach from feed (feed_id = NULL)
            await db.execute(
                update(Article)
                .where(Article.feed_id == feed_id, kept_by_someone)
                .values(feed_id=None)
            )
            # Delete the remaining articles (kept for good by nobody)
            await db.execute(
                delete(Article).where(
                    Article.feed_id == feed_id,
                    ~kept_by_someone,
                )
            )
            # Always delete the feed — no subscribers remain
            await db.delete(feed)

    # Strip this feed from the user's filter/catchup/briefing scopes so it does
    # not dangle after the subscription is gone.
    cleanup = await strip_scope_references(db, kind="feed", ref_id=feed_id, user_id=user.id)

    await db.commit()
    return cleanup


async def cleanup_user_feeds(user_id: int, db: AsyncSession) -> None:
    """Clean up all feed subscriptions for a user being deleted (no commit).

    For each subscription: removes UserArticleState rows, decrements subscriber_count,
    and deletes the feed + its articles if no subscribers remain.
    Called by admin delete_user before the user row is deleted.
    """
    user_feeds_result = await db.execute(
        select(UserFeed).where(UserFeed.user_id == user_id)
    )
    user_feeds = user_feeds_result.scalars().all()

    # Same rule as unsubscribe: an article another user keeps for good must outlive
    # this account's feeds, and saving counts as keeping.
    kept_by_someone = permanently_kept_exists()

    for uf in user_feeds:
        feed_id = uf.feed_id
        article_ids_subq = select(Article.id).where(Article.feed_id == feed_id).scalar_subquery()

        await db.execute(
            delete(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id.in_(article_ids_subq),
                ~permanently_kept_predicate(),
            )
        )
        await db.delete(uf)
        await db.execute(
            update(Feed)
            .where(Feed.id == feed_id)
            .values(subscriber_count=func.greatest(Feed.subscriber_count - 1, 0))
        )
        feed = await db.scalar(select(Feed).where(Feed.id == feed_id))
        if feed and feed.subscriber_count == 0:
            await db.execute(
                update(Article)
                .where(Article.feed_id == feed_id, kept_by_someone)
                .values(feed_id=None)
            )
            await db.execute(
                delete(Article).where(Article.feed_id == feed_id, ~kept_by_someone)
            )
            await db.delete(feed)


async def attach_unread_counts(user_id: int, user_feeds, db: AsyncSession) -> None:
    """Set each feed's ``unread_count`` from a value computed fresh from the DB.

    ``UserFeed`` stores no unread column; every API response that carries the
    number computes it on read (excluding retention-trimmed stubs), matching what
    the web UI shows. Writes a plain, non-persisted attribute on each ORM object;
    the GET path never commits, so nothing is written back to the DB.
    """
    if not user_feeds:
        return
    feed_ids = [uf.feed_id for uf in user_feeds]
    fresh = dict((await db.execute(
        select(Article.feed_id, func.count(Article.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == user_id),
        )
        .where(
            Article.feed_id.in_(feed_ids),
            Article.trimmed_at.is_(None),
            (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
        )
        .group_by(Article.feed_id)
    )).all())
    for uf in user_feeds:
        uf.unread_count = fresh.get(uf.feed_id, 0)


async def list_user_feeds(
    user: User, db: AsyncSession, include_unread: bool = False
) -> list[UserFeed]:
    """Return all subscriptions for a user, ordered by folder name then feed name (both alphabetical).

    With ``include_unread=True`` each returned object gets an ``unread_count``
    computed fresh from the DB (excluding retention-trimmed stubs), matching what
    the web UI shows. Off by default so web callers, which compute their own
    counts, don't pay for a redundant query.
    """
    result = await db.execute(
        select(UserFeed)
        .join(Feed, Feed.id == UserFeed.feed_id)
        .outerjoin(Folder, Folder.id == UserFeed.folder_id)
        .options(selectinload(UserFeed.feed), selectinload(UserFeed.folder))
        .where(UserFeed.user_id == user.id)
        .order_by(
            func.lower(Folder.name).nulls_last(),
            func.lower(func.coalesce(UserFeed.custom_title, Feed.title)),
        )
    )
    user_feeds = result.scalars().all()

    if include_unread:
        await attach_unread_counts(user.id, user_feeds, db)

    return user_feeds
