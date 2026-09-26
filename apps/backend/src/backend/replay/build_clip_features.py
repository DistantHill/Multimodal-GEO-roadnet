"""Project persisted Clip tracks into GeoJSON road features."""

from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
from typing import Any, Protocol

from pyproj import CRS, Proj, Transformer

from ..map_errors import MapDataError
from ..map_features import create_map_feature
from .track_points import TrackBatch


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

    def forward_many(self, coordinates: np.ndarray) -> np.ndarray:
        if coordinates.shape != (len(coordinates), 2) or coordinates.dtype != np.float64:
            raise MapDataError("coordinates must be float64 (N,2)")
        if not len(coordinates):
            return np.empty((0,2), dtype=np.float64)
        if (not np.isfinite(coordinates).all() or np.any(np.abs(coordinates[:,0]) > 180)
                or np.any(np.abs(coordinates[:,1]) > 90)):
            raise MapDataError("input contains invalid EPSG:4326 coordinates")
        self._validate_bounds((float(coordinates[:,0].min()), float(coordinates[:,1].min()),
                               float(coordinates[:,0].max()), float(coordinates[:,1].max())))
        try:
            xs, ys = self._forward.transform(coordinates[:,0], coordinates[:,1], errcheck=True)
        except Exception as exc:
            raise MapDataError("coordinates could not be projected") from exc
        result = np.column_stack((xs,ys))
        if not np.isfinite(result).all():
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
    key: tuple[str, int], points: TrackBatch, intersections: list[ClipIntersection],
    requested: set[int], detect_id: str, transformer: CoordinateTransformer,
) -> list[dict[str, Any]]:
    if len(points) < 2:
        return []
    if np.any(points.crossing_valid != points.intersection_valid):
        raise MapDataError(f"subtrack {key[1]} has invalid crossing metadata")
    xy = transformer.forward_many(points.lonlat)
    centers = transformer.forward_many(np.asarray([(item.lon,item.lat) for item in intersections], dtype=np.float64).reshape(-1,2))
    first_x,first_y = xy[0]
    active = {item.intersection_id for item,(cx,cy) in zip(intersections,centers,strict=True)
              if math.hypot(first_x-cx, first_y-cy) < item.radius_m-_EPS}
    events = np.flatnonzero(points.crossing_valid).tolist()
    features = []
    open_point: int | None = None
    open_index = 0
    cursor = 0
    while cursor < len(events):
        first = events[cursor]
        batch = [first]
        cursor += 1
        while cursor < len(events):
            index = events[cursor]
            if index != events[cursor-1]+1 or not np.all(np.abs(points.lonlat[first]-points.lonlat[index]) <= 1e-12):
                break
            batch.append(index)
            cursor += 1
        before_empty = not active
        ordering = lambda i: (int(points.intersection_ids[i]) or -1, int(points.crossing_ids[i]) or -1)
        exits = sorted((i for i in batch if points.crossing_types[i] == 0), key=ordering)
        entries = sorted((i for i in batch if points.crossing_types[i] == 1), key=ordering)
        if len(exits)+len(entries) != len(batch):
            raise MapDataError(f"subtrack {key[1]} has invalid crossing metadata")
        for index in exits:
            identifier = int(points.intersection_ids[index])
            if identifier not in active:
                raise MapDataError(f"subtrack {key[1]} exits inactive intersection {identifier}")
            active.remove(identifier)
        for index in entries:
            identifier = int(points.intersection_ids[index])
            if identifier in active:
                raise MapDataError(f"subtrack {key[1]} re-enters active intersection {identifier}")
            active.add(identifier)
        after_empty = not active
        if not before_empty and after_empty:
            open_point,open_index = exits[-1],batch[-1]
        elif before_empty and not after_empty:
            if open_point is not None:
                end = entries[0]
                if {int(points.intersection_ids[open_point]), int(points.intersection_ids[end])} == requested:
                    stop = batch[0]+1
                    distance = float(np.linalg.norm(np.diff(xy[open_index:stop], axis=0), axis=1).sum())
                    if distance > _EPS:
                        segment = points.slice(open_index,stop)
                        features.append(create_map_feature(
                            f"{detect_id}:{key[1]}:{len(features)}",
                            {"type":"LineString", "coordinates":segment.lonlat.tolist()},
                            vehicle_id=key[0],subtrack_index=key[1],clip_type="between_intersection",
                            from_intersection_id=int(points.intersection_ids[open_point]),
                            to_intersection_id=int(points.intersection_ids[end]),
                            start_crossing_id=int(points.crossing_ids[open_point]),end_crossing_id=int(points.crossing_ids[end]),
                            resolved_point_ids=[int(value) if valid else None for value,valid in zip(segment.resolved_point_ids[1:-1],segment.resolved_valid[1:-1])],
                            source_row_keys=[str(value) if valid else None for value,valid in zip(segment.source_row_keys[1:-1],segment.source_valid[1:-1])],
                            distance_m=distance,point_count=len(segment),
                        ))
            open_point = None
        elif before_empty and after_empty:
            raise MapDataError(f"subtrack {key[1]} has a no-op crossing batch")
    return features

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
    grouped_tracks: dict[tuple[str, int], TrackBatch],
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
