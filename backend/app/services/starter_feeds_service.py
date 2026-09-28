"""The starter feeds a new account can pick on the second welcome step.

The list lives in ``app/content/starter_feeds.yml``. Choosing a category subscribes
the account to its feeds, in a folder named after the category, through the same
``subscribe()`` as the feeds page. Public feeds are shared, so on an instance where
someone already reads them this adds no network request and the articles are there
at once; only a feed nobody has yet is fetched while the reader waits.
"""
import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import Folder
from app.models.user import User
from app.services.feed import AlreadySubscribed, FeedLimitReached, subscribe
from app.services.folder_service import next_folder_position
from app.services.relevance_service import tokenize

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent.parent / "content" / "starter_feeds.yml"


@dataclass(frozen=True)
class StarterFeed:
    title: str
    url: str


@dataclass(frozen=True)
class StarterCategory:
    id: str
    name: str
    keywords: frozenset[str]
    feeds: tuple[StarterFeed, ...]


@dataclass
class StarterResult:
    added: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    over_limit: bool = False


@functools.lru_cache(maxsize=1)
def load_starter_categories() -> tuple[StarterCategory, ...]:
    """The categories from the YAML file. Cached; read once per process.

    A file that is missing or does not parse (it is the one self-hosters are told
    to edit) gives no categories rather than an error. /app sends every new account
    to the step that reads this, so an exception here would lock them all out; with
    no categories the step still offers the import and add-your-own ways.
    """
    try:
        return _read_categories(_PATH)
    except Exception:
        logger.error("Starter feeds from %s could not be read; offering none", _PATH,
                     exc_info=True)
        return ()


def _read_categories(path: Path) -> tuple[StarterCategory, ...]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return tuple(
        StarterCategory(
            id=str(c["id"]),
            name=str(c["name"]),
            keywords=frozenset(t for k in c.get("keywords") or () for t in tokenize(str(k))),
            feeds=tuple(StarterFeed(title=str(f["title"]), url=str(f["url"])) for f in c["feeds"]),
        )
        for c in data.get("categories") or ()
    )


def preselected(categories: tuple[StarterCategory, ...], terms: str | None) -> set[str]:
    """Ids of the categories whose keywords appear among the reader's topics."""
    words = set(tokenize(terms or ""))
    return {c.id for c in categories if c.keywords & words}


async def subscribe_starter(
    user: User,
    category_ids: list[str],
    db: AsyncSession,
) -> StarterResult:
    """Subscribe *user* to the feeds of the chosen categories.

    A feed that fails (the site is down, the address moved) is left out and named in
    the result; the rest still go in. Unknown ids are ignored, as the form is the only
    source of them.
    """
    result = StarterResult()
    chosen = [c for c in load_starter_categories() if c.id in set(category_ids)]
    next_pos = await next_folder_position(db, user.id)
    for category in chosen:
        folder_id = await db.scalar(
            select(Folder.id).where(Folder.user_id == user.id, Folder.name == category.name)
        )
        if folder_id is None:
            folder = Folder(user_id=user.id, name=category.name, position=next_pos)
            next_pos += 1
            db.add(folder)
            # Committed on its own, so a feed that fails below cannot roll it back
            # from under the feeds after it.
            await db.commit()
            folder_id = folder.id
        for feed in category.feeds:
            try:
                await subscribe(
                    user=user,
                    url=feed.url,
                    folder_id=folder_id,
                    custom_title=feed.title,
                    fetch_auth_user=None,
                    fetch_auth_pass=None,
                    db=db,
                )
                result.added += 1
            except AlreadySubscribed:
                result.skipped += 1
            except FeedLimitReached:
                result.over_limit = True
                return result
            except Exception:
                logger.warning("Starter feed %s failed", feed.url, exc_info=True)
                await db.rollback()
                # The rollback expires the user, and a lazy load is not allowed here.
                await db.refresh(user)
                result.failed.append(feed.title)
    return result
