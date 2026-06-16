"""API tests for /api/v1/labels and /api/v1/articles/{id}/labels."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest


def _make_label_response(id=1, name="Tech", color="#6366f1", position=0):
    return SimpleNamespace(
        id=id,
        name=name,
        color=color,
        position=position,
        created_at=datetime(2024, 1, 1),
    )


class TestListLabels:
    def test_returns_list(self, client):
        labels = [_make_label_response(id=1), _make_label_response(id=2, name="News")]
        with patch("app.routers.api.v1.labels.list_labels", new=AsyncMock(return_value=labels)):
            response = client.get("/api/v1/labels")
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_empty_list(self, client):
        with patch("app.routers.api.v1.labels.list_labels", new=AsyncMock(return_value=[])):
            response = client.get("/api/v1/labels")
        assert response.status_code == 200
        assert response.json() == []

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/labels")
        assert response.status_code == 401


class TestCreateLabel:
    def test_creates_label(self, client):
        label = _make_label_response(name="Sports")
        with patch("app.routers.api.v1.labels.create_label", new=AsyncMock(return_value=label)):
            response = client.post(
                "/api/v1/labels",
                json={"name": "Sports", "color": "#ff0000"},
            )
        assert response.status_code == 201
        assert response.json()["name"] == "Sports"

    def test_duplicate_name_returns_409(self, client):
        from app.services.label_service import LabelAlreadyExistsError
        with patch(
            "app.routers.api.v1.labels.create_label",
            new=AsyncMock(side_effect=LabelAlreadyExistsError("Sports")),
        ):
            response = client.post(
                "/api/v1/labels",
                json={"name": "Sports", "color": "#ff0000"},
            )
        assert response.status_code == 409

    def test_invalid_color_returns_422(self, client):
        response = client.post("/api/v1/labels", json={"name": "Tech", "color": "red"})
        assert response.status_code == 422

    def test_missing_name_returns_422(self, client):
        response = client.post("/api/v1/labels", json={"color": "#ff0000"})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/labels", json={"name": "Tech"})
        assert response.status_code == 401


class TestUpdateLabel:
    def test_updates_label(self, client):
        label = _make_label_response(name="Updated")
        with patch("app.routers.api.v1.labels.update_label", new=AsyncMock(return_value=label)):
            response = client.patch("/api/v1/labels/1", json={"name": "Updated"})
        assert response.status_code == 200
        assert response.json()["name"] == "Updated"

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.labels.update_label", new=AsyncMock(return_value=None)):
            response = client.patch("/api/v1/labels/99", json={"name": "X"})
        assert response.status_code == 404

    def test_invalid_color_returns_422(self, client):
        response = client.patch("/api/v1/labels/1", json={"color": "invalid"})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.patch("/api/v1/labels/1", json={"name": "X"})
        assert response.status_code == 401


class TestDeleteLabel:
    def test_deletes_successfully(self, client):
        with patch("app.routers.api.v1.labels.delete_label", new=AsyncMock(return_value=True)):
            response = client.delete("/api/v1/labels/1")
        assert response.status_code == 204

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.labels.delete_label", new=AsyncMock(return_value=False)):
            response = client.delete("/api/v1/labels/99")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.delete("/api/v1/labels/1")
        assert response.status_code == 401


class TestAssignLabel:
    def test_assigns_successfully(self, client):
        with patch("app.routers.api.v1.labels.assign_label", new=AsyncMock(return_value=True)):
            response = client.post("/api/v1/articles/1/labels", json={"label_id": 1})
        assert response.status_code == 204

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.labels.assign_label", new=AsyncMock(return_value=False)):
            response = client.post("/api/v1/articles/1/labels", json={"label_id": 99})
        assert response.status_code == 404

    def test_missing_label_id_returns_422(self, client):
        response = client.post("/api/v1/articles/1/labels", json={})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/articles/1/labels", json={"label_id": 1})
        assert response.status_code == 401


class TestRemoveLabel:
    def test_removes_successfully(self, client):
        with patch("app.routers.api.v1.labels.remove_label", new=AsyncMock(return_value=True)):
            response = client.delete("/api/v1/articles/1/labels/1")
        assert response.status_code == 204

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.labels.remove_label", new=AsyncMock(return_value=False)):
            response = client.delete("/api/v1/articles/1/labels/1")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.delete("/api/v1/articles/1/labels/1")
        assert response.status_code == 401
