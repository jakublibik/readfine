"""Reading side of story grouping: the rest of the coverage of one piece of news.

``app.fetcher.stories`` builds the groups, this reads them back for one reader. The
split matters: ``story_id`` is global, so a group routinely holds articles from feeds
this reader doesn't subscribe to. Everything here goes through
``add_article_access_joins`` + ``article_access_predicate``, the same gate as every
other article read path, and nothing outside this module should query members itself.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.schemas.article import StoryMember
from app.services.article import add_article_access_joins, article_access_predicate

# A group is small by construction (measured median 2, largest 13), so this is a guard
# against a pathological cluster, not a page size. Nothing paginates the footer.
MEMBER_LIMIT = 25


def _members_query(columns, user_id: int, story_id: int, exclude_article_id: int):
    """Members of one story that this user may see, minus the article being read.

    Trimmed articles are left out for the same reason the list leaves them out: the
    retention pass stripped them down to a snippet and they are gone from every other
    view, so offering one here would open a stub.
    """
    return add_article_access_joins(select(*columns), user_id).where(
        Article.story_id == story_id,
        Article.id != exclude_article_id,
        Article.trimmed_at.is_(None),
        article_access_predicate(),
    )


async def count_members(
    user_id: int, story_id: int | None, article_id: int, db: AsyncSession
) -> int:
    """How many other sources this reader can actually open.

    Asked on every article open, so it stays a count: the footer only needs to know
    whether to render, and the members themselves are fetched when it is unfolded.
    A group can also shrink to nothing visible, through unsubscribing or retention,
    which is why "story_id is set" is never treated as "there is something to show".
    """
    if story_id is None:
        return 0
    return await db.scalar(
        _members_query([func.count(Article.id)], user_id, story_id, article_id)
    ) or 0


async def list_members(
    user_id: int, story_id: int | None, article_id: int, db: AsyncSession
) -> list[StoryMember]:
    """The other coverage, newest first, whatever state it is in.

    Read, starred and (from the suppression branch) hidden articles all belong here:
    the point of the footer is that it shows what the reader would otherwise not find,
    and filtering it by state would hide exactly the articles it exists to surface.
    """
    if story_id is None:
        return []
    rows = (await db.execute(
        _members_query(
            [
                Article.id,
                Article.title,
                Article.url,
                Article.published_at,
                Article.fetched_at,
                Feed.title.label("feed_title"),
                UserFeed.custom_title,
                UserArticleState.is_read,
                UserArticleState.is_starred,
            ],
            user_id, story_id, article_id,
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .order_by(
            func.coalesce(Article.published_at, Article.fetched_at).desc(),
            Article.id.desc(),
        )
        .limit(MEMBER_LIMIT)
    )).all()

    return [
        StoryMember(
            id=r.id,
            title=r.title,
            url=r.url,
            feed_title=r.custom_title or r.feed_title,
            published_at=r.published_at or r.fetched_at,
            is_read=bool(r.is_read),
            is_starred=bool(r.is_starred),
        )
        for r in rows
    ]
