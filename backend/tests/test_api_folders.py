"""API tests for /api/v1/folders.

The routes go through folder_service, which looks folders up with ``db.scalar``
(the folder by id, a clashing name's id, the highest position) and leaves
``db.execute`` to the listing and the scope cleanup on delete.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_folder(id=1, name="Tech", position=0):
    return SimpleNamespace(
        id=id,
        name=name,
        position=position,
        user_id=1,
        created_at=datetime(2024, 1, 1),
    )


def _scalar_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalars.return_value.all.return_value = value if isinstance(value, list) else []
    return r


class TestListFolders:
    def test_returns_empty_list(self, client, mock_db):
        mock_db.execute.return_value = _scalar_result([])
        response = client.get("/api/v1/folders")
        assert response.status_code == 200
        assert response.json() == []

    def test_returns_folders(self, client, mock_db):
        folders = [_make_folder(id=1, name="Tech"), _make_folder(id=2, name="News")]
        mock_db.execute.return_value = _scalar_result(folders)
        response = client.get("/api/v1/folders")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "Tech"

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/folders")
        assert response.status_code == 401


class TestCreateFolder:
    def test_duplicate_name_returns_409(self, client, mock_db):
        mock_db.scalar.side_effect = [7]  # id of the folder already called "Tech"

        response = client.post("/api/v1/folders", json={"name": "Tech"})
        assert response.status_code == 409
        mock_db.add.assert_not_called()

    def test_too_long_name_returns_422(self, client, mock_db):
        response = client.post("/api/v1/folders", json={"name": "x" * 101})
        assert response.status_code == 422

    def test_empty_name_returns_422(self, client, mock_db):
        response = client.post("/api/v1/folders", json={"name": "   "})
        assert response.status_code == 422

    def test_missing_name_returns_422(self, client, mock_db):
        response = client.post("/api/v1/folders", json={})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/folders", json={"name": "Tech"})
        assert response.status_code == 401

    def test_creates_folder_successfully(self, client, mock_db):
        mock_db.scalar.side_effect = [None, 3]  # name free, highest position 3

        async def set_id(obj):
            obj.id = 10
            obj.created_at = datetime(2024, 1, 1)

        mock_db.refresh.side_effect = set_id

        response = client.post("/api/v1/folders", json={"name": "New Folder"})
        assert response.status_code == 201
        mock_db.add.assert_called_once()
        mock_db.commit.assert_awaited_once()
        assert mock_db.add.call_args.args[0].position == 4


class TestUpdateFolder:
    def test_not_found_returns_404(self, client, mock_db):
        mock_db.scalar.side_effect = [None]
        response = client.patch("/api/v1/folders/99", json={"name": "New"})
        assert response.status_code == 404

    def test_duplicate_name_returns_409(self, client, mock_db):
        folder = _make_folder(id=1, name="Tech")
        # The folder by id, then the id of the other folder already called "Science".
        mock_db.scalar.side_effect = [folder, 2]
        response = client.patch("/api/v1/folders/1", json={"name": "Science"})
        assert response.status_code == 409
        assert folder.name == "Tech"

    def test_requires_auth(self, unauth_client):
        response = unauth_client.patch("/api/v1/folders/1", json={"name": "X"})
        assert response.status_code == 401

    def test_updates_name_successfully(self, client, mock_db):
        folder = _make_folder(id=1, name="Old")
        mock_db.scalar.side_effect = [folder, None]  # the folder, no clashing name

        async def set_attrs(obj):
            pass

        mock_db.refresh.side_effect = set_attrs
        response = client.patch("/api/v1/folders/1", json={"name": "New"})
        assert response.status_code == 200
        assert folder.name == "New"


class TestDeleteFolder:
    def test_not_found_returns_404(self, client, mock_db):
        mock_db.scalar.side_effect = [None]
        response = client.delete("/api/v1/folders/99")
        assert response.status_code == 404

    def test_deletes_successfully(self, client, mock_db):
        folder = _make_folder()
        mock_db.scalar.side_effect = [folder]
        mock_db.execute.return_value = _scalar_result([])  # no scopes to clean up
        response = client.delete("/api/v1/folders/1")
        assert response.status_code == 204
        mock_db.delete.assert_awaited_once_with(folder)

    def test_requires_auth(self, unauth_client):
        response = unauth_client.delete("/api/v1/folders/1")
        assert response.status_code == 401
