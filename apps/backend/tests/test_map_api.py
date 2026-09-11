from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.map_api import (
    MapNotFoundError,
    clip,
    layer_catalog,
    read_layer,
    replay,
)
from backend.map_schemas import ClipRequest, ReplayRequest


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
        self.replay_request: ReplayRequest | None = None
        self.clip_request: ClipRequest | None = None
        self.error: Exception | None = None

    def layer_catalog(self) -> dict[str, Any]:
        return {
            "crs": "EPSG:4326",
            "layers": [{"id": "resolved_subtrack_points"}],
            "default_context": {"detect_id": "detect-latest", "fit_id": None},
        }

    def read_layer(self, layer_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.layer_calls.append({"layer_id": layer_id, **kwargs})
        return feature_collection(layer=layer_id, detect_id=kwargs["detect_id"])

    def replay(self, request: ReplayRequest) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.replay_request = request
        return feature_collection(operation="replay")

    def clip(self, request: ClipRequest) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.clip_request = request
        return feature_collection(operation="clip")


def test_layer_catalog_exposes_crs_and_default_context() -> None:
    result = layer_catalog(store=FakeMapStore())
    assert result["crs"] == "EPSG:4326"
    assert result["default_context"]["detect_id"] == "detect-latest"


def test_layer_query_parses_bbox_context_and_limit() -> None:
    store = FakeMapStore()
    detect_id = UUID("12345678-1234-5678-1234-567812345678")

    result = read_layer(
        "intersections",
        bbox="120.1,30.2,120.3,30.4",
        detect_id=detect_id,
        fit_id=None,
        limit=25,
        store=store,
    )

    assert store.layer_calls == [{
        "layer_id": "intersections",
        "bbox": (120.1, 30.2, 120.3, 30.4),
        "detect_id": str(detect_id),
        "fit_id": None,
        "limit": 25,
    }]
    assert result["metadata"]["crs"] == "EPSG:4326"


@pytest.mark.parametrize(
    "bbox",
    ["120,30,121", "west,30,121,31", "121,30,120,31", "120,-91,121,31"],
)
def test_layer_query_rejects_invalid_bbox(bbox: str) -> None:
    store = FakeMapStore()
    with pytest.raises(HTTPException) as caught:
        read_layer(
            "resolved_subtrack_points",
            bbox=bbox,
            detect_id=None,
            fit_id=None,
            limit=25,
            store=store,
        )
    assert caught.value.status_code == 422
    assert store.layer_calls == []


def test_replay_accepts_external_and_internal_id_namespaces() -> None:
    store = FakeMapStore()
    source_request = ReplayRequest(source_row_keys=["external-7", "9"])
    replay(source_request, store=store)
    assert store.replay_request == source_request

    with pytest.raises(ValidationError, match="resolved_point_ids requires detect_id"):
        ReplayRequest(resolved_point_ids=[7, 9])

    resolved_request = ReplayRequest(
        detect_id=UUID("12345678-1234-5678-1234-567812345678"),
        resolved_point_ids=[7, 9],
    )
    replay(resolved_request, store=store)
    assert store.replay_request == resolved_request


def test_replay_requires_exactly_one_selection() -> None:
    with pytest.raises(ValidationError, match="provide exactly one"):
        ReplayRequest()
    with pytest.raises(ValidationError, match="provide exactly one"):
        ReplayRequest(source_row_keys=["7"], bbox=(120, 30, 121, 31))


@pytest.mark.parametrize(
    "intersection_ids",
    [[], [1], [1, 1], [0, 1], [-1, 2], [1, 2, 3]],
)
def test_clip_requires_exactly_two_distinct_intersections(
    intersection_ids: list[int],
) -> None:
    with pytest.raises(ValidationError):
        ClipRequest(
            detect_id=UUID("12345678-1234-5678-1234-567812345678"),
            intersection_ids=intersection_ids,
        )


def test_clip_forwards_valid_request() -> None:
    store = FakeMapStore()
    request = ClipRequest(
        detect_id=UUID("12345678-1234-5678-1234-567812345678"),
        intersection_ids=[1, 2],
    )
    clip(request, store=store)
    assert store.clip_request == request


def test_store_not_found_is_an_http_404() -> None:
    store = FakeMapStore()
    store.error = MapNotFoundError("no completed Detect Run is available")
    with pytest.raises(HTTPException) as caught:
        read_layer(
            "intersections",
            bbox=None,
            detect_id=None,
            fit_id=None,
            limit=25,
            store=store,
        )
    assert caught.value.status_code == 404
    assert caught.value.detail == "no completed Detect Run is available"
