"""Feed subscription service: subscribe, unsubscribe, list."""
import asyncio
import logging

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.fetcher.rss import fetch_and_parse_url, fetch_feed
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, Folder, UserFeed
from app.models.settings import AppSettings
from app.models.user import User
from app.utils.crypto import encrypt
from app.utils.url_validator import validate_feed_url

logger = logging.getLogger(__name__)


async def subscribe(
    user: User,
    url: str,
    folder_id: int | None,
    custom_title: str | None,
    fetch_auth_user: str | None,
    fetch_auth_pass: str | None,
    db: AsyncSession,
) -> UserFeed:
    """
    Subscribe a user to a feed URL.

    Public feeds (no auth) are shared: if the feed already exists in DB, the
    existing row is reused. Private feeds always get a dedicated row.
    """
    is_private = bool(fetch_auth_user or fetch_auth_pass)

    # SSRF protection
    validate_feed_url(url)

    # Validate folder ownership
    if folder_id is not None:
        folder_result = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
        )
        if not folder_result.scalar_one_or_none():
            raise ValueError("Folder not found")

    # Check subscription limit
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

    if not is_private:
        # Look for existing public feed
        existing = await db.execute(
            select(Feed).where(Feed.feed_url == url, Feed.is_private == False)  # noqa: E712
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
        # Fetch feed to discover title / validate URL
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

    feed.subscriber_count += 1

    user_feed = UserFeed(
        user_id=user.id,
        feed_id=feed.id,
        folder_id=folder_id,
        custom_title=custom_title[:255] if custom_title else None,
    )
    db.add(user_feed)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("Already subscribed to this feed")
    await db.refresh(user_feed)

    # Kick off initial fetch in the background
    asyncio.create_task(_initial_fetch(feed.id))

    return user_feed


async def _initial_fetch(feed_id: int) -> None:
    """Run an immediate fetch for a newly subscribed feed."""
    import app.database as db_module
    if db_module.async_session_factory is None:
        return
    async with db_module.async_session_factory() as session:
        feed = await session.get(Feed, feed_id)
        if feed:
            await fetch_feed(feed, session)


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
            UserArticleState.is_starred == False,  # noqa: E712
            UserArticleState.is_archived == False,  # noqa: E712
        )
    )

    # 2. Delete the subscription
    await db.delete(user_feed)

    # 3. Decrement subscriber_count
    result = await db.execute(select(Feed).where(Feed.id == feed_id))
    feed = result.scalar_one_or_none()
    if feed:
        new_count = max(0, (feed.subscriber_count or 0) - 1)
        feed.subscriber_count = new_count

        # 4. If no subscribers left: orphan surviving articles, delete the rest, delete the feed
        if new_count == 0 and feed:
            starred_or_archived_subq = (
                select(UserArticleState.article_id)
                .where(
                    UserArticleState.article_id == Article.id,
                    (UserArticleState.is_starred == True) | (UserArticleState.is_archived == True),  # noqa: E712
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


async def list_user_feeds(user: User, db: AsyncSession) -> list[UserFeed]:
    """Return all subscriptions for a user, ordered by folder/position."""
    result = await db.execute(
        select(UserFeed)
        .options(selectinload(UserFeed.feed), selectinload(UserFeed.folder))
        .where(UserFeed.user_id == user.id)
        .order_by(UserFeed.folder_id.nulls_last(), UserFeed.position)
    )
    return result.scalars().all()
