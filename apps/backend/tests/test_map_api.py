from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.map_api import MapNotFoundError, get_map_store


def feature_collection(**metadata: Any) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": [],
        "metadata": {"crs": "EPSG:4326", **metadata},
    }


class FakeMapStore:
    def __init__(self) -> None:
        self.layer_calls: list[dict[str, Any]] = []
        self.replay_request: Any = None
        self.clip_request: Any = None
        self.error: Exception | None = None

    def layer_catalog(self) -> dict[str, Any]:
        return {
            "crs": "EPSG:4326",
            "layers": [{"id": "intersections"}],
            "default_context": {"detect_id": "detect-latest", "fit_id": None},
        }

    def read_layer(self, layer_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.layer_calls.append({"layer_id": layer_id, **kwargs})
        return feature_collection(layer=layer_id, detect_id=kwargs["detect_id"])

    def replay(self, request: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.replay_request = request
        return feature_collection(operation="replay")

    def clip(self, request: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.clip_request = request
        return feature_collection(operation="clip")


@pytest.fixture
def client_and_store() -> Iterator[tuple[TestClient, FakeMapStore]]:
    store = FakeMapStore()
    app.dependency_overrides[get_map_store] = lambda: store
    try:
        yield TestClient(app), store
    finally:
        app.dependency_overrides.clear()


def test_layer_catalog_exposes_explicit_crs_and_default_context(
    client_and_store: tuple[TestClient, FakeMapStore],
) -> None:
    client, _ = client_and_store

    response = client.get("/api/v1/map/layers")

    assert response.status_code == 200
    assert response.json()["crs"] == "EPSG:4326"
    assert response.json()["default_context"]["detect_id"] == "detect-latest"


def test_layer_query_parses_bbox_context_and_limit(
    client_and_store: tuple[TestClient, FakeMapStore],
) -> None:
    client, store = client_and_store
    detect_id = "12345678-1234-5678-1234-567812345678"

    response = client.get(
        "/api/v1/map/layers/intersections",
        params={"bbox": "120.1,30.2,120.3,30.4", "detect_id": detect_id, "limit": 25},
    )

    assert response.status_code == 200
    assert store.layer_calls == [
        {
            "layer_id": "intersections",
            "bbox": (120.1, 30.2, 120.3, 30.4),
            "detect_id": detect_id,
            "fit_id": None,
            "limit": 25,
        }
    ]
    assert response.json()["metadata"]["crs"] == "EPSG:4326"


@pytest.mark.parametrize(
    "bbox",
    ["120,30,121", "west,30,121,31", "121,30,120,31", "120,-91,121,31"],
)
def test_layer_query_rejects_invalid_bbox(
    client_and_store: tuple[TestClient, FakeMapStore],
    bbox: str,
) -> None:
    client, store = client_and_store

    response = client.get("/api/v1/map/layers/trajectory_points", params={"bbox": bbox})

    assert response.status_code == 422
    assert store.layer_calls == []


def test_replay_accepts_exactly_one_selection(
    client_and_store: tuple[TestClient, FakeMapStore],
) -> None:
    client, store = client_and_store

    response = client.post("/api/v1/map/replay", json={"point_ids": [7, 9]})

    assert response.status_code == 200
    assert store.replay_request.point_ids == [7, 9]
    assert store.replay_request.bbox is None

    missing = client.post("/api/v1/map/replay", json={})
    both = client.post(
        "/api/v1/map/replay",
        json={"point_ids": [7], "bbox": [120, 30, 121, 31]},
    )
    assert missing.status_code == 422
    assert both.status_code == 422


@pytest.mark.parametrize(
    "intersection_ids",
    [[], [1], [1, 1], [0, 1], [-1, 2], [1, 2, 3]],
)
def test_clip_requires_exactly_two_distinct_intersections(
    client_and_store: tuple[TestClient, FakeMapStore],
    intersection_ids: list[int],
) -> None:
    client, _ = client_and_store

    response = client.post(
        "/api/v1/map/clip",
        json={
            "detect_id": "12345678-1234-5678-1234-567812345678",
            "intersection_ids": intersection_ids,
        },
    )

    assert response.status_code == 422


def test_store_not_found_is_an_http_404(
    client_and_store: tuple[TestClient, FakeMapStore],
) -> None:
    client, store = client_and_store
    store.error = MapNotFoundError("no completed Detect Run is available")

    response = client.get("/api/v1/map/layers/intersections")

    assert response.status_code == 404
    assert response.json() == {"detail": "no completed Detect Run is available"}
