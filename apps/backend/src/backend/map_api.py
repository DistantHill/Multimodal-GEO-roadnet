from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pyproj import CRS, Proj, Transformer

from .database import get_read_connection
from .map_schemas import BBox, ClipRequest, FeatureCollection, ReplayRequest, parse_bbox


router = APIRouter(prefix="/api/v1/map", tags=["map"])

_CRS = {
    "type": "name",
    "properties": {"name": "EPSG:4326"},
}
_LAYER_LIMIT_DEFAULT = 1_000
_LAYER_LIMIT_MAX = 5_000
_CLIP_FEATURE_LIMIT = 1_000
_EPS = 1e-9
_COORDINATE_SEMANTICS_VERSION = 2


class MapNotFoundError(Exception):
    pass


class MapDataError(Exception):
    pass


class MapStore(Protocol):
    def layer_catalog(self) -> dict[str, Any]: ...

    def read_layer(
        self,
        layer_id: str,
        *,
        bbox: BBox | None,
        detect_id: str | None,
        fit_id: str | None,
        limit: int,
    ) -> dict[str, Any]: ...

    def replay(self, request: ReplayRequest) -> dict[str, Any]: ...

    def clip(self, request: ClipRequest) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _DetectContext:
    detect_id: str
    clean_id: str
    target_table: str
    source_srid: int | None
    processing_srid: int | None
    projection_config: dict[str, Any]


