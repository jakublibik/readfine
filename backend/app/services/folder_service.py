"""Folder ordering: alphabetical or an order the user arranges by hand.

``UserSettings.folder_order`` picks between the two modes and ``Folder.position``
carries the manual one. Positions stay dense (1..N per user) because every write
that touches them renumbers the whole list, so gaps left by deleted folders and
duplicates set through the API cannot pile up.

Sorting has to work on two shapes of query: a plain ``select(Folder)`` and the
outer join in ``list_user_feeds``, where a feed in no folder joins to NULL. The
same clause covers both - ``nulls_last()`` is a no-op on the first (the columns
are NOT NULL) and keeps "No folder" at the bottom on the second.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import Folder, UserFeed
from app.models.user import UserSettings

FOLDER_ORDER_MODES = ("name", "custom")
FOLDER_ORDER_DEFAULT = "name"
MOVE_DIRECTIONS = ("up", "down")


def folder_order_clause(mode: str):
    """ORDER BY criteria for folders in ``mode``, as a tuple to splat."""
    if mode == "custom":
        return (Folder.position.nulls_last(), func.lower(Folder.name).nulls_last())
    return (func.lower(Folder.name).nulls_last(),)


async def get_folder_order(db: AsyncSession, user_id: int) -> str:
    """The user's folder order mode, defaulting for a user with no settings row."""
    mode = await db.scalar(
        select(UserSettings.folder_order).where(UserSettings.user_id == user_id)
    )
    return mode if mode in FOLDER_ORDER_MODES else FOLDER_ORDER_DEFAULT


async def next_folder_position(db: AsyncSession, user_id: int) -> int:
    """Position for a new folder: the end of the list.

    Callers creating several folders in one go (OPML import) should take this
    once and count up themselves, rather than asking per folder.
    """
    highest = await db.scalar(
        select(func.max(Folder.position)).where(Folder.user_id == user_id)
    )
    return (highest or 0) + 1


async def folder_ids_with_feeds(db: AsyncSession, user_id: int) -> set[int]:
    """Folders the user actually sees. An empty folder is rendered nowhere."""
    rows = await db.execute(
        select(UserFeed.folder_id)
        .where(UserFeed.user_id == user_id, UserFeed.folder_id.is_not(None))
        .distinct()
    )
    return {row[0] for row in rows}


async def _ordered_folders(db: AsyncSession, user_id: int, mode: str) -> list[Folder]:
    result = await db.execute(
        select(Folder).where(Folder.user_id == user_id).order_by(*folder_order_clause(mode))
    )
    return list(result.scalars().all())


def _renumber(folders: list[Folder]) -> None:
    for index, folder in enumerate(folders, start=1):
        if folder.position != index:
            folder.position = index


async def set_folder_order(db: AsyncSession, settings: UserSettings, mode: str) -> None:
    """Switch the mode, committing the change.

    Turning the manual order on seeds it from the alphabetical order the user is
    looking at, but only until they have arranged anything: stored positions
    otherwise follow creation order (a new account) or lag behind renames and
    later additions (an upgraded one), and the list would jump for no reason
    they could see.

    Once they have moved a folder, the arrangement is data and alphabetical is a
    view of it, so switching between the views leaves positions alone in both
    directions. Otherwise a look at the alphabetical list would cost them the
    order they built by hand.
    """
    if mode not in FOLDER_ORDER_MODES:
        raise ValueError(f"Unknown folder order: {mode}")
    if mode == "custom" and not settings.folders_arranged:
        _renumber(await _ordered_folders(db, settings.user_id, "name"))
    settings.folder_order = mode
    await db.commit()


async def reset_folder_order(db: AsyncSession, settings: UserSettings) -> None:
    """Throw the arrangement away and go back to alphabetical positions.

    The way out for someone who wants to start over, and the reason switching
    views can stay instant: discarding an arrangement is a thing the user asks
    for on purpose, not a side effect of looking at the other view.
    """
    _renumber(await _ordered_folders(db, settings.user_id, "name"))
    settings.folders_arranged = False
    await db.commit()


async def move_folder(
    db: AsyncSession, settings: UserSettings, folder_id: int, direction: str
) -> bool:
    """Move a folder one step past the nearest folder that has feeds.

    The step has to clear empty folders in one go: they are rendered nowhere, so
    swapping with the neighbouring position would leave the visible order
    unchanged and the click would look like it did nothing. The folder is lifted
    out of the full order and re-inserted next to the nearest visible one, which
    leaves each empty folder where it was relative to the folder above it.

    A move that lands marks the order as arranged, which is what stops later
    view switching from seeding positions over it.

    Returns False when the folder is not the user's or already sits at the end it
    was asked to move towards.
    """
    if direction not in MOVE_DIRECTIONS:
        raise ValueError(f"Unknown direction: {direction}")
    if settings.folder_order != "custom":
        # Sorting alphabetically means positions decide nothing, so a move would
        # rewrite them with nothing to show for it. The arrows are not rendered in
        # that mode; a request that gets here anyway was not made by the UI.
        raise ValueError("Folders are sorted alphabetically")

    folders = await _ordered_folders(db, settings.user_id, "custom")
    index = next((i for i, f in enumerate(folders) if f.id == folder_id), None)
    if index is None:
        return False

    visible = await folder_ids_with_feeds(db, settings.user_id)
    if direction == "up":
        steps = range(index - 1, -1, -1)
    else:
        steps = range(index + 1, len(folders))
    target = next((i for i in steps if folders[i].id in visible), None)
    if target is None:
        return False

    # Moving down, popping first shifts the target left by one, so inserting at
    # `target` lands after it. Moving up, indices below `index` are untouched and
    # inserting at `target` lands before it. Both directions, one insert.
    moved = folders.pop(index)
    folders.insert(target, moved)
    _renumber(folders)
    settings.folders_arranged = True
    await db.commit()
    return True
