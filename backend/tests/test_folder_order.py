"""Folder ordering: moving a folder past hidden neighbours, and switching modes.

The interesting part is that folders with no feeds are rendered nowhere, so the
order the user sees skips them while the stored order does not.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.folder_service import (
    move_folder, next_folder_position, reset_folder_order, set_folder_order,
)


def _settings(arranged=False):
    return SimpleNamespace(user_id=1, folder_order="custom", folders_arranged=arranged)


def _folder(id, name, position):
    return SimpleNamespace(id=id, name=name, position=position, user_id=1)


def _db(folders, folder_ids_with_feeds):
    """A db whose first query returns the ordered folders, second the filled ids."""
    ordered = MagicMock()
    ordered.scalars.return_value.all.return_value = list(folders)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[ordered, [(fid,) for fid in folder_ids_with_feeds]])
    return db


def _order(folders):
    return [(f.name, f.position) for f in sorted(folders, key=lambda f: f.position)]


class TestMoveFolder:
    @pytest.mark.asyncio
    async def test_up_clears_an_empty_folder_in_one_step(self):
        """A, B (empty), C: moving C up has to land above A.

        Swapping with the neighbouring position would put C where B sits, which
        changes nothing on screen and makes the click look broken.
        """
        a, b, c = _folder(1, "A", 1), _folder(2, "B", 2), _folder(3, "C", 3)
        db = _db([a, b, c], {1, 3})

        assert await move_folder(db, _settings(), folder_id=3, direction="up") is True

        assert _order([a, b, c]) == [("C", 1), ("A", 2), ("B", 3)]
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_down_clears_an_empty_folder_in_one_step(self):
        a, b, c = _folder(1, "A", 1), _folder(2, "B", 2), _folder(3, "C", 3)
        db = _db([a, b, c], {1, 3})

        assert await move_folder(db, _settings(), folder_id=1, direction="down") is True

        assert _order([a, b, c]) == [("B", 1), ("C", 2), ("A", 3)]

    @pytest.mark.asyncio
    async def test_empty_folder_keeps_its_place_between_visible_ones(self):
        """B stays between A and D, so filling it later shows it there."""
        a, b, c, d = (
            _folder(1, "A", 1), _folder(2, "B", 2), _folder(3, "C", 3), _folder(4, "D", 4),
        )
        db = _db([a, b, c, d], {1, 3, 4})

        assert await move_folder(db, _settings(), folder_id=4, direction="up") is True

        assert _order([a, b, c, d]) == [("A", 1), ("B", 2), ("D", 3), ("C", 4)]

    @pytest.mark.asyncio
    async def test_first_visible_folder_cannot_move_up(self):
        a, b = _folder(1, "A", 1), _folder(2, "B", 2)
        db = _db([a, b], {1, 2})

        assert await move_folder(db, _settings(), folder_id=1, direction="up") is False

        assert _order([a, b]) == [("A", 1), ("B", 2)]
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trailing_empty_folders_do_not_count_as_a_step_down(self):
        """Nothing visible below A, so the arrow stays a no-op rather than
        burying A under folders the user cannot see."""
        a, b = _folder(1, "A", 1), _folder(2, "B", 2)
        db = _db([a, b], {1})

        assert await move_folder(db, _settings(), folder_id=1, direction="down") is False
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_folder_is_not_moved(self):
        a = _folder(1, "A", 1)
        db = _db([a], {1})

        assert await move_folder(db, _settings(), folder_id=999, direction="up") is False
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_move_renumbers_gaps_and_duplicates(self):
        """Deleted folders leave gaps and the API can set the same position
        twice; every move rewrites the whole list, so neither accumulates."""
        a, b, c = _folder(1, "A", 4), _folder(2, "B", 4), _folder(3, "C", 9)
        db = _db([a, b, c], {1, 2, 3})

        assert await move_folder(db, _settings(), folder_id=3, direction="up") is True

        assert _order([a, b, c]) == [("A", 1), ("C", 2), ("B", 3)]

    @pytest.mark.asyncio
    async def test_a_move_marks_the_order_as_arranged(self):
        """What stops a later switch of views from seeding over the arrangement."""
        a, b = _folder(1, "A", 1), _folder(2, "B", 2)
        db = _db([a, b], {1, 2})
        settings = _settings(arranged=False)

        assert await move_folder(db, settings, folder_id=2, direction="up") is True
        assert settings.folders_arranged is True

    @pytest.mark.asyncio
    async def test_a_move_that_does_nothing_does_not_mark_it(self):
        a, b = _folder(1, "A", 1), _folder(2, "B", 2)
        db = _db([a, b], {1, 2})
        settings = _settings(arranged=False)

        assert await move_folder(db, settings, folder_id=1, direction="up") is False
        assert settings.folders_arranged is False

    @pytest.mark.asyncio
    async def test_moving_while_sorted_alphabetically_raises(self):
        """Positions decide nothing in that mode, so the move would rewrite them
        with nothing to show for it. The arrows are not rendered there."""
        db = AsyncMock()
        settings = SimpleNamespace(user_id=1, folder_order="name", folders_arranged=True)
        with pytest.raises(ValueError):
            await move_folder(db, settings, folder_id=1, direction="up")
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_direction_raises(self):
        db = AsyncMock()
        with pytest.raises(ValueError):
            await move_folder(db, _settings(), folder_id=1, direction="sideways")
        db.execute.assert_not_awaited()


def _alphabetical_db(folders):
    """A db whose only query returns the folders in alphabetical order."""
    ordered = MagicMock()
    ordered.scalars.return_value.all.return_value = sorted(folders, key=lambda f: f.name.lower())
    db = AsyncMock()
    db.execute = AsyncMock(return_value=ordered)
    return db


class TestSetFolderOrder:
    @pytest.mark.asyncio
    async def test_first_switch_to_custom_seeds_from_the_alphabetical_view(self):
        """Stored positions follow creation order on a new account, so without
        seeding the list would jump the first time the user switches."""
        third, first, second = _folder(1, "Zulu", 1), _folder(2, "Alpha", 2), _folder(3, "Beta", 3)
        db = _alphabetical_db([third, first, second])
        settings = SimpleNamespace(user_id=1, folder_order="name", folders_arranged=False)

        await set_folder_order(db, settings, "custom")

        assert settings.folder_order == "custom"
        assert _order([first, second, third]) == [("Alpha", 1), ("Beta", 2), ("Zulu", 3)]
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_switch_to_custom_leaves_an_arrangement_alone(self):
        a, b = _folder(1, "A", 2), _folder(2, "B", 1)
        db = AsyncMock()
        settings = SimpleNamespace(user_id=1, folder_order="name", folders_arranged=True)

        await set_folder_order(db, settings, "custom")

        assert _order([a, b]) == [("B", 1), ("A", 2)]
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arrangement_survives_a_round_trip_through_alphabetical(self):
        """Alphabetical is a view of the order, not a reset of it. Looking at it
        must not cost the user the arrangement they made by hand."""
        a, b, c = _folder(1, "A", 3), _folder(2, "B", 1), _folder(3, "C", 2)
        settings = SimpleNamespace(user_id=1, folder_order="custom", folders_arranged=True)

        for mode in ("name", "custom"):
            db = AsyncMock()
            await set_folder_order(db, settings, mode)
            db.execute.assert_not_awaited()

        assert settings.folder_order == "custom"
        assert _order([a, b, c]) == [("B", 1), ("C", 2), ("A", 3)]

    @pytest.mark.asyncio
    async def test_switching_to_alphabetical_never_seeds(self):
        a, b = _folder(1, "A", 2), _folder(2, "B", 1)
        db = AsyncMock()
        settings = SimpleNamespace(user_id=1, folder_order="custom", folders_arranged=False)

        await set_folder_order(db, settings, "name")

        assert _order([a, b]) == [("B", 1), ("A", 2)]
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_mode_raises(self):
        db = AsyncMock()
        settings = SimpleNamespace(user_id=1, folder_order="name")
        with pytest.raises(ValueError):
            await set_folder_order(db, settings, "by-unread")
        assert settings.folder_order == "name"


class TestResetFolderOrder:
    @pytest.mark.asyncio
    async def test_reset_restores_alphabetical_and_clears_the_flag(self):
        a, b, c = _folder(1, "Zulu", 1), _folder(2, "Alpha", 2), _folder(3, "Beta", 3)
        db = _alphabetical_db([a, b, c])
        settings = _settings(arranged=True)

        await reset_folder_order(db, settings)

        assert _order([a, b, c]) == [("Alpha", 1), ("Beta", 2), ("Zulu", 3)]
        assert settings.folders_arranged is False
        db.commit.assert_awaited_once()


class TestNextFolderPosition:
    @pytest.mark.asyncio
    async def test_appends_after_the_highest(self):
        db = AsyncMock()
        db.scalar = AsyncMock(return_value=7)
        assert await next_folder_position(db, 1) == 8

    @pytest.mark.asyncio
    async def test_first_folder_starts_at_one(self):
        db = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        assert await next_folder_position(db, 1) == 1
