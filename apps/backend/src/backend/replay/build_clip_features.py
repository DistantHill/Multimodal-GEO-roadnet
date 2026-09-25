"""Project persisted Clip tracks into GeoJSON road features."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Protocol

from pyproj import CRS, Proj, Transformer

from ..map_errors import MapDataError
from ..map_features import create_map_feature
from .track_points import TrackPoint


_EPS = 1e-9
_COORDINATE_SEMANTICS_VERSION = 2


class ClipProjectionContext(Protocol):
    detect_id: str
    source_srid: int
    processing_srid: int
    projection_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClipIntersection:
    intersection_id: int
    lon: float
    lat: float
    radius_m: float

class CoordinateTransformer:
    """Validate and apply the same fixed-CRS contract as Pipeline."""

    def __init__(self, context: ClipProjectionContext) -> None:
        if context.source_srid != 4326:
            raise MapDataError(
                f"Detect Run {context.detect_id} does not use EPSG:4326 source geometry"
            )
        if context.projection_config.get("coordinate_semantics_version") != (
            _COORDINATE_SEMANTICS_VERSION
        ):
            raise MapDataError(
                f"Detect Run {context.detect_id} has invalid coordinate semantics"
            )
        try:
            source = CRS.from_epsg(context.source_srid)
            processing = CRS.from_epsg(context.processing_srid)
            projection = Proj(processing)
            forward = Transformer.from_crs(source, processing, always_xy=True)
        except Exception as exc:
            raise MapDataError(
                f"Detect Run {context.detect_id} has an invalid projection context"
            ) from exc
        if not source.is_geographic or not processing.is_projected:
            raise MapDataError(
                f"Detect Run {context.detect_id} requires a projected processing CRS"
            )
        axes = processing.axis_info[:2]
        if len(axes) < 2 or any(
            str(axis.unit_name).lower() not in {"metre", "meter"}
            or not math.isclose(float(axis.unit_conversion_factor), 1.0, abs_tol=1e-12)
            for axis in axes
        ):
            raise MapDataError(
                f"Detect Run {context.detect_id} processing CRS must use metres"
            )
        if {str(axis.direction).lower() for axis in axes} != {"east", "north"}:
            raise MapDataError(
                f"Detect Run {context.detect_id} processing CRS axes must be east/north"
            )
        try:
            max_scale_error = float(
                context.projection_config.get("max_scale_error", 0.01)
            )
        except (TypeError, ValueError) as exc:
            raise MapDataError("invalid max_scale_error") from exc
        if not math.isfinite(max_scale_error) or max_scale_error < 0.0:
            raise MapDataError("invalid max_scale_error")
        self._processing = processing
        self._processing_srid = context.processing_srid
        self._projection = projection
        self._forward = forward
        self._enforce_area_of_use = bool(
            context.projection_config.get("enforce_area_of_use", True)
        )
        self._max_scale_error = max_scale_error

    def forward_many(
        self,
        coordinates: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        if not coordinates:
            return []
        if any(
            not (math.isfinite(lon) and math.isfinite(lat))
            or not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0)
            for lon, lat in coordinates
        ):
            raise MapDataError("input contains invalid EPSG:4326 coordinates")
        lons = [coordinate[0] for coordinate in coordinates]
        lats = [coordinate[1] for coordinate in coordinates]
        self._validate_bounds((min(lons), min(lats), max(lons), max(lats)))
        try:
            xs, ys = self._forward.transform(lons, lats, errcheck=True)
        except Exception as exc:
            raise MapDataError("coordinates could not be projected") from exc
        result = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in result):
            raise MapDataError("coordinates could not be projected")
        return result

    def _validate_bounds(self, bounds: tuple[float, float, float, float]) -> None:
        lon_min, lat_min, lon_max, lat_max = bounds
        area = self._processing.area_of_use
        if self._enforce_area_of_use and area is not None:
            if (
                not _longitude_bounds_within_area(
                    lon_min,
                    lon_max,
                    west=float(area.west),
                    east=float(area.east),
                )
                or lat_min < float(area.south)
                or lat_max > float(area.north)
            ):
                raise MapDataError(
                    f"input falls outside EPSG:{self._processing_srid} area of use"
                )
        samples = {
            (lon_min, lat_min),
            (lon_min, lat_max),
            (lon_max, lat_min),
            (lon_max, lat_max),
            ((lon_min + lon_max) / 2.0, (lat_min + lat_max) / 2.0),
        }
        for lon, lat in samples:
            try:
                factors = self._projection.get_factors(lon, lat)
                scales = (
                    float(factors.meridional_scale),
                    float(factors.parallel_scale),
                    float(factors.tissot_semimajor),
                    float(factors.tissot_semiminor),
                )
            except Exception as exc:
                raise MapDataError("scale distortion could not be evaluated") from exc
            if any(
                not math.isfinite(scale)
                or abs(scale - 1.0) > self._max_scale_error
                for scale in scales
            ):
                raise MapDataError(
                    f"EPSG:{self._processing_srid} scale distortion exceeds "
                    f"max_scale_error={self._max_scale_error:g}"
                )

def _clip_track(
    key: tuple[str, int],
    points: list[TrackPoint],
    intersections: list[ClipIntersection],
    requested: set[int],
    detect_id: str,
    transformer: CoordinateTransformer,
) -> list[dict[str, Any]]:
    if len(points) < 2:
        return []
    projected = transformer.forward_many(
        [(points[0].lon, points[0].lat)]
        + [(item.lon, item.lat) for item in intersections]
    )
    first_x, first_y = projected[0]
    active = {
        item.intersection_id
        for item, (center_x, center_y) in zip(intersections, projected[1:], strict=True)
        if math.hypot(first_x - center_x, first_y - center_y) < item.radius_m - _EPS
    }
    event_indexes = [
        index for index, point in enumerate(points) if point.crossing_id is not None
    ]
    features: list[dict[str, Any]] = []
    open_point: TrackPoint | None = None
    open_index = 0
    cursor = 0
    while cursor < len(event_indexes):
        first = event_indexes[cursor]
        batch_indexes = [first]
        cursor += 1
        while cursor < len(event_indexes):
            index = event_indexes[cursor]
            previous = event_indexes[cursor - 1]
            if index != previous + 1 or not _same_position(points[first], points[index]):
                break
            batch_indexes.append(index)
            cursor += 1
        batch = [points[index] for index in batch_indexes]
        before_empty = not active
        exits = sorted(
            (point for point in batch if point.crossing_type == 0),
            key=lambda point: (point.intersection_id or -1, point.crossing_id or -1),
        )
        entries = sorted(
            (point for point in batch if point.crossing_type == 1),
            key=lambda point: (point.intersection_id or -1, point.crossing_id or -1),
        )
        if len(exits) + len(entries) != len(batch):
            raise MapDataError(f"subtrack {key[1]} has invalid crossing metadata")
        for point in exits:
            assert point.intersection_id is not None
            if point.intersection_id not in active:
                raise MapDataError(
                    f"subtrack {key[1]} exits inactive intersection {point.intersection_id}"
                )
            active.remove(point.intersection_id)
        for point in entries:
            assert point.intersection_id is not None
            if point.intersection_id in active:
                raise MapDataError(
                    f"subtrack {key[1]} re-enters active intersection {point.intersection_id}"
                )
            active.add(point.intersection_id)
        after_empty = not active
        if not before_empty and after_empty:
            open_point = exits[-1]
            open_index = batch_indexes[-1]
        elif before_empty and not after_empty:
            if open_point is not None:
                end_point = entries[0]
                if {open_point.intersection_id, end_point.intersection_id} == requested:
                    segment = points[open_index : batch_indexes[0] + 1]
                    coordinates = [[point.lon, point.lat] for point in segment]
                    distance = _polyline_distance(coordinates, transformer)
                    if distance > _EPS:
                        ordinal = len(features)
                        features.append(
                            create_map_feature(
                                f"{detect_id}:{key[1]}:{ordinal}",
                                {"type": "LineString", "coordinates": coordinates},
                                vehicle_id=key[0],
                                subtrack_index=key[1],
                                clip_type="between_intersection",
                                from_intersection_id=open_point.intersection_id,
                                to_intersection_id=end_point.intersection_id,
                                start_crossing_id=open_point.crossing_id,
                                end_crossing_id=end_point.crossing_id,
                                resolved_point_ids=[
                                    point.resolved_point_id for point in segment[1:-1]
                                ],
                                source_row_keys=[
                                    point.source_row_key for point in segment[1:-1]
                                ],
                                distance_m=distance,
                                point_count=len(segment),
                            )
                        )
            open_point = None
        elif before_empty and after_empty:
            raise MapDataError(f"subtrack {key[1]} has a no-op crossing batch")
    return features

def _same_position(left: TrackPoint, right: TrackPoint) -> bool:
    return abs(left.lon - right.lon) <= 1e-12 and abs(left.lat - right.lat) <= 1e-12

def _polyline_distance(
    coordinates: list[list[float]],
    transformer: CoordinateTransformer,
) -> float:
    projected = transformer.forward_many(
        [(coordinate[0], coordinate[1]) for coordinate in coordinates]
    )
    return sum(
        math.hypot(right[0] - left[0], right[1] - left[1])
        for left, right in zip(projected, projected[1:])
    )

def _longitude_bounds_within_area(
    lon_min: float,
    lon_max: float,
    *,
    west: float,
    east: float,
) -> bool:
    if west <= east:
        return west <= lon_min and lon_max <= east
    return (west <= lon_min and lon_max <= 180.0) or (
        -180.0 <= lon_min and lon_max <= east
    )


def build_clip_features(
    grouped_tracks: dict[tuple[str, int], list[TrackPoint]],
    intersections: list[ClipIntersection],
    requested_intersection_ids: set[int],
    context: ClipProjectionContext,
    feature_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Build bounded Clip features from already selected and ordered DB rows."""
    transformer = CoordinateTransformer(context)
    features: list[dict[str, Any]] = []
    truncated = False
    for key, points in grouped_tracks.items():
        for feature in _clip_track(
            key,
            points,
            intersections,
            requested_intersection_ids,
            context.detect_id,
            transformer,
        ):
            if len(features) >= feature_limit:
                truncated = True
                break
            features.append(feature)
        if truncated:
            break
    return features, truncated
