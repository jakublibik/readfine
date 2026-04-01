"""Feed subscription service: subscribe, unsubscribe, list."""
import asyncio
import logging

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
    is_private: bool = False,
) -> UserFeed:
    """
    Subscribe a user to a feed URL.

    Public feeds (no auth) are shared: if the feed already exists in DB, the
    existing row is reused. Private feeds always get a dedicated row.
    """
    is_private = is_private or bool(fetch_auth_user or fetch_auth_pass)

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


async def list_user_feeds(user: User, db: AsyncSession) -> list[UserFeed]:
    """Return all subscriptions for a user, ordered by folder name then feed name (both alphabetical)."""
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
    return result.scalars().all()
