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
from app.utils.crypto import encrypt
from app.utils.url_validator import async_validate_feed_url, fetch_url_with_ssrf_check

logger = logging.getLogger(__name__)

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
_feed_preview_cache: dict[str, tuple[float, feedparser.FeedParserDict]] = {}


def cache_feed_preview(url: str, parsed: feedparser.FeedParserDict) -> None:
    """Store a successful public-feed parse for brief reuse by subscribe()."""
    now = time.monotonic()
    for stale in [k for k, (exp, _) in _feed_preview_cache.items() if exp <= now]:
        _feed_preview_cache.pop(stale, None)
    _feed_preview_cache[url] = (now + _FEED_PREVIEW_TTL, parsed)


def get_cached_feed_preview(url: str) -> feedparser.FeedParserDict | None:
    """Return a still-fresh cached parse for *url*, else None (evicting if expired)."""
    entry = _feed_preview_cache.get(url)
    if entry is None:
        return None
    expiry, parsed = entry
    if expiry <= time.monotonic():
        _feed_preview_cache.pop(url, None)
        return None
    return parsed


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
) -> UserFeed:
    """
    Subscribe a user to a feed URL.

    Public feeds (no auth) are shared: if the feed already exists in DB, the
    existing row is reused. Private feeds always get a dedicated row.
    """
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
            raise ValueError(f"Feed limit reached ({max_feeds})")

    feed: Feed | None = None
    parsed = None

    if not is_private:
        # Look for existing public feed
        existing = await db.execute(
            select(Feed).where(Feed.feed_url == url, Feed.is_private == False)
        )
        feed = existing.scalar_one_or_none()

        if feed:
            # Check if already subscribed
            already = await db.execute(
                select(UserFeed).where(
                    UserFeed.user_id == user.id,
                    UserFeed.feed_id == feed.id,
                )
            )
            if already.scalar_one_or_none():
                raise ValueError("Already subscribed to this feed")

    if feed is None:
        # Reuse a recent Test-step parse if available so the whole add flow is a
        # single network request (avoids tripping rate limits like Reddit's). Only
        # public feeds are cached; private/auth feeds always fetch fresh.
        parsed = None if is_private else get_cached_feed_preview(url)
        if parsed is None:
            parsed = await fetch_and_parse_url(url)
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
            fetch_auth_pass_encrypted=encrypt(fetch_auth_pass) if fetch_auth_pass else None,
            title=title[:255],
            site_url=site_url[:2048] if site_url else None,
            subscriber_count=0,
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
        # Existing feed: derive from recent articles already in DB
        sample_result = await db.execute(
            select(Article.word_count)
            .where(Article.feed_id == feed.id, Article.word_count.isnot(None))
            .order_by(Article.id.desc())
            .limit(5)
        )
        word_counts = [r[0] for r in sample_result]
        if word_counts and sum(1 for c in word_counts if c > 500) / len(word_counts) >= 0.8:
            extract_readable = False
        else:
            extract_readable = True

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
        raise ValueError("Already subscribed to this feed")
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
    """
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
            raise ValueError(f"Feed limit reached ({max_feeds})")

    selector = selector.strip()
    if not selector:
        raise ValueError("CSS selector is required")
    if len(selector) > 500:
        raise ValueError("CSS selector is too long (max 500 characters)")

    # Validate selector against the live page before saving
    if validate_selector:
        from app.fetcher.scrape import extract_article_links
        loop = asyncio.get_running_loop()
        try:
            html = await loop.run_in_executor(
                None, fetch_url_with_ssrf_check, url, None, 30,
                {"User-Agent": "Readfine/1.0", "Accept": "text/html,*/*"},
            )
        except Exception as exc:
            raise ValueError(f"Could not fetch the page: {exc}") from exc
        links = extract_article_links(html, selector, url)
        if not links:
            raise ValueError(
                f"CSS selector '{selector}' matched no article links on the page. "
                "Use the Preview button to test your selector before saving."
            )

    # Share public scrape feeds with matching URL + selector
    existing = await db.execute(
        select(Feed).where(
            Feed.feed_url == url,
            Feed.feed_type == "scrape",
            Feed.is_private == False,
            Feed.type_config["article_links_selector"].astext == selector,
        )
    )
    feed = existing.scalar_one_or_none()
    is_new_feed = feed is None

    if feed:
        already = await db.execute(
            select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed.id)
        )
        if already.scalar_one_or_none():
            raise ValueError(f"Already subscribed to this URL with the same CSS selector ({selector})")
    else:
        feed = Feed(
            feed_url=url[:2048],
            feed_type="scrape",
            is_private=False,
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
        raise ValueError(f"Already subscribed to this URL with the same CSS selector ({selector})")
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


async def unsubscribe(user: User, user_feed_id: int, db: AsyncSession) -> None:
    """Remove a user's subscription with full lifecycle cleanup.

    1. Deletes UserArticleState rows for non-starred, non-archived articles.
    2. Deletes the UserFeed row.
    3. Decrements subscriber_count on the Feed.
    4. If subscriber_count reaches 0: deletes orphan articles (not starred/archived
       by anyone) and the Feed itself if no articles remain.
    """
    result = await db.execute(
        select(UserFeed).where(UserFeed.id == user_feed_id, UserFeed.user_id == user.id)
    )
    user_feed = result.scalar_one_or_none()
    if not user_feed:
        raise ValueError("Subscription not found")

    feed_id = user_feed.feed_id

    # 1. Delete non-starred, non-archived UserArticleState rows for this user + feed
    article_ids_subq = select(Article.id).where(Article.feed_id == feed_id).scalar_subquery()
    await db.execute(
        delete(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id.in_(article_ids_subq),
            UserArticleState.is_starred == False,
            UserArticleState.is_archived == False,
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
            starred_or_archived_subq = (
                select(UserArticleState.article_id)
                .where(
                    UserArticleState.article_id == Article.id,
                    (UserArticleState.is_starred == True) | (UserArticleState.is_archived == True),
                )
                .correlate(Article)
                .exists()
            )
            # Surviving articles (starred/archived by someone): detach from feed (feed_id = NULL)
            await db.execute(
                update(Article)
                .where(Article.feed_id == feed_id, starred_or_archived_subq)
                .values(feed_id=None)
            )
            # Delete the remaining articles (not starred/archived by anyone)
            await db.execute(
                delete(Article).where(
                    Article.feed_id == feed_id,
                    ~starred_or_archived_subq,
                )
            )
            # Always delete the feed — no subscribers remain
            await db.delete(feed)

    await db.commit()


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

    starred_or_archived_subq = (
        select(UserArticleState.article_id)
        .where(
            UserArticleState.article_id == Article.id,
            (UserArticleState.is_starred == True) | (UserArticleState.is_archived == True),
        )
        .correlate(Article)
        .exists()
    )

    for uf in user_feeds:
        feed_id = uf.feed_id
        article_ids_subq = select(Article.id).where(Article.feed_id == feed_id).scalar_subquery()

        await db.execute(
            delete(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id.in_(article_ids_subq),
                UserArticleState.is_starred == False,
                UserArticleState.is_archived == False,
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
                .where(Article.feed_id == feed_id, starred_or_archived_subq)
                .values(feed_id=None)
            )
            await db.execute(
                delete(Article).where(Article.feed_id == feed_id, ~starred_or_archived_subq)
            )
            await db.delete(feed)


async def list_user_feeds(
    user: User, db: AsyncSession, include_unread: bool = False
) -> list[UserFeed]:
    """Return all subscriptions for a user, ordered by folder name then feed name (both alphabetical).

    With ``include_unread=True`` each returned object's ``unread_count`` is replaced
    with a value computed fresh from the DB (excluding retention-trimmed stubs),
    matching what the web UI shows. The cached ``UserFeed.unread_count`` column can
    drift (the fetcher and retention don't recompute it consistently), so API
    callers that surface the number should opt in. Off by default so web callers,
    which compute their own counts, don't pay for a redundant query.
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

    if include_unread and user_feeds:
        feed_ids = [uf.feed_id for uf in user_feeds]
        fresh = dict((await db.execute(
            select(Article.feed_id, func.count(Article.id))
            .outerjoin(
                UserArticleState,
                (UserArticleState.article_id == Article.id)
                & (UserArticleState.user_id == user.id),
            )
            .where(
                Article.feed_id.in_(feed_ids),
                Article.trimmed_at.is_(None),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
            .group_by(Article.feed_id)
        )).all())
        # Plain attribute write: the GET path never commits (get_db only rolls
        # back on close) and no query runs after this, so it never persists.
        for uf in user_feeds:
            uf.unread_count = fresh.get(uf.feed_id, 0)

    return user_feeds
