"""Read-only map, replay, and Clip projections for schema v3.

The Backend never reconstructs Detect geometry. In particular, Clip consumes
the final ``resolved_subtrack_points`` order and its persisted crossing rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg
from pyproj import CRS, Proj, Transformer

from .database import get_read_connection, open_read_connection, require_schema_v3
from .map_schemas import BBox, ClipRequest, FeatureCollection, ReplayRequest, parse_bbox


router = APIRouter(prefix="/api/v1/map", tags=["map"])

_CRS = {"type": "name", "properties": {"name": "EPSG:4326"}}
_LAYER_LIMIT_DEFAULT = 1_000
_LAYER_LIMIT_MAX = 5_000
_CLIP_FEATURE_LIMIT = 1_000
_EPS = 1e-9
_COORDINATE_SEMANTICS_VERSION = 2
_SOURCE_KEY_TYPES = {
    "int2": "smallint",
    "int4": "integer",
    "int8": "bigint",
    "uuid": "uuid",
    "text": "text",
    "varchar": "varchar",
}


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
class _CleanContext:
    clean_id: str
    source_table: str
    quoted_source_table: str
    source_key_sql_type: str
    source_fingerprint: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _DetectContext:
    detect_id: str
    clean: _CleanContext
    source_srid: int
    processing_srid: int
    projection_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Intersection:
    intersection_id: int
    lon: float
    lat: float
    radius_m: float


@dataclass(frozen=True, slots=True)
class _TrackPoint:
    resolved_point_id: int | None
    source_row_key: str | None
    lon: float
    lat: float
    point_kind: str
    crossing_id: int | None = None
    intersection_id: int | None = None
    crossing_type: int | None = None


class _CoordinateTransformer:
    """Validate and apply the same fixed-CRS contract as Pipeline."""

    def __init__(self, context: _DetectContext) -> None:
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


_LAYER_DEFINITIONS = (
    {
        "id": "resolved_subtrack_points",
        "title": "Resolved subtrack points",
        "role": "trajectory",
        "geometry_type": "Point",
        "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
    {
        "id": "intersections",
        "title": "Verified intersections",
        "role": "intersection",
        "geometry_type": "Polygon",
        "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
    {
        "id": "entry_exit_points",
        "title": "Verified entry/exit crossings",
        "role": "point",
        "geometry_type": "Point",
        "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
    {
        "id": "centerlines",
        "title": "Fitted centerlines",
        "role": "road",
        "geometry_type": "LineString",
        "required_context": ["fit_id"],
        "default_context": "latest completed Fit Run",
    },
)


class PostgresMapStore:
    """Bounded, read-only projections over Pipeline schema v3."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def layer_catalog(self) -> dict[str, Any]:
        row = self._fetchone(
            "SELECT "
            "(SELECT d.id::text FROM detect_runs AS d WHERE d.status = 'completed' "
            " ORDER BY d.finished_at DESC NULLS LAST, d.started_at DESC LIMIT 1), "
            "(SELECT f.id::text FROM fit_runs AS f WHERE f.status = 'completed' "
            " ORDER BY f.finished_at DESC NULLS LAST, f.started_at DESC LIMIT 1), "
            "(SELECT f.detect_id::text FROM fit_runs AS f WHERE f.status = 'completed' "
            " ORDER BY f.finished_at DESC NULLS LAST, f.started_at DESC LIMIT 1)",
            (),
        )
        return {
            "crs": "EPSG:4326",
            "layers": [dict(item) for item in _LAYER_DEFINITIONS],
            "default_context": {
                "detect_id": str(row[0]) if row and row[0] is not None else None,
                "fit_id": str(row[1]) if row and row[1] is not None else None,
                "fit_detect_id": str(row[2]) if row and row[2] is not None else None,
            },
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
        if layer_id == "resolved_subtrack_points":
            context = self._resolve_detect(detect_id)
            rows = self._resolved_point_layer(context, bbox, limit + 1)
            features = [
                self._resolved_point_feature(row, context.detect_id)
                for row in rows[:limit]
            ]
            metadata = {"detect_id": context.detect_id, "clean_id": context.clean.clean_id}
        elif layer_id == "intersections":
            context = self._resolve_detect(detect_id)
            rows = self._intersection_layer(context.detect_id, bbox, limit + 1)
            features = [
                self._intersection_feature(row, context.detect_id)
                for row in rows[:limit]
            ]
            metadata = {"detect_id": context.detect_id, "clean_id": context.clean.clean_id}
        elif layer_id == "entry_exit_points":
            context = self._resolve_detect(detect_id)
            rows = self._entry_exit_layer(context.detect_id, bbox, limit + 1)
            features = [
                self._entry_exit_feature(row, context.detect_id)
                for row in rows[:limit]
            ]
            metadata = {"detect_id": context.detect_id, "clean_id": context.clean.clean_id}
        elif layer_id == "centerlines":
            resolved_fit_id, resolved_detect_id = self._resolve_fit(fit_id)
            if detect_id is not None and str(detect_id) != resolved_detect_id:
                raise MapNotFoundError(
                    f"completed Fit Run {resolved_fit_id} does not belong to Detect Run {detect_id}"
                )
            rows = self._centerline_layer(resolved_fit_id, bbox, limit + 1)
            features = [self._centerline_feature(row) for row in rows[:limit]]
            metadata = {"fit_id": resolved_fit_id, "detect_id": resolved_detect_id}
        else:
            raise MapNotFoundError(f"unknown map layer: {layer_id}")
        return _feature_collection(
            features,
            layer=layer_id,
            count=len(features),
            truncated=len(rows) > limit,
            bbox=list(bbox) if bbox is not None else None,
            **metadata,
        )

    def replay(self, request: ReplayRequest) -> dict[str, Any]:
        if request.database_name is not None:
            return self._query_database("replay", request)
        self._validate_database(request.database_name)
        if request.intersection_id is not None:
            from .intersection_replay import replay_intersection

            return replay_intersection(self, request)
        detect = (
            self._resolve_detect(str(request.detect_id))
            if request.detect_id is not None
            else None
        )
        clean = detect.clean if detect is not None else self._resolve_clean(None)
        self._verify_source_fingerprint(clean)
        if request.resolved_point_ids is not None:
            assert detect is not None
            selection = "resolved_point_ids"
            selected, found, vehicles_truncated = self._select_resolved_tracks(
                detect,
                resolved_point_ids=request.resolved_point_ids,
                source_row_keys=None,
                bbox=None,
                max_vehicles=request.max_vehicles,
            )
            missing: list[int] | list[str] | None = sorted(
                set(request.resolved_point_ids) - {int(value) for value in found}
            )
        elif request.source_row_keys is not None:
            selection = "source_row_keys"
            if detect is not None:
                selected, found, vehicles_truncated = self._select_resolved_tracks(
                    detect,
                    resolved_point_ids=None,
                    source_row_keys=request.source_row_keys,
                    bbox=None,
                    max_vehicles=request.max_vehicles,
                )
            else:
                selected, found, vehicles_truncated = self._select_clean_tracks(
                    clean,
                    source_row_keys=request.source_row_keys,
                    bbox=None,
                    max_vehicles=request.max_vehicles,
                )
            missing = sorted(set(request.source_row_keys) - {str(value) for value in found})
        else:
            assert request.bbox is not None
            selection = "bbox"
            missing = None
            if detect is not None:
                selected, _, vehicles_truncated = self._select_resolved_tracks(
                    detect,
                    resolved_point_ids=None,
                    source_row_keys=None,
                    bbox=request.bbox,
                    max_vehicles=request.max_vehicles,
                )
            else:
                selected, _, vehicles_truncated = self._select_clean_tracks(
                    clean,
                    source_row_keys=None,
                    bbox=request.bbox,
                    max_vehicles=request.max_vehicles,
                )

        if not selected:
            raise MapNotFoundError(f"no replay points match {selection}")
        rows = (
            self._fetch_resolved_tracks(detect, selected, request.max_points + 1)
            if detect is not None
            else self._fetch_clean_tracks(clean, selected, request.max_points + 1)
        )
        grouped, points_truncated = _complete_tracks(rows, request.max_points)
        features = [
            _track_feature(key, points, detect.detect_id if detect else None)
            for key, points in grouped.items()
            if points
        ]
        return _feature_collection(
            features,
            operation="replay",
            selection=selection,
            detect_id=detect.detect_id if detect else None,
            clean_id=clean.clean_id,
            selected_track_count=len(selected),
            returned_track_count=len(features),
            returned_point_count=sum(len(points) for points in grouped.values()),
            missing_source_row_keys=missing if selection == "source_row_keys" else None,
            missing_resolved_point_ids=(
                missing if selection == "resolved_point_ids" else None
            ),
            truncated=vehicles_truncated or points_truncated,
        )

    def clip(self, request: ClipRequest) -> dict[str, Any]:
        if request.database_name is not None:
            return self._query_database("clip", request)
        self._validate_database(request.database_name)
        context = self._resolve_detect(str(request.detect_id))
        self._verify_source_fingerprint(context.clean)
        requested = [int(value) for value in request.intersection_ids]
        all_intersections = self._fetch_intersections(context.detect_id)
        existing = {item.intersection_id for item in all_intersections}
        missing = sorted(set(requested) - existing)
        if missing:
            raise MapNotFoundError(
                f"completed intersections not found in Detect Run {context.detect_id}: {missing}"
            )
        selected, vehicles_truncated = self._select_clip_tracks(
            context,
            requested,
            request.max_vehicles,
        )
        rows = self._fetch_resolved_tracks(context, selected, request.max_points + 1)
        grouped, points_truncated = _complete_tracks(rows, request.max_points)
        transformer = _CoordinateTransformer(context)
        features: list[dict[str, Any]] = []
        clips_truncated = False
        for key, points in grouped.items():
            for feature in _clip_track(
                key,
                points,
                all_intersections,
                set(requested),
                context.detect_id,
                transformer,
            ):
                if len(features) >= _CLIP_FEATURE_LIMIT:
                    clips_truncated = True
                    break
                features.append(feature)
            if clips_truncated:
                break
        return _feature_collection(
            features,
            operation="clip",
            detect_id=context.detect_id,
            clean_id=context.clean.clean_id,
            intersection_ids=requested,
            selected_track_count=len(selected),
            returned_clip_count=len(features),
            returned_point_count=sum(
                int(feature["properties"]["point_count"]) for feature in features
            ),
            truncated=vehicles_truncated or points_truncated or clips_truncated,
        )

    def _validate_database(self, database_name: str | None) -> None:
        if database_name is None:
            return  # Existing Web clients use the configured Backend database.
        row = self._fetchone(
            "SELECT current_database(), EXISTS(SELECT 1 FROM pg_database WHERE datname = %s)",
            (database_name,),
        )
        actual, exists = row
        if not exists:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "database_not_found",
                    "message": (
                        f"所选图层数据库 {database_name!r} "
                        + "在后端 PostgreSQL 中不存在。"
                        + f"后端当前数据库为 {actual!r}，请确认 QGIS 图层的 database 是否对应。"
                    ),
                },
            )

    def _query_database(self, operation: str, request: ReplayRequest | ClipRequest) -> dict[str, Any]:
        self._validate_database(request.database_name)
        try:
            with open_read_connection(request.database_name) as connection:
                try:
                    require_schema_v3(connection)
                except RuntimeError as exc:
                    raise HTTPException(status_code=409, detail={
                        "error": "database_schema_mismatch",
                        "message": "所选数据库不是兼容的 schema v3，请确认 QGIS 图层的 database 是否对应。",
                    }) from exc
                result = getattr(PostgresMapStore(connection), operation)(
                    request.model_copy(update={"database_name": None})
                )
                result.setdefault("metadata", {})["database_name"] = request.database_name
                return result
        except psycopg.Error as exc:
            raise HTTPException(status_code=503, detail={
                "error": "database_query_failed",
                "message": "所选数据库连接或查询失败，请确认访问权限及 QGIS 图层的 database 是否对应。",
            }) from exc

    def _resolve_detect(self, detect_id: str | None) -> _DetectContext:
        clause = "d.id = %s" if detect_id else "TRUE"
        order = "" if detect_id else " ORDER BY d.finished_at DESC NULLS LAST, d.started_at DESC"
        row = self._fetchone(
            "SELECT d.id::text, c.id::text, c.source_table, c.source_fingerprint, "
            "c.source_srid, c.processing_srid, c.projection_config "
            "FROM detect_runs AS d JOIN clean_runs AS c ON c.id = d.clean_id "
            f"WHERE d.status = 'completed' AND c.status = 'completed' AND {clause}"
            + order
            + " LIMIT 1",
            (detect_id,) if detect_id else (),
        )
        if row is None:
            if detect_id:
                raise MapNotFoundError(f"unknown completed Detect Run: {detect_id}")
            raise MapNotFoundError("no completed Detect Run is available")
        clean = self._clean_context(str(row[1]), str(row[2]), row[3])
        if row[5] is None:
            raise MapDataError(f"Detect Run {row[0]} has no processing CRS")
        return _DetectContext(
            detect_id=str(row[0]),
            clean=clean,
            source_srid=int(row[4]),
            processing_srid=int(row[5]),
            projection_config=_json_object(row[6], "projection_config"),
        )

    def _resolve_clean(self, clean_id: str | None) -> _CleanContext:
        clause = "id = %s" if clean_id else "TRUE"
        order = "" if clean_id else " ORDER BY finished_at DESC NULLS LAST, started_at DESC"
        row = self._fetchone(
            "SELECT id::text, source_table, source_fingerprint FROM clean_runs "
            f"WHERE status = 'completed' AND {clause}" + order + " LIMIT 1",
            (clean_id,) if clean_id else (),
        )
        if row is None:
            raise MapNotFoundError("no completed Clean Run is available")
        return self._clean_context(str(row[0]), str(row[1]), row[2])

    def _clean_context(
        self,
        clean_id: str,
        source_table: str,
        source_fingerprint: Any,
    ) -> _CleanContext:
        quoted = _quoted_identifier(source_table)
        row = self._fetchone(
            "SELECT type.typname FROM pg_attribute AS attribute "
            "JOIN pg_type AS type ON type.oid = attribute.atttypid "
            "WHERE attribute.attrelid = %s::regclass AND attribute.attname = 'id' "
            "AND attribute.attnum > 0 AND NOT attribute.attisdropped",
            (source_table,),
        )
        if row is None or str(row[0]) not in _SOURCE_KEY_TYPES:
            raise MapDataError(f"unsupported source key type for {source_table}")
        return _CleanContext(
            clean_id=clean_id,
            source_table=source_table,
            quoted_source_table=quoted,
            source_key_sql_type=_SOURCE_KEY_TYPES[str(row[0])],
            source_fingerprint=_json_object(source_fingerprint, "source_fingerprint"),
        )

    def _resolve_fit(self, fit_id: str | None) -> tuple[str, str]:
        clause = "f.id = %s" if fit_id else "TRUE"
        order = "" if fit_id else " ORDER BY f.finished_at DESC NULLS LAST, f.started_at DESC"
        row = self._fetchone(
            "SELECT f.id::text, f.detect_id::text FROM fit_runs AS f "
            f"WHERE f.status = 'completed' AND {clause}" + order + " LIMIT 1",
            (fit_id,) if fit_id else (),
        )
        if row is None:
            if fit_id:
                raise MapNotFoundError(f"unknown completed Fit Run: {fit_id}")
            raise MapNotFoundError("no completed Fit Run is available")
        return str(row[0]), str(row[1])

    def _verify_source_fingerprint(self, clean: _CleanContext) -> None:
        expected = clean.source_fingerprint
        if not expected:
            raise MapDataError(f"Clean Run {clean.clean_id} has no source fingerprint")
        extra_row = self._fetchone(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE "
            "attrelid = %s::regclass AND attname = 'extra' "
            "AND attnum > 0 AND NOT attisdropped)",
            (clean.source_table,),
        )
        has_extra = bool(extra_row[0])
        extra_sql = "source.extra" if has_extra else "'{}'::jsonb"
        sql = f"""
            WITH referenced(source_row_key) AS (
                SELECT source_row_key FROM kept_trajectory_points WHERE clean_id = %s
                UNION
                SELECT source_row_key FROM excluded_points WHERE clean_id = %s
            )
            SELECT referenced.source_row_key, source.id IS NOT NULL,
                   source.vehicle_id, source.timestamp, source.lon, source.lat,
                   source.heading, source.speed, {extra_sql}
              FROM referenced
              LEFT JOIN {clean.quoted_source_table} AS source
                ON source.id = referenced.source_row_key::{clean.source_key_sql_type}
             ORDER BY referenced.source_row_key
        """
        digest = hashlib.sha256()
        count = 0
        for row in self._fetchall(sql, (clean.clean_id, clean.clean_id)):
            if not bool(row[1]):
                raise MapDataError(
                    f"Clean Run {clean.clean_id} references a missing source row"
                )
            canonical = {
                "source_row_key": str(row[0]),
                "vehicle_id": str(row[2]),
                "timestamp": row[3],
                "lon": row[4],
                "lat": row[5],
                "heading": row[6],
                "speed": row[7],
                "extra": row[8] if isinstance(row[8], dict) else {},
            }
            encoded = json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        actual = {"version": 1, "row_count": count, "sha256": digest.hexdigest()}
        if actual != expected:
            raise MapDataError(
                f"source rows for Clean Run {clean.clean_id} changed; replay is refused"
            )

    def _resolved_point_layer(
        self,
        context: _DetectContext,
        bbox: BBox | None,
        limit: int,
    ) -> list[Any]:
        base = self._resolved_base_sql(context.clean)
        bbox_clause, bbox_params = _bbox_clause("resolved.geom", bbox)
        return self._fetchall(
            base
            + " SELECT subtrack_index, vehicle_id, resolved_point_id, source_row_key, "
            "point_kind, crossing_id, intersection_id, crossing_type, "
            "ST_AsGeoJSON(geom)::jsonb FROM resolved WHERE detect_id = %s"
            + bbox_clause
            + " ORDER BY subtrack_index, sequence_in_subtrack LIMIT %s",
            (context.detect_id, context.detect_id, *bbox_params, limit),
        )

    def _intersection_layer(self, detect_id: str, bbox: BBox | None, limit: int) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("intersection.geom", bbox)
        return self._fetchall(
            "SELECT intersection.id, candidate.centroid_lon, candidate.centroid_lat, "
            "intersection.clip_radius_m, intersection.branch_centers_deg, "
            "intersection.transition_counts, intersection.observation_count, "
            "candidate.contributors, candidate.critical_contributors, "
            "ST_AsGeoJSON(intersection.geom)::jsonb "
            "FROM intersections AS intersection JOIN candidate_intersections AS candidate "
            "ON candidate.id = intersection.candidate_intersection_id "
            "AND candidate.detect_id = intersection.detect_id "
            "WHERE intersection.detect_id = %s"
            + bbox_clause
            + " ORDER BY intersection.id LIMIT %s",
            (detect_id, *bbox_params, limit),
        )

    def _entry_exit_layer(self, detect_id: str, bbox: BBox | None, limit: int) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("crossing.geom", bbox)
        return self._fetchall(
            "SELECT crossing.id, intersection.id, resolved.id, resolved.subtrack_index, "
            "crossing.crossing_type, ST_AsGeoJSON(crossing.geom)::jsonb "
            "FROM candidate_entry_exit_points AS crossing "
            "JOIN intersections AS intersection ON "
            "intersection.detect_id = crossing.detect_id AND "
            "intersection.candidate_intersection_id = crossing.candidate_intersection_id "
            "JOIN resolved_subtrack_points AS resolved ON "
            "resolved.detect_id = crossing.detect_id AND "
            "resolved.crossing_point_id = crossing.id "
            "WHERE crossing.detect_id = %s"
            + bbox_clause
            + " ORDER BY resolved.subtrack_index, resolved.sequence_in_subtrack LIMIT %s",
            (detect_id, *bbox_params, limit),
        )

    def _centerline_layer(self, fit_id: str, bbox: BBox | None, limit: int) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("centerline.geom", bbox)
        return self._fetchall(
            "SELECT centerline.id, centerline.detect_id::text, centerline.variant, "
            "centerline.from_intersection_id, centerline.to_intersection_id, "
            "centerline.length_m, centerline.forward_clip_count, "
            "centerline.reverse_clip_count, ST_AsGeoJSON(centerline.geom)::jsonb "
            "FROM centerlines AS centerline WHERE centerline.fit_id = %s"
            + bbox_clause
            + " ORDER BY centerline.id LIMIT %s",
            (fit_id, *bbox_params, limit),
        )

    def _select_resolved_tracks(
        self,
        context: _DetectContext,
        *,
        resolved_point_ids: list[int] | None,
        source_row_keys: list[str] | None,
        bbox: BBox | None,
        max_vehicles: int,
    ) -> tuple[list[int], set[int | str], bool]:
        base = self._resolved_base_sql(context.clean)
        params: list[Any] = [context.detect_id, context.detect_id]
        if resolved_point_ids is not None:
            predicate = "resolved_point_id = ANY(%s::bigint[])"
            params.append(resolved_point_ids)
            identity = "resolved_point_id"
        elif source_row_keys is not None:
            predicate = "source_row_key = ANY(%s::text[])"
            params.append(source_row_keys)
            identity = "source_row_key"
        else:
            assert bbox is not None
            predicate = "geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
            params.extend(bbox)
            identity = "NULL"
        rows = self._fetchall(
            base
            + f" SELECT DISTINCT vehicle_id, subtrack_index, {identity} "
            "FROM resolved WHERE detect_id = %s AND "
            + predicate
            + " ORDER BY vehicle_id, subtrack_index",
            tuple(params),
        )
        return _limit_selected_tracks(rows, max_vehicles)

    def _select_clean_tracks(
        self,
        clean: _CleanContext,
        *,
        source_row_keys: list[str] | None,
        bbox: BBox | None,
        max_vehicles: int,
    ) -> tuple[list[int], set[int | str], bool]:
        if source_row_keys is not None:
            rows = self._fetchall(
                "SELECT DISTINCT kept.vehicle_id, kept.subtrack_index, kept.source_row_key "
                "FROM kept_trajectory_points AS kept WHERE kept.clean_id = %s "
                "AND kept.source_row_key = ANY(%s::text[]) "
                "ORDER BY kept.vehicle_id, kept.subtrack_index",
                (clean.clean_id, source_row_keys),
            )
        else:
            assert bbox is not None
            rows = self._fetchall(
                "SELECT vehicle_id, subtrack_index, NULL::text FROM ("
                "SELECT kept.vehicle_id, kept.subtrack_index "
                f"FROM kept_trajectory_points AS kept JOIN {clean.quoted_source_table} AS source "
                f"ON source.id = kept.source_row_key::{clean.source_key_sql_type} "
                "WHERE kept.clean_id = %s AND source.geom && "
                "ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
                "UNION SELECT injected.vehicle_id, injected.subtrack_index "
                "FROM injected_points AS injected WHERE injected.clean_id = %s "
                "AND injected.geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
                ") AS matching ORDER BY vehicle_id, subtrack_index",
                (clean.clean_id, *bbox, clean.clean_id, *bbox),
            )
        return _limit_selected_tracks(rows, max_vehicles)

    def _select_clip_tracks(
        self,
        context: _DetectContext,
        intersection_ids: list[int],
        max_vehicles: int,
    ) -> tuple[list[int], bool]:
        rows = self._fetchall(
            "WITH subtrack_vehicle AS ("
            " SELECT resolved.subtrack_index, MIN(COALESCE(kept.vehicle_id, injected.vehicle_id)) AS vehicle_id "
            " FROM resolved_subtrack_points AS resolved "
            " LEFT JOIN kept_trajectory_points AS kept ON kept.id = resolved.kept_point_id "
            " LEFT JOIN injected_points AS injected ON injected.id = resolved.injected_point_id "
            " WHERE resolved.detect_id = %s GROUP BY resolved.subtrack_index"
            "), matching AS ("
            " SELECT resolved.subtrack_index FROM resolved_subtrack_points AS resolved "
            " JOIN candidate_entry_exit_points AS crossing ON "
            " crossing.id = resolved.crossing_point_id AND crossing.detect_id = resolved.detect_id "
            " JOIN intersections AS intersection ON "
            " intersection.detect_id = crossing.detect_id AND "
            " intersection.candidate_intersection_id = crossing.candidate_intersection_id "
            " WHERE resolved.detect_id = %s AND intersection.id = ANY(%s::bigint[]) "
            " GROUP BY resolved.subtrack_index HAVING COUNT(DISTINCT intersection.id) = 2"
            ") SELECT vehicle.vehicle_id, matching.subtrack_index, NULL::bigint "
            "FROM matching JOIN subtrack_vehicle AS vehicle USING (subtrack_index) "
            "ORDER BY vehicle.vehicle_id, matching.subtrack_index",
            (context.detect_id, context.detect_id, intersection_ids),
        )
        selected, _, truncated = _limit_selected_tracks(rows, max_vehicles)
        return selected, truncated

    def _fetch_resolved_tracks(
        self,
        context: _DetectContext,
        subtrack_indexes: list[int],
        limit: int,
    ) -> list[Any]:
        if not subtrack_indexes:
            return []
        base = self._resolved_base_sql(context.clean)
        return self._fetchall(
            base
            + " SELECT subtrack_index, vehicle_id, resolved_point_id, source_row_key, "
            "lon, lat, point_kind, crossing_id, intersection_id, crossing_type "
            "FROM resolved WHERE detect_id = %s AND subtrack_index = ANY(%s::integer[]) "
            "ORDER BY subtrack_index, sequence_in_subtrack LIMIT %s",
            (context.detect_id, context.detect_id, subtrack_indexes, limit),
        )

    def _fetch_clean_tracks(
        self,
        clean: _CleanContext,
        subtrack_indexes: list[int],
        limit: int,
    ) -> list[Any]:
        if not subtrack_indexes:
            return []
        return self._fetchall(
            "SELECT subtrack_index, vehicle_id, NULL::bigint, source_row_key, "
            "lon, lat, point_kind, NULL::bigint, NULL::bigint, NULL::smallint FROM ("
            " SELECT kept.subtrack_index, kept.vehicle_id, kept.source_row_key, "
            " source.lon, source.lat, 'kept'::text AS point_kind, "
            " kept.sequence_in_subtrack AS anchor_sequence, 0 AS kind_order, kept.id AS row_id "
            f" FROM kept_trajectory_points AS kept JOIN {clean.quoted_source_table} AS source "
            f" ON source.id = kept.source_row_key::{clean.source_key_sql_type} "
            " WHERE kept.clean_id = %s AND kept.subtrack_index = ANY(%s::integer[]) "
            " UNION ALL SELECT injected.subtrack_index, injected.vehicle_id, NULL::text, "
            " injected.lon, injected.lat, 'injected', injected.after_sequence_in_subtrack, "
            " 1, injected.id FROM injected_points AS injected WHERE injected.clean_id = %s "
            " AND injected.subtrack_index = ANY(%s::integer[])"
            ") AS clean_points ORDER BY subtrack_index, anchor_sequence, kind_order, row_id LIMIT %s",
            (clean.clean_id, subtrack_indexes, clean.clean_id, subtrack_indexes, limit),
        )

    def _resolved_base_sql(self, clean: _CleanContext) -> str:
        return f"""
            WITH subtrack_vehicle AS (
                SELECT resolved.subtrack_index,
                       MIN(COALESCE(kept.vehicle_id, injected.vehicle_id)) AS vehicle_id
                  FROM resolved_subtrack_points AS resolved
                  LEFT JOIN kept_trajectory_points AS kept
                    ON kept.id = resolved.kept_point_id
                  LEFT JOIN injected_points AS injected
                    ON injected.id = resolved.injected_point_id
                 WHERE resolved.detect_id = %s
                 GROUP BY resolved.subtrack_index
            ), resolved AS (
                SELECT point.detect_id, point.subtrack_index,
                       point.sequence_in_subtrack, vehicle.vehicle_id,
                       point.id AS resolved_point_id, kept.source_row_key,
                       COALESCE(source.lon, injected.lon, crossing.crossing_lon) AS lon,
                       COALESCE(source.lat, injected.lat, crossing.crossing_lat) AS lat,
                       COALESCE(source.geom, injected.geom, crossing.geom) AS geom,
                       CASE WHEN point.kept_point_id IS NOT NULL THEN 'kept'
                            WHEN point.injected_point_id IS NOT NULL THEN 'injected'
                            ELSE 'crossing' END AS point_kind,
                       crossing.id AS crossing_id,
                       intersection.id AS intersection_id,
                       crossing.crossing_type
                  FROM resolved_subtrack_points AS point
                  JOIN subtrack_vehicle AS vehicle USING (subtrack_index)
                  LEFT JOIN kept_trajectory_points AS kept
                    ON kept.id = point.kept_point_id
                  LEFT JOIN {clean.quoted_source_table} AS source
                    ON source.id = kept.source_row_key::{clean.source_key_sql_type}
                  LEFT JOIN injected_points AS injected
                    ON injected.id = point.injected_point_id
                  LEFT JOIN candidate_entry_exit_points AS crossing
                    ON crossing.id = point.crossing_point_id
                   AND crossing.detect_id = point.detect_id
                  LEFT JOIN intersections AS intersection
                    ON intersection.detect_id = crossing.detect_id
                   AND intersection.candidate_intersection_id = crossing.candidate_intersection_id
            )
        """

    def _fetch_intersections(self, detect_id: str) -> list[_Intersection]:
        rows = self._fetchall(
            "SELECT intersection.id, candidate.centroid_lon, candidate.centroid_lat, "
            "intersection.clip_radius_m FROM intersections AS intersection "
            "JOIN candidate_intersections AS candidate ON "
            "candidate.id = intersection.candidate_intersection_id "
            "AND candidate.detect_id = intersection.detect_id "
            "WHERE intersection.detect_id = %s ORDER BY intersection.id",
            (detect_id,),
        )
        return [
            _Intersection(int(row[0]), float(row[1]), float(row[2]), float(row[3]))
            for row in rows
        ]

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    @staticmethod
    def _resolved_point_feature(row: Any, detect_id: str) -> dict[str, Any]:
        return _feature(
            int(row[2]),
            _geometry(row[8]),
            detect_id=detect_id,
            vehicle_id=str(row[1]),
            subtrack_index=int(row[0]),
            resolved_point_id=int(row[2]),
            source_row_key=str(row[3]) if row[3] is not None else None,
            point_kind=str(row[4]),
            crossing_id=int(row[5]) if row[5] is not None else None,
            intersection_id=int(row[6]) if row[6] is not None else None,
            crossing_type=int(row[7]) if row[7] is not None else None,
        )

    @staticmethod
    def _intersection_feature(row: Any, detect_id: str) -> dict[str, Any]:
        return _feature(
            int(row[0]),
            _geometry(row[9]),
            detect_id=detect_id,
            intersection_id=int(row[0]),
            centroid=[float(row[1]), float(row[2])],
            clip_radius_m=float(row[3]),
            branch_centers_deg=_json_value(row[4], []),
            transition_counts=_json_value(row[5], {}),
            observation_count=int(row[6]),
            contributors=_json_value(row[7], {}),
            critical_contributors=_json_value(row[8], []),
        )

    @staticmethod
    def _entry_exit_feature(row: Any, detect_id: str) -> dict[str, Any]:
        crossing_type = int(row[4])
        return _feature(
            int(row[0]),
            _geometry(row[5]),
            detect_id=detect_id,
            crossing_id=int(row[0]),
            intersection_id=int(row[1]),
            resolved_point_id=int(row[2]),
            source_row_key=None,
            subtrack_index=int(row[3]),
            crossing_type=crossing_type,
            crossing_kind="entry" if crossing_type == 1 else "exit",
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


def get_map_store(
    connection: Any = Depends(get_read_connection),
    database_name: str | None = Query(default=None, min_length=1, max_length=63),
):
    store = PostgresMapStore(connection)
    if database_name is None:
        yield store
        return
    store._validate_database(database_name)
    try:
        with open_read_connection(database_name) as target:
            try:
                require_schema_v3(target)
            except RuntimeError as exc:
                raise HTTPException(409, detail="所选数据库不兼容 schema v3，请选择业务数据库。") from exc
            yield PostgresMapStore(target)
    except psycopg.Error as exc:
        raise HTTPException(503, detail="所选数据库连接或查询失败，请检查权限。") from exc


@router.get("/layers")
def layer_catalog(store: MapStore = Depends(get_map_store)) -> dict[str, Any]:
    return store.layer_catalog()


@router.get("/runs")
def run_catalog(store: Any = Depends(get_map_store)) -> dict[str, Any]:
    return {
        "clean": [{"id": row[0], "started_at": row[1]} for row in store._fetchall(
            "SELECT id::text, started_at FROM clean_runs WHERE status = 'completed' ORDER BY finished_at DESC NULLS LAST, started_at DESC", ()
        )],
        "detect": [{"id": row[0], "clean_id": row[1], "started_at": row[2]} for row in store._fetchall(
            "SELECT id::text, clean_id::text, started_at FROM detect_runs WHERE status = 'completed' ORDER BY finished_at DESC NULLS LAST, started_at DESC", ()
        )],
        "fit": [{"id": row[0], "detect_id": row[1], "started_at": row[2]} for row in store._fetchall(
            "SELECT id::text, detect_id::text, started_at FROM fit_runs WHERE status = 'completed' ORDER BY finished_at DESC NULLS LAST, started_at DESC", ()
        )],
    }


@router.get("/layers/{layer_id}", response_model=FeatureCollection)
def read_layer(
    layer_id: str,
    bbox: str | None = Query(default=None, description="minLon,minLat,maxLon,maxLat"),
    detect_id: UUID | None = None,
    fit_id: UUID | None = None,
    clean_id: UUID | None = None,
    limit: int = Query(default=_LAYER_LIMIT_DEFAULT, ge=1, le=_LAYER_LIMIT_MAX),
    store: MapStore = Depends(get_map_store),
) -> dict[str, Any]:
    try:
        if clean_id is not None:
            if detect_id is None or store._fetchone(
                "SELECT 1 FROM detect_runs WHERE id = %s AND clean_id = %s",
                (str(detect_id), str(clean_id)),
            ) is None:
                raise MapDataError("Detect ID 不属于所选 Clean ID。")
            if fit_id is not None and store._fetchone(
                "SELECT 1 FROM fit_runs WHERE id = %s AND detect_id = %s",
                (str(fit_id), str(detect_id)),
            ) is None:
                raise MapDataError("Fit ID 不属于所选 Detect ID。")
        return store.read_layer(
            layer_id,
            bbox=parse_bbox(bbox),
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


def _limit_selected_tracks(
    rows: list[Any],
    max_vehicles: int,
) -> tuple[list[int], set[int | str], bool]:
    vehicles: list[str] = []
    subtracks: list[int] = []
    found: set[int | str] = set()
    truncated = False
    for row in rows:
        if row[2] is not None:
            found.add(row[2])
        vehicle = str(row[0])
        if vehicle not in vehicles:
            if len(vehicles) >= max_vehicles:
                truncated = True
                continue
            vehicles.append(vehicle)
        subtrack = int(row[1])
        if subtrack not in subtracks:
            subtracks.append(subtrack)
    return subtracks, found, truncated


def _rows_to_grouped(rows: list[Any]) -> dict[tuple[str, int], list[_TrackPoint]]:
    grouped: dict[tuple[str, int], list[_TrackPoint]] = {}
    for row in rows:
        key = (str(row[1]), int(row[0]))
        grouped.setdefault(key, []).append(
            _TrackPoint(
                resolved_point_id=int(row[2]) if row[2] is not None else None,
                source_row_key=str(row[3]) if row[3] is not None else None,
                lon=float(row[4]),
                lat=float(row[5]),
                point_kind=str(row[6]),
                crossing_id=int(row[7]) if row[7] is not None else None,
                intersection_id=int(row[8]) if row[8] is not None else None,
                crossing_type=int(row[9]) if row[9] is not None else None,
            )
        )
    return grouped


def _complete_tracks(
    rows: list[Any],
    max_points: int,
) -> tuple[dict[tuple[str, int], list[_TrackPoint]], bool]:
    truncated = len(rows) > max_points
    kept = rows[:max_points]
    if truncated and kept:
        last_key = (str(kept[-1][1]), int(kept[-1][0]))
        next_key = (str(rows[max_points][1]), int(rows[max_points][0]))
        if next_key == last_key:
            kept = [
                row
                for row in kept
                if (str(row[1]), int(row[0])) != last_key
            ]
    return _rows_to_grouped(kept), truncated


def _track_feature(
    key: tuple[str, int],
    points: list[_TrackPoint],
    detect_id: str | None,
) -> dict[str, Any]:
    coordinates = [[point.lon, point.lat] for point in points]
    geometry: dict[str, Any]
    if len(coordinates) == 1:
        geometry = {"type": "Point", "coordinates": coordinates[0]}
    else:
        geometry = {"type": "LineString", "coordinates": coordinates}
    return _feature(
        f"{key[0]}:{key[1]}",
        geometry,
        vehicle_id=key[0],
        subtrack_index=key[1],
        detect_id=detect_id,
        resolved_point_ids=[point.resolved_point_id for point in points],
        source_row_keys=[point.source_row_key for point in points],
        point_kinds=[point.point_kind for point in points],
        crossing_ids=[point.crossing_id for point in points],
        point_count=len(points),
    )


def _clip_track(
    key: tuple[str, int],
    points: list[_TrackPoint],
    intersections: list[_Intersection],
    requested: set[int],
    detect_id: str,
    transformer: _CoordinateTransformer,
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
    open_point: _TrackPoint | None = None
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
                            _feature(
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


def _same_position(left: _TrackPoint, right: _TrackPoint) -> bool:
    return abs(left.lon - right.lon) <= 1e-12 and abs(left.lat - right.lat) <= 1e-12


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


def _bbox_clause(column: str, bbox: BBox | None) -> tuple[str, tuple[Any, ...]]:
    if bbox is None:
        return "", ()
    return (
        f" AND {column} && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
        tuple(bbox),
    )


def _quoted_identifier(value: str) -> str:
    parts = value.split(".")
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
    ):
        raise MapDataError(f"invalid source table name in Clean Run: {value!r}")
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


def _json_object(value: Any, field: str) -> dict[str, Any]:
    decoded = _json_value(value, None)
    if not isinstance(decoded, dict):
        raise MapDataError(f"database returned invalid {field}")
    return dict(decoded)


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