class _CoordinateTransformer:
    """Backend-local fixed-CRS adapter.

    Backend is built as a standalone image, so it cannot import Pipeline's
    projection module. Keep this validation boundary small and fail closed on
    legacy Detect Runs whose x/y semantics were never pinned to one CRS.
    """

    def __init__(self, context: _DetectContext) -> None:
        if context.source_srid != 4326:
            raise MapDataError(
                f"Detect Run {context.detect_id} does not use EPSG:4326 source geometry"
            )
        if context.processing_srid is None:
            raise MapDataError(
                f"Detect Run {context.detect_id} has no fixed processing CRS"
            )
        if (
            context.projection_config.get("coordinate_semantics_version")
            != _COORDINATE_SEMANTICS_VERSION
        ):
            raise MapDataError(
                f"Detect Run {context.detect_id} has legacy coordinate semantics"
            )
        try:
            source = CRS.from_epsg(context.source_srid)
            processing = CRS.from_epsg(context.processing_srid)
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
            or not math.isclose(
                float(axis.unit_conversion_factor),
                1.0,
                abs_tol=1e-12,
            )
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
            raise MapDataError(
                f"Detect Run {context.detect_id} has an invalid max_scale_error"
            ) from exc
        if not math.isfinite(max_scale_error) or max_scale_error < 0.0:
            raise MapDataError(
                f"Detect Run {context.detect_id} has an invalid max_scale_error"
            )
        try:
            projection = Proj(processing)
            forward = Transformer.from_crs(source, processing, always_xy=True)
            inverse = Transformer.from_crs(processing, source, always_xy=True)
        except Exception as exc:
            raise MapDataError(
                f"Detect Run {context.detect_id} has an invalid projection context"
            ) from exc
        self._context = context
        self._processing = processing
        self._processing_srid = context.processing_srid
        self._projection = projection
        self._forward = forward
        self._inverse = inverse
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
            raise MapDataError("clip input contains invalid EPSG:4326 coordinates")
        lons = [coordinate[0] for coordinate in coordinates]
        lats = [coordinate[1] for coordinate in coordinates]
        self._validate_bounds((min(lons), min(lats), max(lons), max(lats)))
        try:
            xs, ys = self._forward.transform(
                lons,
                lats,
                errcheck=True,
            )
        except Exception as exc:
            raise MapDataError("clip input could not be projected") from exc
        projected = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in projected):
            raise MapDataError("clip input could not be projected")
        return projected

    def _validate_bounds(
        self,
        bounds: tuple[float, float, float, float],
    ) -> None:
        """Apply the Pipeline's area-of-use and scale-distortion contract."""
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
                    f"clip input falls outside EPSG:{self._processing_srid} "
                    f"area of use ({area.west}, {area.south}, "
                    f"{area.east}, {area.north})"
                )

        centre_lon = (lon_min + lon_max) / 2.0
        centre_lat = (lat_min + lat_max) / 2.0
        samples = {
            (lon_min, lat_min),
            (lon_min, lat_max),
            (lon_max, lat_min),
            (lon_max, lat_max),
            (centre_lon, centre_lat),
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
                raise MapDataError(
                    "clip input scale distortion could not be evaluated"
                ) from exc
            if any(
                not math.isfinite(scale)
                or abs(scale - 1.0) > self._max_scale_error
                for scale in scales
            ):
                raise MapDataError(
                    f"EPSG:{self._processing_srid} scale distortion exceeds "
                    f"configured max_scale_error={self._max_scale_error:g} "
                    f"at ({lon}, {lat})"
                )

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        try:
            lon, lat = self._inverse.transform(x, y, errcheck=True)
        except Exception as exc:
            raise MapDataError("clip endpoint could not be transformed to EPSG:4326") from exc
        result = float(lon), float(lat)
        if not all(math.isfinite(value) for value in result):
            raise MapDataError("clip endpoint could not be transformed to EPSG:4326")
        return result


def _longitude_bounds_within_area(
    lon_min: float,
    lon_max: float,
    *,
    west: float,
    east: float,
) -> bool:
    """Check an ordered longitude interval against an EPSG area-of-use bbox."""
    if west <= east:
        return west <= lon_min and lon_max <= east
    return (
        west <= lon_min and lon_max <= 180.0
    ) or (
        -180.0 <= lon_min and lon_max <= east
    )


@dataclass(frozen=True, slots=True)
class _Intersection:
    intersection_id: int
    lon: float
    lat: float
    radius_m: float


@dataclass(frozen=True, slots=True)
class _TrackPoint:
    point_id: int | None
    timestamp: float
    lon: float
    lat: float
    heading: float
    speed: float
    source_row_key: str | None
    injected: bool


@dataclass(frozen=True, slots=True)
class _Crossing:
    progress: float
    kind: str
    intersection_id: int
    segment_index: int
    fraction: float
    lon: float
    lat: float
    timestamp: float


_LAYER_DEFINITIONS = (
    {
        "id": "trajectory_points",
        "title": "Trajectory points",
        "geometry_type": "Point",
        "required_context": [],
    },
    {
        "id": "intersections",
        "title": "Verified intersections",
        "geometry_type": "Polygon",
        "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
    {
        "id": "entry_exit_points",
        "title": "Verified entry/exit points",
        "geometry_type": "Point",
        "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
    {
        "id": "centerlines",
        "title": "Fitted centerlines",
        "geometry_type": "LineString",
        "required_context": ["fit_id"],
        "default_context": "latest completed Fit Run",
    },
)


class PostgresMapStore:
    """Bounded, read-only projections over the Pipeline PostgreSQL schema."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def layer_catalog(self) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT "
                "(SELECT d.id::text FROM detect_runs AS d "
                " WHERE d.status = 'completed' "
                " ORDER BY d.finished_at DESC NULLS LAST, d.started_at DESC LIMIT 1), "
                "(SELECT f.id::text FROM fit_runs AS f "
                " WHERE f.status = 'completed' "
                " ORDER BY f.finished_at DESC NULLS LAST, f.started_at DESC LIMIT 1), "
                "(SELECT f.detect_id::text FROM fit_runs AS f "
                " WHERE f.status = 'completed' "
                " ORDER BY f.finished_at DESC NULLS LAST, f.started_at DESC LIMIT 1)"
            )
            row = cursor.fetchone()
        default_context = {
            "detect_id": str(row[0]) if row and row[0] is not None else None,
            "fit_id": str(row[1]) if row and row[1] is not None else None,
            "fit_detect_id": str(row[2]) if row and row[2] is not None else None,
        }
        return {
            "crs": "EPSG:4326",
            "layers": [dict(item) for item in _LAYER_DEFINITIONS],
            "default_context": default_context,
        }

    def read_layer(
        self,
        layer_id: str,
        *,
        bbox: BBox | None,
        detect_id: str | None,
        fit_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if layer_id == "trajectory_points":
            rows = self._trajectory_layer(bbox, limit + 1)
            context: dict[str, Any] = {}
            features = [self._trajectory_feature(row) for row in rows[:limit]]
        elif layer_id == "intersections":
            context_row = self._resolve_detect(detect_id)
            rows = self._intersection_layer(context_row.detect_id, bbox, limit + 1)
            context = {"detect_id": context_row.detect_id, "clean_id": context_row.clean_id}
            features = [
                self._intersection_feature(row, context_row.detect_id)
                for row in rows[:limit]
            ]
        elif layer_id == "entry_exit_points":
            context_row = self._resolve_detect(detect_id)
            rows = self._entry_exit_layer(context_row.detect_id, bbox, limit + 1)
            context = {"detect_id": context_row.detect_id, "clean_id": context_row.clean_id}
            features = [
                self._entry_exit_feature(row, context_row.detect_id)
                for row in rows[:limit]
            ]
        elif layer_id == "centerlines":
            resolved_fit_id, resolved_detect_id = self._resolve_fit(fit_id)
            if detect_id is not None and str(detect_id) != resolved_detect_id:
                raise MapNotFoundError(
                    f"completed Fit Run {resolved_fit_id} does not belong to Detect Run {detect_id}"
                )
            rows = self._centerline_layer(resolved_fit_id, bbox, limit + 1)
            context = {"fit_id": resolved_fit_id, "detect_id": resolved_detect_id}
            features = [self._centerline_feature(row) for row in rows[:limit]]
        else:
            raise MapNotFoundError(f"unknown map layer: {layer_id}")

        return _feature_collection(
            features,
            layer=layer_id,
            count=len(features),
            truncated=len(rows) > limit,
            bbox=list(bbox) if bbox is not None else None,
            **context,
        )

    def replay(self, request: ReplayRequest) -> dict[str, Any]:
        detect_context = (
            self._resolve_detect(str(request.detect_id))
            if request.detect_id is not None
            else None
        )
        selection_kind = "point_ids" if request.point_ids is not None else "bbox"
        resolved_point_ids: set[int] | None = None
        missing_point_ids: list[int] | None = None
        if request.point_ids is not None:
            resolved_point_ids = self._resolve_point_ids(
                request.point_ids,
                detect_context.clean_id if detect_context is not None else None,
            )
            missing_point_ids = sorted(set(request.point_ids) - resolved_point_ids)
            if not resolved_point_ids:
                scope = (
                    f"completed Detect Run {detect_context.detect_id}"
                    if detect_context is not None
                    else "active trajectories"
                )
                raise MapNotFoundError(f"none of the requested point_ids exist in {scope}")
        if detect_context is None:
            selected, vehicles_truncated = self._select_raw_tracks(
                point_ids=request.point_ids,
                bbox=request.bbox,
                limit=request.max_vehicles,
            )
            rows = self._fetch_raw_tracks(selected, request.max_points + 1)
        else:
            selected, vehicles_truncated = self._select_clean_tracks(
                detect_context.clean_id,
                detect_context.target_table,
                point_ids=request.point_ids,
                bbox=request.bbox,
                limit=request.max_vehicles,
            )
            rows = self._fetch_clean_tracks(
                detect_context.clean_id,
                detect_context.target_table,
                selected,
                request.max_points + 1,
            )

        grouped, points_truncated = _complete_tracks(rows, request.max_points)
        features = [
            _track_feature(key, points, detect_context)
            for key, points in grouped.items()
            if points
        ]
        return _feature_collection(
            features,
            operation="replay",
            selection=selection_kind,
            detect_id=detect_context.detect_id if detect_context else None,
            clean_id=detect_context.clean_id if detect_context else None,
            selected_track_count=len(selected),
            returned_track_count=len(features),
            returned_point_count=sum(len(points) for points in grouped.values()),
            requested_point_count=(
                len(request.point_ids) if request.point_ids is not None else None
            ),
            resolved_point_count=(
                len(resolved_point_ids) if resolved_point_ids is not None else None
            ),
            missing_point_ids=missing_point_ids,
            truncated=vehicles_truncated or points_truncated,
        )

    def clip(self, request: ClipRequest) -> dict[str, Any]:
        context = self._resolve_detect(str(request.detect_id))
        transformer = _CoordinateTransformer(context)
        requested_ids = [int(value) for value in request.intersection_ids]
        intersections = self._fetch_intersections(context.detect_id, requested_ids)
        if len(intersections) != 2:
            found = {item.intersection_id for item in intersections}
            missing = sorted(set(requested_ids) - found)
            raise MapNotFoundError(
                f"completed intersections not found in Detect Run {context.detect_id}: {missing}"
            )
        if any(
            not math.isfinite(item.radius_m) or item.radius_m <= 0.0
            for item in intersections
        ):
            raise MapDataError("selected intersections have an invalid clip radius")

        selected, vehicles_truncated = self._select_clip_tracks(
            context.clean_id,
            context.detect_id,
            requested_ids,
            request.max_vehicles,
        )
        rows = self._fetch_clean_tracks(
            context.clean_id,
            context.target_table,
            selected,
            request.max_points + 1,
        )
        grouped, points_truncated = _complete_tracks(rows, request.max_points)
        features: list[dict[str, Any]] = []
        response_point_count = 0
        clips_truncated = False
        for key, points in grouped.items():
            for feature in _clip_track(
                key,
                points,
                intersections,
                context.detect_id,
                transformer,
            ):
                if len(features) >= _CLIP_FEATURE_LIMIT:
                    clips_truncated = True
                    break
                response_point_count += int(feature["properties"]["point_count"])
                features.append(feature)
            if clips_truncated:
                break

        return _feature_collection(
            features,
            operation="clip",
            detect_id=context.detect_id,
            clean_id=context.clean_id,
            intersection_ids=requested_ids,
            selected_track_count=len(selected),
            returned_clip_count=len(features),
            returned_point_count=response_point_count,
            truncated=vehicles_truncated or points_truncated or clips_truncated,
        )

    def _resolve_detect(self, detect_id: str | None) -> _DetectContext:
        clauses = "d.id = %s AND d.status = 'completed'" if detect_id else "d.status = 'completed'"
        order = "" if detect_id else " ORDER BY d.finished_at DESC NULLS LAST, d.started_at DESC"
        params: tuple[Any, ...] = (detect_id,) if detect_id else ()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.id::text, d.clean_id::text, "
                "COALESCE(c.parameters->>'target_table', 'trajectory_points'), "
                "c.source_srid, c.processing_srid, c.projection_config "
                "FROM detect_runs AS d JOIN clean_runs AS c ON c.id = d.clean_id WHERE "
                + clauses + order + " LIMIT 1",
                params,
            )
            row = cursor.fetchone()
        if row is None:
            if detect_id:
                raise MapNotFoundError(f"unknown completed Detect Run: {detect_id}")
            raise MapNotFoundError("no completed Detect Run is available")
        target_table = str(row[2])
        _quoted_identifier(target_table)
        return _DetectContext(
            detect_id=str(row[0]),
            clean_id=str(row[1]),
            target_table=target_table,
            source_srid=int(row[3]) if row[3] is not None else None,
            processing_srid=int(row[4]) if row[4] is not None else None,
            projection_config=_projection_config(row[5], str(row[0])),
        )

    def _resolve_fit(self, fit_id: str | None) -> tuple[str, str]:
        clauses = "f.id = %s AND f.status = 'completed'" if fit_id else "f.status = 'completed'"
        order = "" if fit_id else " ORDER BY f.finished_at DESC NULLS LAST, f.started_at DESC"
        params: tuple[Any, ...] = (fit_id,) if fit_id else ()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT f.id::text, f.detect_id::text "
                "FROM fit_runs AS f WHERE " + clauses + order + " LIMIT 1",
                params,
            )
            row = cursor.fetchone()
        if row is None:
            if fit_id:
                raise MapNotFoundError(f"unknown completed Fit Run: {fit_id}")
            raise MapNotFoundError("no completed Fit Run is available")
        return str(row[0]), str(row[1])

    def _trajectory_layer(self, bbox: BBox | None, limit: int) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("tp.geom", bbox)
        sql = (
            "SELECT tp.id, tp.vehicle_id, tp.timestamp, tp.heading, tp.speed, tp.extra, "
            "ST_AsGeoJSON(tp.geom)::jsonb FROM trajectory_points AS tp "
            "WHERE tp.is_active IS TRUE" + bbox_clause + " ORDER BY tp.id LIMIT %s"
        )
        return self._fetchall(sql, (*bbox_params, limit))

    def _intersection_layer(self, detect_id: str, bbox: BBox | None, limit: int) -> list[Any]:
        geometry = (
            "COALESCE(i.geom, ST_Buffer(ST_SetSRID(ST_MakePoint(ci.centroid_lon, "
            "ci.centroid_lat), 4326)::geography, i.clip_radius_m)::geometry)"
        )
        bbox_clause, bbox_params = _bbox_clause(geometry, bbox)
        sql = (
            "SELECT i.id, ci.centroid_lon, ci.centroid_lat, "
            "i.clip_radius_m, i.branch_centers_deg, i.transition_counts, "
            "i.observation_count, i.reason, ST_AsGeoJSON(" + geometry + ")::jsonb "
            "FROM intersections AS i JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id AND ci.detect_id = i.detect_id "
            "WHERE i.detect_id = %s AND i.is_complete IS TRUE" + bbox_clause +
            " ORDER BY i.id LIMIT %s"
        )
        return self._fetchall(sql, (detect_id, *bbox_params, limit))

    def _entry_exit_layer(self, detect_id: str, bbox: BBox | None, limit: int) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("crossing.geom", bbox)
        sql = (
            "SELECT CONCAT(eep.id, ':', endpoint.kind), eep.id, eep.intersection_id, "
            "eep.vehicle_id, endpoint.kind, crossing.branch_angle_deg, "
            "ST_AsGeoJSON(crossing.geom)::jsonb "
            "FROM entry_exit_points AS eep "
            "CROSS JOIN LATERAL (VALUES (eep.entry_crossing_id, 'entry'), "
            "(eep.exit_crossing_id, 'exit')) AS endpoint(crossing_id, kind) "
            "JOIN candidate_entry_exit_points AS crossing "
            "ON crossing.id = endpoint.crossing_id AND crossing.detect_id = eep.detect_id "
            "WHERE eep.detect_id = %s" + bbox_clause +
            " ORDER BY eep.id, endpoint.kind LIMIT %s"
        )
        return self._fetchall(sql, (detect_id, *bbox_params, limit))

    def _centerline_layer(self, fit_id: str, bbox: BBox | None, limit: int) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("c.geom", bbox)
        sql = (
            "SELECT c.id, c.detect_id::text, c.variant, c.from_intersection_id, "
            "c.to_intersection_id, c.length_m, c.forward_clip_count, c.reverse_clip_count, "
            "ST_AsGeoJSON(c.geom)::jsonb FROM centerlines AS c "
            "WHERE c.fit_id = %s" + bbox_clause + " ORDER BY c.id LIMIT %s"
        )
        return self._fetchall(sql, (fit_id, *bbox_params, limit))

    def _select_raw_tracks(
        self,
        *,
        point_ids: list[int] | None,
        bbox: BBox | None,
        limit: int,
    ) -> tuple[list[tuple[str, int]], bool]:
        if point_ids is not None:
            sql = (
                "SELECT DISTINCT tp.vehicle_id, 0 FROM trajectory_points AS tp "
                "WHERE tp.is_active IS TRUE AND tp.id = ANY(%s::bigint[]) "
                "ORDER BY tp.vehicle_id LIMIT %s"
            )
            params: tuple[Any, ...] = (point_ids, limit + 1)
        else:
            assert bbox is not None
            sql = (
                "SELECT DISTINCT tp.vehicle_id, 0 FROM trajectory_points AS tp "
                "WHERE tp.is_active IS TRUE AND tp.geom && "
                "ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
                "ORDER BY tp.vehicle_id LIMIT %s"
            )
            params = (*bbox, limit + 1)
        rows = self._fetchall(sql, params)
        return [(str(row[0]), int(row[1])) for row in rows[:limit]], len(rows) > limit

    def _resolve_point_ids(
        self,
        requested: list[int],
        clean_id: str | None,
    ) -> set[int]:
        if clean_id is None:
            rows = self._fetchall(
                "SELECT tp.id FROM trajectory_points AS tp "
                "WHERE tp.is_active IS TRUE AND tp.id = ANY(%s::bigint[])",
                (requested,),
            )
        else:
            rows = self._fetchall(
                "SELECT DISTINCT ktp.trajectory_point_id "
                "FROM kept_trajectory_points AS ktp "
                "WHERE ktp.clean_id = %s "
                "AND ktp.trajectory_point_id = ANY(%s::bigint[])",
                (clean_id, requested),
            )
        return {int(row[0]) for row in rows}

    def _select_clean_tracks(
        self,
        clean_id: str,
        target_table: str,
        *,
        point_ids: list[int] | None,
        bbox: BBox | None,
        limit: int,
    ) -> tuple[list[tuple[str, int]], bool]:
        if point_ids is not None:
            sql = (
                "SELECT DISTINCT ktp.vehicle_id, ktp.subtrack_index "
                "FROM kept_trajectory_points AS ktp "
                "WHERE ktp.clean_id = %s AND ktp.trajectory_point_id = ANY(%s::bigint[]) "
                "ORDER BY ktp.vehicle_id, ktp.subtrack_index LIMIT %s"
            )
            params: tuple[Any, ...] = (clean_id, point_ids, limit + 1)
        else:
            assert bbox is not None
            safe_table = _quoted_identifier(target_table)
            sql = (
                "SELECT selected.vehicle_id, selected.subtrack_index FROM ("
                " SELECT ktp.vehicle_id, ktp.subtrack_index "
                f" FROM kept_trajectory_points AS ktp JOIN {safe_table} AS tp "
                " ON tp.id = ktp.trajectory_point_id "
                " WHERE ktp.clean_id = %s AND tp.geom && "
                " ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
                " UNION SELECT ip.vehicle_id, ip.subtrack_index FROM injected_points AS ip "
                " WHERE ip.clean_id = %s AND ip.geom && "
                " ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
                ") AS selected ORDER BY selected.vehicle_id, selected.subtrack_index LIMIT %s"
            )
            params = (clean_id, *bbox, clean_id, *bbox, limit + 1)
        rows = self._fetchall(sql, params)
        return [(str(row[0]), int(row[1])) for row in rows[:limit]], len(rows) > limit

    def _select_clip_tracks(
        self,
        clean_id: str,
        detect_id: str,
        intersection_ids: list[int],
        limit: int,
    ) -> tuple[list[tuple[str, int]], bool]:
        sql = (
            "WITH matching_vehicles AS ("
            " SELECT eep.vehicle_id FROM entry_exit_points AS eep "
            " WHERE eep.detect_id = %s AND eep.intersection_id = ANY(%s::bigint[]) "
            " GROUP BY eep.vehicle_id HAVING COUNT(DISTINCT eep.intersection_id) = 2"
            ") SELECT DISTINCT ktp.vehicle_id, ktp.subtrack_index "
            "FROM kept_trajectory_points AS ktp JOIN matching_vehicles AS mv "
            "ON mv.vehicle_id = ktp.vehicle_id WHERE ktp.clean_id = %s "
            "ORDER BY ktp.vehicle_id, ktp.subtrack_index LIMIT %s"
        )
        rows = self._fetchall(sql, (detect_id, intersection_ids, clean_id, limit + 1))
        return [(str(row[0]), int(row[1])) for row in rows[:limit]], len(rows) > limit

    def _fetch_raw_tracks(self, selected: list[tuple[str, int]], limit: int) -> list[Any]:
        if not selected:
            return []
        vehicle_ids = [vehicle_id for vehicle_id, _ in selected]
        return self._fetchall(
            "SELECT tp.vehicle_id, 0, tp.id, tp.timestamp, tp.lon, tp.lat, "
            "tp.heading, tp.speed, NULL::text, FALSE "
            "FROM trajectory_points AS tp WHERE tp.is_active IS TRUE "
            "AND tp.vehicle_id = ANY(%s::text[]) "
            "ORDER BY tp.vehicle_id, tp.timestamp, tp.id LIMIT %s",
            (vehicle_ids, limit),
        )

    def _fetch_clean_tracks(
        self,
        clean_id: str,
        target_table: str,
        selected: list[tuple[str, int]],
        limit: int,
    ) -> list[Any]:
        if not selected:
            return []
        vehicle_ids = [vehicle_id for vehicle_id, _ in selected]
        subtrack_indexes = [subtrack_index for _, subtrack_index in selected]
        safe_table = _quoted_identifier(target_table)
        sql = (
            "WITH requested(vehicle_id, subtrack_index) AS ("
            " SELECT * FROM unnest(%s::text[], %s::integer[])"
            ") SELECT vehicle_id, subtrack_index, point_id, timestamp, lon, lat, "
            "heading, speed, source_row_key, injected FROM ("
            " SELECT ktp.vehicle_id, ktp.subtrack_index, tp.id AS point_id, "
            " tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, ktp.source_row_key, "
            " FALSE AS injected, ktp.sequence_in_subtrack AS anchor_sequence, "
            " 0 AS point_kind, ktp.id AS row_id "
            " FROM kept_trajectory_points AS ktp JOIN requested AS r "
            f" USING (vehicle_id, subtrack_index) JOIN {safe_table} AS tp "
            " ON tp.id = ktp.trajectory_point_id WHERE ktp.clean_id = %s "
            " UNION ALL SELECT ip.vehicle_id, ip.subtrack_index, NULL::bigint, "
            " ip.timestamp, ip.lon, ip.lat, ip.heading, ip.speed, ip.source_row_key, "
            " TRUE, ip.after_sequence_in_subtrack, 1, ip.id FROM injected_points AS ip "
            " JOIN requested AS r USING (vehicle_id, subtrack_index) "
            " WHERE ip.clean_id = %s"
            ") AS clean_points ORDER BY vehicle_id, subtrack_index, anchor_sequence, "
            "point_kind, row_id LIMIT %s"
        )
        return self._fetchall(
            sql,
            (vehicle_ids, subtrack_indexes, clean_id, clean_id, limit),
        )

    def _fetch_intersections(
        self,
        detect_id: str,
        intersection_ids: list[int],
    ) -> list[_Intersection]:
        rows = self._fetchall(
            "SELECT i.id, ci.centroid_lon, ci.centroid_lat, i.clip_radius_m "
            "FROM intersections AS i JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id AND ci.detect_id = i.detect_id "
            "WHERE i.detect_id = %s AND i.is_complete IS TRUE "
            "AND i.id = ANY(%s::bigint[]) ORDER BY i.id",
            (detect_id, intersection_ids),
        )
        return [
            _Intersection(int(row[0]), float(row[1]), float(row[2]), float(row[3]))
            for row in rows
        ]

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    @staticmethod
    def _trajectory_feature(row: Any) -> dict[str, Any]:
        return _feature(
            int(row[0]),
            _geometry(row[6]),
            database_id=int(row[0]),
            vehicle_id=str(row[1]),
            timestamp=float(row[2]),
            heading=float(row[3]),
            speed=float(row[4]),
            extra=row[5] if isinstance(row[5], dict) else {},
        )

    @staticmethod
    def _intersection_feature(row: Any, detect_id: str) -> dict[str, Any]:
        return _feature(
            int(row[0]),
            _geometry(row[8]),
            detect_id=detect_id,
            intersection_id=int(row[0]),
            centroid=[float(row[1]), float(row[2])],
            clip_radius_m=float(row[3]),
            branch_centers_deg=_json_value(row[4], []),
            transition_counts=_json_value(row[5], {}),
            observation_count=int(row[6]),
            reason=str(row[7]),
        )

    @staticmethod
    def _entry_exit_feature(row: Any, detect_id: str) -> dict[str, Any]:
        return _feature(
            str(row[0]),
            _geometry(row[6]),
            entry_exit_id=int(row[1]),
            detect_id=detect_id,
            intersection_id=int(row[2]),
            vehicle_id=str(row[3]),
            crossing_type=str(row[4]),
            branch_angle_deg=float(row[5]) if row[5] is not None else None,
        )

    @staticmethod
    def _centerline_feature(row: Any) -> dict[str, Any]:
        return _feature(
            int(row[0]),
            _geometry(row[8]),
            detect_id=str(row[1]),
            variant=str(row[2]),
            from_intersection_id=int(row[3]),
            to_intersection_id=int(row[4]),
            length_m=float(row[5]),
            forward_clip_count=int(row[6]),
            reverse_clip_count=int(row[7]),
        )


def get_map_store(connection: Any = Depends(get_read_connection)) -> MapStore:
    return PostgresMapStore(connection)


@router.get("/layers")
def layer_catalog(store: MapStore = Depends(get_map_store)) -> dict[str, Any]:
    return store.layer_catalog()


@router.get("/layers/{layer_id}", response_model=FeatureCollection)
def read_layer(
    layer_id: str,
    bbox: str | None = Query(default=None, description="minLon,minLat,maxLon,maxLat"),
    detect_id: UUID | None = None,
    fit_id: UUID | None = None,
    limit: int = Query(default=_LAYER_LIMIT_DEFAULT, ge=1, le=_LAYER_LIMIT_MAX),
    store: MapStore = Depends(get_map_store),
) -> dict[str, Any]:
    try:
        parsed_bbox = parse_bbox(bbox)
        return store.read_layer(
            layer_id,
            bbox=parsed_bbox,
            detect_id=str(detect_id) if detect_id else None,
            fit_id=str(fit_id) if fit_id else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MapNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MapDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/replay", response_model=FeatureCollection)
def replay(request: ReplayRequest, store: MapStore = Depends(get_map_store)) -> dict[str, Any]:
    try:
        return store.replay(request)
    except MapNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MapDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/clip", response_model=FeatureCollection)
def clip(request: ClipRequest, store: MapStore = Depends(get_map_store)) -> dict[str, Any]:
    try:
        return store.clip(request)
    except MapNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MapDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _bbox_clause(column: str, bbox: BBox | None) -> tuple[str, tuple[Any, ...]]:
    if bbox is None:
        return "", ()
    return (
        f" AND {column} && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
        tuple(bbox),
    )


def _quoted_identifier(value: str) -> str:
    """Quote the Pipeline-owned table after strict identifier validation."""
    parts = value.split(".")
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None
        for part in parts
    ):
        raise MapDataError(f"invalid trajectory table name in Clean Run: {value!r}")
    return ".".join(f'"{part}"' for part in parts)


def _feature(feature_id: Any, geometry: dict[str, Any], **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": geometry,
        "properties": properties,
    }


def _feature_collection(features: list[dict[str, Any]], **metadata: Any) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "crs": _CRS,
        "features": features,
        "metadata": {"crs": "EPSG:4326", **metadata},
    }


def _geometry(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise MapDataError("database returned an invalid GeoJSON geometry")


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _projection_config(value: Any, detect_id: str) -> dict[str, Any]:
    """Decode persisted projection metadata without accepting malformed JSON."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MapDataError(
                f"Detect Run {detect_id} has invalid projection_config"
            ) from exc
        if isinstance(decoded, dict):
            return decoded
    raise MapDataError(f"Detect Run {detect_id} has invalid projection_config")


def _rows_to_grouped(rows: list[Any]) -> dict[tuple[str, int], list[_TrackPoint]]:
    grouped: dict[tuple[str, int], list[_TrackPoint]] = {}
    for row in rows:
        key = (str(row[0]), int(row[1]))
        grouped.setdefault(key, []).append(
            _TrackPoint(
                point_id=int(row[2]) if row[2] is not None else None,
                timestamp=float(row[3]),
                lon=float(row[4]),
                lat=float(row[5]),
                heading=float(row[6]),
                speed=float(row[7]),
                source_row_key=str(row[8]) if row[8] is not None else None,
                injected=bool(row[9]),
            )
        )
    return grouped


def _complete_tracks(
    rows: list[Any],
    max_points: int,
) -> tuple[dict[tuple[str, int], list[_TrackPoint]], bool]:
    truncated = len(rows) > max_points
    kept_rows = rows[:max_points]
    if truncated and kept_rows:
        last_kept_key = (str(kept_rows[-1][0]), int(kept_rows[-1][1]))
        next_key = (str(rows[max_points][0]), int(rows[max_points][1]))
        if next_key == last_kept_key:
            kept_rows = [
                row
                for row in kept_rows
                if (str(row[0]), int(row[1])) != last_kept_key
            ]
    return _rows_to_grouped(kept_rows), truncated


def _track_feature(
    key: tuple[str, int],
    points: list[_TrackPoint],
    context: _DetectContext | None,
) -> dict[str, Any]:
    geometry_type = "LineString" if len(points) >= 2 else "Point"
    coordinates: Any = [[point.lon, point.lat] for point in points]
    if geometry_type == "Point":
        coordinates = coordinates[0]
    return _feature(
        f"{key[0]}:{key[1]}",
        {"type": geometry_type, "coordinates": coordinates},
        vehicle_id=key[0],
        subtrack_index=key[1],
        detect_id=context.detect_id if context else None,
        timestamps=[point.timestamp for point in points],
        point_ids=[point.point_id for point in points],
        headings=[point.heading for point in points],
        speeds=[point.speed for point in points],
        source_row_keys=[point.source_row_key for point in points],
        injected=[point.injected for point in points],
        point_count=len(points),
    )


def _clip_track(
    key: tuple[str, int],
    points: list[_TrackPoint],
    intersections: list[_Intersection],
    detect_id: str,
    transformer: _CoordinateTransformer,
) -> list[dict[str, Any]]:
    if len(points) < 2:
        return []
    requested = {item.intersection_id for item in intersections}
    events = _crossings(points, intersections, transformer)
    pending_exit: _Crossing | None = None
    features: list[dict[str, Any]] = []
    for event in events:
        if event.kind == "exit":
            pending_exit = event
            continue
        if pending_exit is None:
            continue
        if {pending_exit.intersection_id, event.intersection_id} == requested:
            coordinates = [[pending_exit.lon, pending_exit.lat]]
            interior_start = math.floor(pending_exit.progress + _EPS) + 1
            interior_end = math.ceil(event.progress - _EPS)
            coordinates.extend(
                [point.lon, point.lat]
                for point in points[interior_start:interior_end]
            )
            coordinates.append([event.lon, event.lat])
            if len(coordinates) >= 2 and event.progress - pending_exit.progress > _EPS:
                ordinal = len(features)
                features.append(
                    _feature(
                        f"{detect_id}:{key[0]}:{key[1]}:{ordinal}",
                        {"type": "LineString", "coordinates": coordinates},
                        vehicle_id=key[0],
                        subtrack_index=key[1],
                        from_intersection_id=pending_exit.intersection_id,
                        to_intersection_id=event.intersection_id,
                        start_timestamp=pending_exit.timestamp,
                        end_timestamp=event.timestamp,
                        distance_m=_polyline_distance(coordinates, transformer),
                        point_count=len(coordinates),
                    )
                )
        pending_exit = None
    return features


def _crossings(
    points: list[_TrackPoint],
    intersections: list[_Intersection],
    transformer: _CoordinateTransformer,
) -> list[_Crossing]:
    events: list[_Crossing] = []
    point_coordinates = transformer.forward_many(
        [(point.lon, point.lat) for point in points]
    )
    intersection_coordinates = transformer.forward_many(
        [(intersection.lon, intersection.lat) for intersection in intersections]
    )
    for segment_index, (start, end) in enumerate(zip(points, points[1:])):
        start_x, start_y = point_coordinates[segment_index]
        end_x, end_y = point_coordinates[segment_index + 1]
        for intersection, (center_x, center_y) in zip(
            intersections,
            intersection_coordinates,
            strict=True,
        ):
            relative_start_x = start_x - center_x
            relative_start_y = start_y - center_y
            dx = end_x - start_x
            dy = end_y - start_y
            qa = dx * dx + dy * dy
            if qa <= _EPS:
                continue
            qb = 2.0 * (relative_start_x * dx + relative_start_y * dy)
            qc = (
                relative_start_x * relative_start_x
                + relative_start_y * relative_start_y
                - intersection.radius_m * intersection.radius_m
            )
            discriminant = qb * qb - 4.0 * qa * qc
            if discriminant <= _EPS:
                continue
            root = math.sqrt(discriminant)
            fractions = sorted(((-qb - root) / (2.0 * qa), (-qb + root) / (2.0 * qa)))
            for raw_fraction in fractions:
                if not (-_EPS <= raw_fraction <= 1.0 + _EPS):
                    continue
                fraction = min(1.0, max(0.0, raw_fraction))
                radial_velocity = (
                    (relative_start_x + fraction * dx) * dx
                    + (relative_start_y + fraction * dy) * dy
                )
                if abs(radial_velocity) <= _EPS:
                    continue
                crossing_x = start_x + fraction * dx
                crossing_y = start_y + fraction * dy
                crossing_lon, crossing_lat = transformer.inverse(
                    crossing_x,
                    crossing_y,
                )
                events.append(
                    _Crossing(
                        progress=segment_index + fraction,
                        kind="entry" if radial_velocity < 0.0 else "exit",
                        intersection_id=intersection.intersection_id,
                        segment_index=segment_index,
                        fraction=fraction,
                        lon=crossing_lon,
                        lat=crossing_lat,
                        timestamp=start.timestamp + fraction * (end.timestamp - start.timestamp),
                    )
                )
    events.sort(key=lambda item: (item.progress, item.intersection_id, item.kind))
    deduplicated: list[_Crossing] = []
    for event in events:
        if deduplicated:
            previous = deduplicated[-1]
            if (
                previous.intersection_id == event.intersection_id
                and abs(previous.progress - event.progress) <= _EPS
            ):
                if previous.kind != event.kind:
                    deduplicated.pop()
                continue
        deduplicated.append(event)
    return deduplicated


def _polyline_distance(
    coordinates: list[list[float]],
    transformer: _CoordinateTransformer,
) -> float:
    projected = transformer.forward_many(
        [(coordinate[0], coordinate[1]) for coordinate in coordinates]
    )
    return sum(
        math.hypot(right[0] - left[0], right[1] - left[1])
        for left, right in zip(projected, projected[1:])
    )
