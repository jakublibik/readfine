"""API tests for /api/v1/filters."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest


def _make_filter_response(id=1, name="My Filter"):
    return SimpleNamespace(
        id=id,
        name=name,
        is_active=True,
        match_operator="AND",
        position=0,
        stop_on_match=False,
        scope_include=[],
        scope_except=[],
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
        conditions=[],
        actions=[],
    )


_VALID_FILTER_PAYLOAD = {
    "name": "Test Filter",
    "conditions": [{"field": "title", "operator": "contains", "value": "python"}],
    "actions": [{"action_type": "mark_read"}],
}


class TestListFilters:
    def test_returns_list(self, client):
        filters = [_make_filter_response(id=1), _make_filter_response(id=2, name="Filter 2")]
        with patch("app.routers.api.v1.filters.list_filters", new=AsyncMock(return_value=filters)):
            response = client.get("/api/v1/filters")
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_empty_list(self, client):
        with patch("app.routers.api.v1.filters.list_filters", new=AsyncMock(return_value=[])):
            response = client.get("/api/v1/filters")
        assert response.status_code == 200
        assert response.json() == []

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/filters")
        assert response.status_code == 401


class TestCreateFilter:
    def test_creates_filter(self, client):
        f = _make_filter_response(name="Test Filter")
        with patch("app.routers.api.v1.filters.create_filter", new=AsyncMock(return_value=f)):
            response = client.post("/api/v1/filters", json=_VALID_FILTER_PAYLOAD)
        assert response.status_code == 201

    def test_invalid_payload_raises_422(self, client):
        with patch("app.routers.api.v1.filters.create_filter", side_effect=ValueError("bad")):
            response = client.post("/api/v1/filters", json=_VALID_FILTER_PAYLOAD)
        assert response.status_code == 422

    def test_missing_name_returns_422(self, client):
        payload = {k: v for k, v in _VALID_FILTER_PAYLOAD.items() if k != "name"}
        response = client.post("/api/v1/filters", json=payload)
        assert response.status_code == 422

    def test_invalid_operator_returns_422(self, client):
        payload = {**_VALID_FILTER_PAYLOAD, "conditions": [
            {"field": "title", "operator": "INVALID", "value": "x"}
        ]}
        response = client.post("/api/v1/filters", json=payload)
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/filters", json=_VALID_FILTER_PAYLOAD)
        assert response.status_code == 401


class TestGetFilter:
    def test_returns_filter(self, client):
        f = _make_filter_response()
        with patch("app.routers.api.v1.filters.get_filter", new=AsyncMock(return_value=f)):
            response = client.get("/api/v1/filters/1")
        assert response.status_code == 200
        assert response.json()["id"] == 1

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.filters.get_filter", new=AsyncMock(return_value=None)):
            response = client.get("/api/v1/filters/99")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.get("/api/v1/filters/1")
        assert response.status_code == 401


class TestUpdateFilter:
    def test_updates_filter(self, client):
        f = _make_filter_response(name="Renamed")
        with patch("app.routers.api.v1.filters.update_filter", new=AsyncMock(return_value=f)):
            response = client.patch("/api/v1/filters/1", json={"name": "Renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.filters.update_filter", new=AsyncMock(return_value=None)):
            response = client.patch("/api/v1/filters/99", json={"name": "X"})
        assert response.status_code == 404

    def test_invalid_payload_raises_422(self, client):
        with patch("app.routers.api.v1.filters.update_filter", side_effect=ValueError("bad")):
            response = client.patch("/api/v1/filters/1", json={"name": "X"})
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client):
        response = unauth_client.patch("/api/v1/filters/1", json={"name": "X"})
        assert response.status_code == 401


class TestDeleteFilter:
    def test_deletes_successfully(self, client):
        with patch("app.routers.api.v1.filters.delete_filter", new=AsyncMock(return_value=True)):
            response = client.delete("/api/v1/filters/1")
        assert response.status_code == 204

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.filters.delete_filter", new=AsyncMock(return_value=False)):
            response = client.delete("/api/v1/filters/99")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.delete("/api/v1/filters/1")
        assert response.status_code == 401


class TestTestFilter:
    def test_returns_test_result(self, client):
        test_result = SimpleNamespace(matched_count=5, samples=[])
        with patch("app.routers.api.v1.filters.test_filter", new=AsyncMock(return_value=test_result)):
            response = client.post("/api/v1/filters/1/test")
        assert response.status_code == 200
        assert response.json()["matched_count"] == 5

    def test_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.filters.test_filter", new=AsyncMock(return_value=None)):
            response = client.post("/api/v1/filters/99/test")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/filters/1/test")
        assert response.status_code == 401


class TestApplyFilter:
    def test_returns_counts(self, client):
        with patch("app.routers.api.v1.filters.apply_filter_retroactively", new=AsyncMock(return_value=(10, 5, 3))):
            response = client.post("/api/v1/filters/1/apply")
        assert response.status_code == 200
        data = response.json()
        assert data["matched"] == 10
        assert data["changed"] == 5
        assert data["scoring_queued"] == 3

    def test_zero_counts_with_existing_filter(self, client):
        f = _make_filter_response()
        with patch("app.routers.api.v1.filters.apply_filter_retroactively", new=AsyncMock(return_value=(0, 0, 0))):
            with patch("app.routers.api.v1.filters.get_filter", new=AsyncMock(return_value=f)):
                response = client.post("/api/v1/filters/1/apply")
        assert response.status_code == 200

    def test_zero_counts_filter_not_found_returns_404(self, client):
        with patch("app.routers.api.v1.filters.apply_filter_retroactively", new=AsyncMock(return_value=(0, 0, 0))):
            with patch("app.routers.api.v1.filters.get_filter", new=AsyncMock(return_value=None)):
                response = client.post("/api/v1/filters/99/apply")
        assert response.status_code == 404

    def test_requires_auth(self, unauth_client):
        response = unauth_client.post("/api/v1/filters/1/apply")
        assert response.status_code == 401
