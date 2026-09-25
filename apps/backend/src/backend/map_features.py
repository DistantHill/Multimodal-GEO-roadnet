"""GeoJSON feature construction shared by map layers and replay projections."""

from __future__ import annotations

from typing import Any


MAP_CRS = {"type": "name", "properties": {"name": "EPSG:4326"}}


def create_map_feature(
    feature_id: Any,
    geometry: dict[str, Any],
    **properties: Any,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": geometry,
        "properties": properties,
    }


def create_map_feature_collection(
    features: list[dict[str, Any]],
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "crs": MAP_CRS,
        "features": features,
        "metadata": {"crs": "EPSG:4326", **metadata},
    }
