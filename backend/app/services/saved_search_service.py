"""Saved searches: named sets of search parameters, each listed in the sidebar.

A saved search is a live query. Opening one runs the search again with its
parameters, so it always reflects the articles as they are now; nothing is
evaluated or tagged when it is saved.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, literal, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import sqlstate
from app.models.article import SUPPRESSED_BY_BULK, Article, UserArticleState
from app.models.feed import Folder, UserFeed
from app.models.label import Label
from app.models.saved_search import SavedSearch
from app.models.user import User
from app.services.article import count_articles, list_articles
from app.services.scope_tokens import parse_label_tokens, parse_scope_tokens
from app.services.search_params import list_kwargs, normalize_search_params

MAX_SAVED_SEARCHES = 50
MAX_NAME_LENGTH = 100

# How long a sidebar badge may take to count, per statement; see count_saved_search.
COUNT_BUDGET_MS = 100
_QUERY_CANCELED = "57014"

# Keys that only shape the order. A set holding nothing else would be the whole
# archive under another name, and the list renderer wouldn't treat it as a search.
_ORDER_ONLY = {"sort", "score_source"}


class SavedSearchError(ValueError):
    """A save the reader has to correct; the message is shown as is."""


@dataclass
class _Clean:
    name: str
    params: dict


def _clean(name: str, raw_params: Mapping[str, Any]) -> _Clean:
    name = " ".join((name or "").split())
    if not name:
        raise SavedSearchError("Give the search a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise SavedSearchError(f"Keep the name under {MAX_NAME_LENGTH} characters.")
    params = normalize_search_params(raw_params)
    if not set(params) - _ORDER_ONLY:
        raise SavedSearchError("Set a search term or at least one filter before saving.")
    return _Clean(name, params)


async def _name_taken(db: AsyncSession, user_id: int, name: str, *, exclude_id: int | None) -> bool:
    stmt = select(SavedSearch.id).where(
        SavedSearch.user_id == user_id,
        func.lower(SavedSearch.name) == name.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(SavedSearch.id != exclude_id)
    return (await db.scalar(stmt.limit(1))) is not None


async def _flush(db: AsyncSession) -> None:
    """Flush, turning a lost race on the unique name into the same error the check
    above gives. The session is unusable after that; the caller rolls back."""
    try:
        await db.flush()
    except IntegrityError as exc:
        raise SavedSearchError("You already have a saved search with that name.") from exc


async def list_saved_searches(db: AsyncSession, user_id: int) -> list[SavedSearch]:
    """The user's saved searches, alphabetically (case-insensitive)."""
    stmt = (
        select(SavedSearch)
        .where(SavedSearch.user_id == user_id)
        .order_by(func.lower(SavedSearch.name), SavedSearch.id)
    )
    return list((await db.scalars(stmt)).all())


async def get_saved_search(db: AsyncSession, user_id: int, search_id: int) -> SavedSearch | None:
    """One of the user's saved searches, or None. Another user's id is None too."""
    return await db.scalar(
        select(SavedSearch).where(SavedSearch.id == search_id, SavedSearch.user_id == user_id)
    )


async def create_saved_search(
    db: AsyncSession, user_id: int, *, name: str, params: Mapping[str, Any],
) -> SavedSearch:
    """Save a new search. Raises SavedSearchError for anything the reader can fix."""
    clean = _clean(name, params)
    count = await db.scalar(
        select(func.count()).select_from(SavedSearch).where(SavedSearch.user_id == user_id)
    )
    if count >= MAX_SAVED_SEARCHES:
        raise SavedSearchError(
            f"You can keep up to {MAX_SAVED_SEARCHES} saved searches. Delete one to save another."
        )
    if await _name_taken(db, user_id, clean.name, exclude_id=None):
        raise SavedSearchError("You already have a saved search with that name.")
    search = SavedSearch(user_id=user_id, name=clean.name, params=clean.params)
    db.add(search)
    await _flush(db)
    return search


