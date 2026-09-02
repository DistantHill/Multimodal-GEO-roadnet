from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

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
_EARTH_RADIUS_M = 6_371_008.8
_EPS = 1e-9


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
    target_table: str = "trajectory_points"


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
            for feature in _clip_track(key, points, intersections, context.detect_id):
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
                "COALESCE(c.parameters->>'target_table', 'trajectory_points') "
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
        return _DetectContext(str(row[0]), str(row[1]), target_table)

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
) -> list[dict[str, Any]]:
    if len(points) < 2:
        return []
    requested = {item.intersection_id for item in intersections}
    events = _crossings(points, intersections)
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
                        distance_m=_polyline_distance(coordinates),
                        point_count=len(coordinates),
                    )
                )
        pending_exit = None
    return features


def _crossings(
    points: list[_TrackPoint],
    intersections: list[_Intersection],
) -> list[_Crossing]:
    events: list[_Crossing] = []
    for segment_index, (start, end) in enumerate(zip(points, points[1:])):
        for intersection in intersections:
            start_xy = _relative_xy(start.lon, start.lat, intersection.lon, intersection.lat)
            end_xy = _relative_xy(end.lon, end.lat, intersection.lon, intersection.lat)
            dx = end_xy[0] - start_xy[0]
            dy = end_xy[1] - start_xy[1]
            qa = dx * dx + dy * dy
            if qa <= _EPS:
                continue
            qb = 2.0 * (start_xy[0] * dx + start_xy[1] * dy)
            qc = start_xy[0] ** 2 + start_xy[1] ** 2 - intersection.radius_m ** 2
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
                    (start_xy[0] + fraction * dx) * dx
                    + (start_xy[1] + fraction * dy) * dy
                )
                if abs(radial_velocity) <= _EPS:
                    continue
                events.append(
                    _Crossing(
                        progress=segment_index + fraction,
                        kind="entry" if radial_velocity < 0.0 else "exit",
                        intersection_id=intersection.intersection_id,
                        segment_index=segment_index,
                        fraction=fraction,
                        lon=start.lon + fraction * (end.lon - start.lon),
                        lat=start.lat + fraction * (end.lat - start.lat),
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


def _relative_xy(lon: float, lat: float, origin_lon: float, origin_lat: float) -> tuple[float, float]:
    return (
        math.radians(lon - origin_lon) * _EARTH_RADIUS_M * math.cos(math.radians(origin_lat)),
        math.radians(lat - origin_lat) * _EARTH_RADIUS_M,
    )


def _polyline_distance(coordinates: list[list[float]]) -> float:
    distance = 0.0
    for left, right in zip(coordinates, coordinates[1:]):
        lat1 = math.radians(left[1])
        lat2 = math.radians(right[1])
        delta_lat = lat2 - lat1
        delta_lon = math.radians(right[0] - left[0])
        value = (
            math.sin(delta_lat / 2.0) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
        )
        distance += 2.0 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))
    return distance
