"""QGIS-independent request and response helpers for Roadnet Tools.

Keeping this module free of QGIS/PyQt imports makes the backend contract easy to
test in a normal Python environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID


DEFAULT_BACKEND_URL = "http://localhost:8000"
DEFAULT_REPLAY_PATH = "/api/v1/map/replay"
DEFAULT_CLIP_PATH = "/api/v1/map/clip"
DEFAULT_CRS = "EPSG:4326"


class ContractError(ValueError):
    """Raised when a selection or backend response violates the API contract."""


@dataclass(frozen=True)
class SelectionRecord:
    """Portable representation of one selected QGIS feature."""

    properties: Mapping[str, Any]
    feature_id: int | str


@dataclass(frozen=True)
class ParsedGeoJson:
    """Validated GeoJSON response plus display metadata."""

    feature_collection: dict[str, Any]
    crs: str
    metadata: Mapping[str, Any]


def normalize_backend_url(value: str) -> str:
    """Validate and normalize an HTTP(S) backend base URL."""

    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContractError("Backend URL must be an absolute http:// or https:// URL.")
    if parsed.query or parsed.fragment:
        raise ContractError("Backend URL must not contain a query string or fragment.")
    return url


def normalize_endpoint_path(value: str, label: str = "Endpoint") -> str:
    """Validate a path configured beneath the backend URL."""

    path = value.strip()
    if not path:
        raise ContractError(f"{label} path cannot be empty.")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ContractError(f"{label} must be a URL path without query parameters.")
    return "/" + path.lstrip("/")


def endpoint_url(base_url: str, endpoint_path: str) -> str:
    """Join a validated backend URL and endpoint path without urljoin surprises."""

    return normalize_backend_url(base_url) + normalize_endpoint_path(endpoint_path)


def build_replay_payload(records: Sequence[SelectionRecord], *, intersection_layer: bool = False, source_table: str | None = None) -> dict[str, Any]:
    """Build a typed replay body from selected database features."""

    if not records:
        raise ContractError("Select one or more trajectory points before Replay.")

    if intersection_layer or source_table == "intersections":
        ids = []
        for record in records:
            value = _first_present(record.properties, ("intersection_id", "id", "database_id"))
            if value is None:
                raise ContractError("交叉口图层缺少 intersections.id，无法回放。")
            _append_unique(ids, _integer_identifier(value, "intersection ID", 1))
        if len(ids) > 1000:
            raise ContractError("Replay accepts at most 1000 intersections.")
        payload = {"intersection_id": ids[0]} if len(ids) == 1 else {"intersection_ids": ids}
        detect_id = _common_property(records, "detect_id", required=False)
        if detect_id is not None:
            payload["detect_id"] = _uuid_identifier(detect_id)
        return payload

    selectors = {
        "tracks_cross": "tracks_cross_ids",
        "turn_events_geom": "turn_event_ids",
        "turn_events": "turn_event_ids",
        "injected_points": "injected_point_ids",
    }
    if source_table in selectors:
        ids = []
        for record in records:
            value = _first_present(record.properties, ("id", "database_id"))
            if value is None:
                raise ContractError("所选要素缺少数据库 id。")
            _append_unique(ids, _integer_identifier(value, "source ID", 1))
        if len(ids) > 1000:
            raise ContractError("Replay accepts at most 1000 selected IDs.")
        payload = {selectors[source_table]: ids}
        context = "refine_id" if source_table == "injected_points" else "detect_id"
        payload[context] = _uuid_identifier(_common_property(records, context, required=True))
        return payload

    resolved_point_ids: list[Any] = []
    source_row_keys: list[str] = []
    identity_kinds: set[str] = set()
    missing: list[str] = []
    for record in records:
        resolved_value = _first_present(record.properties, ("resolved_point_id",))
        source_value = _first_present(
            record.properties,
            ("source_row_key", "id", "database_id"),
        )
        if resolved_value is not None:
            identity_kinds.add("resolved")
            _append_unique(
                resolved_point_ids,
                _integer_identifier(resolved_value, "resolved point ID", 1),
            )
        elif source_value is not None:
            identity_kinds.add("source")
            _append_unique(source_row_keys, _source_row_key(source_value))
        else:
            missing.append(str(record.feature_id))

    if missing:
        shown = ", ".join(missing[:5])
        suffix = "..." if len(missing) > 5 else ""
        raise ContractError(
            "Selected trajectory features must contain 'resolved_point_id', "
            "'source_row_key', 'id', or 'database_id' "
            f"attribute (missing on feature IDs: {shown}{suffix})."
        )
    if len(identity_kinds) != 1:
        raise ContractError(
            "Replay selection cannot mix resolved point IDs with source row keys."
        )
    selected_count = len(resolved_point_ids) or len(source_row_keys)
    if selected_count > 1_000:
        raise ContractError("Replay accepts at most 1000 distinct selected point keys.")

    payload: dict[str, Any]
    common_detect_id = _common_property(records, "detect_id", required=False)
    if resolved_point_ids:
        if common_detect_id is None:
            raise ContractError(
                "Resolved point Replay requires one common non-null detect_id."
            )
        payload = {
            "detect_id": _uuid_identifier(common_detect_id),
            "resolved_point_ids": resolved_point_ids,
        }
    else:
        payload = {"source_row_keys": source_row_keys}
    if common_detect_id is not None and "detect_id" not in payload:
        payload["detect_id"] = _uuid_identifier(common_detect_id)
    return payload


def _source_row_key(value: Any) -> str:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise ContractError("source row key must be a non-empty scalar value.")
    normalized = str(value)
    if not normalized:
        raise ContractError("source row key must be a non-empty scalar value.")
    return normalized


def build_clip_payload(records: Sequence[SelectionRecord]) -> dict[str, Any]:
    """Build the clip body from exactly two selected intersection features."""

    if len(records) != 2:
        raise ContractError("Clip requires exactly two selected intersections.")

    intersection_ids: list[Any] = []
    for record in records:
        value = _first_present(record.properties, ("intersection_id", "id", "database_id"))
        if value is None:
            value = record.feature_id
        _append_unique(intersection_ids, _integer_identifier(value, "intersection ID", 1))

    if len(intersection_ids) != 2:
        raise ContractError("The two intersections must have distinct intersection IDs.")

    detect_id = _common_property(records, "detect_id", required=True)
    return {
        "detect_id": _uuid_identifier(detect_id),
        "intersection_ids": intersection_ids,
    }


def parse_geojson_response(payload: Any) -> ParsedGeoJson:
    """Accept a direct FeatureCollection or ``{data, crs, meta}`` envelope."""

    if not isinstance(payload, Mapping):
        raise ContractError("Backend response must be a JSON object.")

    if payload.get("type") == "FeatureCollection":
        collection = dict(payload)
        envelope_crs: Any = None
        metadata = _metadata_from(payload)
    else:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ContractError(
                "Backend response must be a GeoJSON FeatureCollection or contain one in 'data'."
            )
        collection = dict(data)
        envelope_crs = payload.get("crs")
        metadata = _metadata_from(payload)

    if collection.get("type") != "FeatureCollection":
        raise ContractError("Backend GeoJSON 'type' must be 'FeatureCollection'.")
    features = collection.get("features")
    if not isinstance(features, list):
        raise ContractError("Backend GeoJSON must contain a 'features' array.")

    for index, feature in enumerate(features):
        _validate_feature(feature, index)

    crs = _parse_crs(envelope_crs or collection.get("crs"))
    return ParsedGeoJson(collection, crs, metadata)


def geometry_groups(
    feature_collection: Mapping[str, Any],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Group features by exact GeoJSON geometry type, preserving first-seen order."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for feature in feature_collection.get("features", []):
        geometry = feature.get("geometry")
        geometry_type = "None" if geometry is None else str(geometry.get("type"))
        grouped.setdefault(geometry_type, []).append(feature)
    return list(grouped.items())


def _validate_feature(feature: Any, index: int) -> None:
    if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
        raise ContractError(f"GeoJSON feature {index} is not a valid Feature object.")
    properties = feature.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        raise ContractError(f"GeoJSON feature {index} has non-object properties.")
    geometry = feature.get("geometry")
    if geometry is None:
        return
    if not isinstance(geometry, Mapping):
        raise ContractError(f"GeoJSON feature {index} has an invalid geometry.")
    geometry_type = geometry.get("type")
    supported = {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }
    if geometry_type not in supported:
        raise ContractError(
            f"GeoJSON feature {index} uses unsupported geometry type {geometry_type!r}."
        )
    if geometry_type == "GeometryCollection":
        if not isinstance(geometry.get("geometries"), list):
            raise ContractError(
                f"GeoJSON feature {index} GeometryCollection needs a geometries array."
            )
    elif "coordinates" not in geometry:
        raise ContractError(f"GeoJSON feature {index} geometry has no coordinates.")


def _metadata_from(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = payload.get("meta", payload.get("metadata", {}))
    return metadata if isinstance(metadata, Mapping) else {}


def _parse_crs(value: Any) -> str:
    if value is None:
        return DEFAULT_CRS
    if isinstance(value, str):
        name = value.strip()
    elif isinstance(value, Mapping):
        properties = value.get("properties")
        name = properties.get("name", "") if isinstance(properties, Mapping) else ""
    else:
        raise ContractError("GeoJSON CRS must be a string or named CRS object.")

    if not name:
        raise ContractError("GeoJSON CRS name cannot be empty.")
    lowered = name.lower()
    if lowered in {
        "urn:ogc:def:crs:ogc:1.3:crs84",
        "ogc:crs84",
        "crs84",
    }:
        return DEFAULT_CRS
    if "epsg::" in lowered:
        return "EPSG:" + lowered.rsplit("epsg::", 1)[1]
    if "/epsg/0/" in lowered:
        return "EPSG:" + lowered.rsplit("/", 1)[1]
    return name.upper() if lowered.startswith("epsg:") else name


def _common_property(
    records: Sequence[SelectionRecord], name: str, *, required: bool
) -> Any:
    values = [_first_present(record.properties, (name,)) for record in records]
    non_null = [value for value in values if value is not None]
    if not non_null:
        if required:
            raise ContractError(f"Selected features must contain a '{name}' attribute.")
        return None
    if len(non_null) != len(records) or any(value != non_null[0] for value in non_null[1:]):
        if required:
            raise ContractError(f"Selected features must share one common '{name}'.")
        return None
    return non_null[0]


def _first_present(properties: Mapping[str, Any], names: Iterable[str]) -> Any:
    lowered = {str(key).lower(): value for key, value in properties.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and value != "":
            return value
    return None


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _integer_identifier(value: Any, label: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be an integer greater than or equal to {minimum}.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ContractError(f"{label} must be an integer.") from exc
    else:
        raise ContractError(f"{label} must be an integer.")
    if parsed < minimum:
        raise ContractError(f"{label} must be greater than or equal to {minimum}.")
    return parsed


def _uuid_identifier(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContractError("detect_id must be a valid UUID.") from exc