TOP_PICKS_NAME = "Top picks"
# The best-scored articles of the last week, best first: the score the list shows (AI,
# else basic) at 60 or more, which on production data let through about one article
# in eight. No status of its own, so it follows the reader's unread setting.
TOP_PICKS_PARAMS = {
    "score_source": "relevance", "score_op": "gte", "score_val": 60,
    "since_days": 7, "sort": "score",
}


async def create_top_picks(db: AsyncSession, user_id: int) -> None:
    """The saved search a new account gets with its first term list, on /welcome.

    Only there: it answers the question the reader just answered, and doing it once
    per account needs no flag, since /welcome runs once. A reader who deletes it
    does not get it back, and one who skipped the question can save it from a search.
    """
    if await _name_taken(db, user_id, TOP_PICKS_NAME, exclude_id=None):
        return
    # In a savepoint: a second /welcome submitted at the same moment loses the race
    # on the unique name, and that must not fail the reader's welcome.
    try:
        async with db.begin_nested():
            await create_saved_search(db, user_id, name=TOP_PICKS_NAME, params=TOP_PICKS_PARAMS)
    except SavedSearchError:
        pass


async def update_saved_search(
    db: AsyncSession, search: SavedSearch, *, name: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> SavedSearch:
    """Change a saved search in place. Anything left None stays as it is."""
    clean = _clean(
        search.name if name is None else name,
        search.params if params is None else params,
    )
    if clean.name.lower() != search.name.lower() and await _name_taken(
        db, search.user_id, clean.name, exclude_id=search.id,
    ):
        raise SavedSearchError("You already have a saved search with that name.")
    search.name = clean.name
    search.params = clean.params
    await _flush(db)
    return search


async def delete_saved_search(db: AsyncSession, search: SavedSearch) -> None:
    await db.delete(search)
    await db.flush()


async def strip_saved_search_references(
    db: AsyncSession, *, kind: str, ref_id: int, user_id: int | None,
) -> list[str]:
    """Drop a deleted feed, folder or label from saved searches that name it.

    ``kind`` is ``"feed"``, ``"folder"`` or ``"label"``. Feeds and folders live in
    ``scope_include``, labels in ``label_filter``. ``user_id`` None cleans every
    user's searches (an admin deleting a shared feed).

    When it is the last one in its list, the reference stays. Removing it would
    widen the search to every feed or label, and the sidebar would then mark
    articles read that the search was never about; deleting the search would throw
    away its query and other filters. Left in, it matches nothing, so the search
    comes up empty until the reader changes it (the edit form says why), and a
    feed subscribed to again comes back under the same id and revives it. The names
    of such searches are returned so the caller can say so.
    Mutates ORM objects; the caller owns the commit.
    """
    key = "label_filter" if kind == "label" else "scope_include"
    token = f"{kind}:{ref_id}"
    stmt = select(SavedSearch).where(SavedSearch.params.contains({key: [token]}))
    if user_id is not None:
        stmt = stmt.where(SavedSearch.user_id == user_id)

    emptied: list[str] = []
    for search in (await db.execute(stmt)).scalars():
        values = search.params.get(key)
        if not isinstance(values, list) or token not in values:
            continue
        remaining = [v for v in values if v != token]
        if not remaining:
            emptied.append(search.name)
            continue
        # A new dict, so the JSONB change is seen.
        search.params = {**search.params, key: remaining}
    return emptied


async def has_missing_references(db: AsyncSession, user_id: int, params: Mapping[str, Any]) -> bool:
    """Whether the search names a feed, folder or label the user no longer has,
    which `strip_saved_search_references` leaves in place when it is the last one.
    ``folder:0`` (no folder) always exists."""
    feed_ids, folder_ids = parse_scope_tokens(json.dumps(params.get("scope_include") or []))
    _, label_ids = parse_label_tokens(json.dumps(params.get("label_filter") or []))
    folder_ids = [i for i in folder_ids if i]
    checks = (
        (UserFeed.feed_id, UserFeed, feed_ids),
        (Folder.id, Folder, folder_ids),
        (Label.id, Label, label_ids),
    )
    for column, model, ids in checks:
        if not ids:
            continue
        found = set((await db.scalars(
            select(column).where(model.user_id == user_id, column.in_(ids))
        )).all())
        if set(ids) - found:
            return True
    return False


def view_filters(params: Mapping[str, Any]) -> dict:
    """A stored parameter set as ``list_articles`` filters, without the sort: what
    the saved search lists, for everything that has to cover exactly that."""
    kw = list_kwargs(params)
    return dict(
        q=kw["q"], read_status=kw["read_status"],
        scope_include=kw["scope_include"], label_filter=kw["label_filter"],
        **kw["score"], since_days=kw["since_days"],
        starred_only=kw["state"] == "starred", archived_only=kw["state"] == "archived",
        saved_only=kw["state"] == "saved", search=True,
    )


async def count_saved_search(
    db: AsyncSession, user: User, search: SavedSearch, *, collapsing: bool,
    budget_ms: int = COUNT_BUDGET_MS,
) -> tuple[int, int | None] | None:
    """The sidebar badge of a saved search: ``(unread, total)``, or None when
    counting took longer than ``budget_ms``.

    Counted as the list draws it (``collapsing``, see ``story_service.row_count``).
    Like the other badges it shows the unread rows when there are some and the grey
    total otherwise, so the total is only counted when nothing is unread (and is
    None when it was not needed).

    Most searches count in a few milliseconds, but what a saved search asks is up to
    the reader, and some shapes cannot use an index (any label over thousands of
    labelled articles took 114 ms on production data). Rather than guess from the
    parameters, each count gets a statement timeout and a search that runs out of it
    shows no number. It is inside a savepoint, so the timeout goes away with it and
    a cancelled statement leaves the session usable.
    """
    filters = view_filters(search.params)
    try:
        async with db.begin_nested():
            await db.execute(text(f"SET LOCAL statement_timeout = {int(budget_ms)}"))
            unread = await count_articles(
                user, db, collapsing=collapsing, **{**filters, "unread_only": True},
            )
            total = None if unread else await count_articles(
                user, db, collapsing=collapsing, **filters,
            )
            await db.execute(text("SET LOCAL statement_timeout = DEFAULT"))
    except DBAPIError as exc:
        if sqlstate(exc) != _QUERY_CANCELED:
            raise
        return None
    return unread, total


async def mark_saved_search_read(
    db: AsyncSession, user: User, search: SavedSearch, *, before: datetime,
) -> None:
    """Mark read what the saved search shows, fetched up to ``before``.

    The articles come from the list's own query (``list_articles(_ids=True)``) with
    the stored parameters, so this marks exactly what the view lists: nothing
    outside it, and of a folded story only the members that match the search, not
    the whole group. Stamped ``suppressed_by='bulk'`` like the sidebar's other
    mark-all-read (see ``mark_scope_read``). The caller owns the commit.
    """
    ids = (await list_articles(
        user, db, **view_filters(search.params), _ids=True,
    )).where(Article.fetched_at <= before).subquery()
    now = datetime.now(timezone.utc)
    stmt = pg_insert(UserArticleState).from_select(
        ["user_id", "article_id", "is_read", "is_starred", "is_archived", "read_at",
         "suppressed_at", "suppressed_by"],
        select(
            literal(user.id), ids.c.id, literal(True), literal(False), literal(False),
            literal(now), literal(now), literal(SUPPRESSED_BY_BULK),
        ),
    ).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        set_={"is_read": True, "read_at": now, "suppressed_at": now, "suppressed_by": SUPPRESSED_BY_BULK},
        where=(UserArticleState.__table__.c.is_read == False),  # noqa: E712
    )
    await db.execute(stmt)
