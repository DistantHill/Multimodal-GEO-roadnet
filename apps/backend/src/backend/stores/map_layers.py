"""Read-only PostgreSQL queries for map layers, replay, and Clip inputs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from track.v2.db.source_table import calculate_source_fingerprint, validate_source_table

from ..api.map_contracts import BBox, ClipRequest, ReplayRequest, resolve_clip_limits, resolve_replay_limits
from ..database import _require_config, open_read_connection, require_current_schema
from ..map_errors import MapDataError, MapDatabaseNotFoundError, MapNotFoundError, MapSchemaMismatchError
from ..map_features import create_map_feature, create_map_feature_collection
from ..replay.build_clip_features import ClipIntersection, build_clip_features
from ..replay.track_points import complete_track_groups

@dataclass(frozen=True, slots=True)
class _RefineContext:
    refine_id: str
    source_table: str
    quoted_source_table: str
    source_key_sql_type: str
    source_fingerprint: dict[str, Any]

@dataclass(frozen=True, slots=True)
class _DetectContext:
    detect_id: str
    refine: _RefineContext
    source_srid: int
    processing_srid: int
    projection_config: dict[str, Any]

_SOURCE_KEY_TYPES = {
    "int2": "smallint",
    "int4": "integer",
    "int8": "bigint",
    "uuid": "uuid",
    "text": "text",
    "varchar": "varchar",
}

_LAYER_DEFINITIONS = (
    {
        "id": "tracks_cross", "title": "跨轨迹事件", "role": "point",
        "geometry_type": "Point", "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
    {
        "id": "turn_event_geom", "title": "转向事件", "role": "point",
        "geometry_type": "Point", "required_context": ["detect_id"],
        "default_context": "latest completed Detect Run",
    },
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
    """Bounded, read-only projections over the current Pipeline schema."""

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
        offset: int = 0,
    ) -> dict[str, Any]:
        if layer_id == "resolved_subtrack_points":
            context = self._resolve_detect(detect_id)
            rows = self._resolved_point_layer(context, bbox, limit + 1, offset)
            features = [
                self._resolved_point_feature(row, context.detect_id)
                for row in rows[:limit]
            ]
            metadata = {"detect_id": context.detect_id, "refine_id": context.refine.refine_id}
        elif layer_id == "intersections":
            context = self._resolve_detect(detect_id)
            rows = self._intersection_layer(context.detect_id, bbox, limit + 1, offset)
            features = [
                self._intersection_feature(row, context.detect_id)
                for row in rows[:limit]
            ]
            metadata = {"detect_id": context.detect_id, "refine_id": context.refine.refine_id}
        elif layer_id == "entry_exit_points":
            context = self._resolve_detect(detect_id)
            rows = self._entry_exit_layer(context.detect_id, bbox, limit + 1, offset)
            features = [
                self._entry_exit_feature(row, context.detect_id)
                for row in rows[:limit]
            ]
            metadata = {"detect_id": context.detect_id, "refine_id": context.refine.refine_id}
        elif layer_id in {"tracks_cross", "turn_event_geom"}:
            context = self._resolve_detect(detect_id)
            table = "tracks_cross" if layer_id == "tracks_cross" else "turn_evnets_gemo"
            bbox_clause, bbox_params = _bbox_clause("event.geom", bbox)
            rows = self._fetchall(
                "SELECT event.id, ST_AsGeoJSON(event.geom)::jsonb, to_jsonb(event) - 'geom' "
                f"FROM {table} AS event WHERE event.detect_id = %s" + bbox_clause
                + " ORDER BY event.id LIMIT %s OFFSET %s",
                (context.detect_id, *bbox_params, limit + 1, offset),
            )
            features = [create_map_feature(row[0], row[1], **row[2]) for row in rows[:limit]]
            metadata = {"detect_id": context.detect_id, "refine_id": context.refine.refine_id}
        elif layer_id == "centerlines":
            resolved_fit_id, resolved_detect_id = self._resolve_fit(fit_id)
            if detect_id is not None and str(detect_id) != resolved_detect_id:
                raise MapNotFoundError(
                    f"completed Fit Run {resolved_fit_id} does not belong to Detect Run {detect_id}"
                )
            rows = self._centerline_layer(resolved_fit_id, bbox, limit + 1, offset)
            features = [self._centerline_feature(row, resolved_fit_id) for row in rows[:limit]]
            metadata = {"fit_id": resolved_fit_id, "detect_id": resolved_detect_id}
        else:
            raise MapNotFoundError(f"unknown map layer: {layer_id}")
        has_more = len(rows) > limit
        return create_map_feature_collection(
            features,
            layer=layer_id,
            count=len(features),
            truncated=has_more,
            has_more=has_more,
            next_cursor=offset + len(features) if has_more else None,
            bbox=list(bbox) if bbox is not None else None,
            **metadata,
        )

    def replay(self, request: ReplayRequest) -> dict[str, Any]:
        request = resolve_replay_limits(request, _require_config().map)
        if request.database_name is not None:
            return self._query_database("replay", request)
        self._validate_database(request.database_name)
        if request.centerline_ids is not None:
            from ..replay.select_centerline_members import replay_centerlines

            return replay_centerlines(self, request, _require_config().map)
        if request.intersection_id is not None:
            from ..replay.select_intersection_members import replay_intersection
            from ..replay.select_event_segments import expand_intersection_segments

            return expand_intersection_segments(self, request, replay_intersection(self, request))
        if request.intersection_ids is not None:
            from ..replay.select_intersection_members import replay_intersections
            from ..replay.select_event_segments import expand_intersection_segments

            return expand_intersection_segments(self, request, replay_intersections(self, request))
        if request.injected_point_ids is not None:
            from ..replay.select_event_segments import replay_injected_points

            return replay_injected_points(self, request)
        if request.tracks_cross_ids is not None or request.turn_event_ids is not None:
            from ..replay.select_event_segments import replay_events

            return replay_events(self, request)
        detect = (
            self._resolve_detect(str(request.detect_id))
            if request.detect_id is not None
            else None
        )
        refine = detect.refine if detect is not None else self._resolve_refine(str(request.refine_id) if request.refine_id else None)
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
                selected, found, vehicles_truncated = self._select_refine_tracks(
                    refine,
                    source_row_keys=request.source_row_keys,
                    bbox=None,
                    max_vehicles=request.max_vehicles,
                )
            missing = sorted(set(request.source_row_keys) - {str(value) for value in found})
        else:
            raise MapDataError("Replay 必须使用准确 ID，不允许范围查询。")

        if not selected:
            raise MapNotFoundError(f"no replay points match {selection}")
        rows = (
            self._fetch_resolved_tracks(detect, selected, request.max_points + 1)
            if detect is not None
            else self._fetch_refine_tracks(refine, selected, request.max_points + 1)
        )
        if any(row[1] is None or row[4] is None or row[5] is None for row in rows):
            raise MapDataError("所选轨迹的溯源记录缺失，无法完整回放。")
        grouped, points_truncated = complete_track_groups(rows, request.max_points)
        from ..replay.build_segment_features import build_segment_features

        features = build_segment_features(self, grouped, request, detect.detect_id if detect else None)
        return create_map_feature_collection(
            features,
            operation="replay",
            selection=selection,
            detect_id=detect.detect_id if detect else None,
            refine_id=refine.refine_id,
            selected_track_count=len(selected),
            returned_track_count=len(grouped),
            highlight_segment_count=sum(f["properties"]["replay_role"] == "highlight" for f in features),
            context_segment_count=sum(f["properties"]["replay_role"] == "context" for f in features),
            returned_point_count=sum(len(points) for points in grouped.values()),
            missing_source_row_keys=missing if selection == "source_row_keys" else None,
            missing_resolved_point_ids=(
                missing if selection == "resolved_point_ids" else None
            ),
            truncated=vehicles_truncated or points_truncated,
        )

    def clip(self, request: ClipRequest) -> dict[str, Any]:
        map_config = _require_config().map
        request = resolve_clip_limits(request, map_config)
        if request.database_name is not None:
            return self._query_database("clip", request)
        self._validate_database(request.database_name)
        context = self._resolve_detect(str(request.detect_id))
        self._verify_source_fingerprint(context.refine)
        requested = [int(value) for value in request.intersection_ids]
        all_intersections = self._fetch_intersections(context.detect_id)
        existing = {item.intersection_id for item in all_intersections}
        missing = sorted(set(requested) - existing)
        if missing:
            raise MapNotFoundError(
                f"completed intersections not found in Detect Run {context.detect_id}: {missing}"
            )
        selected, vehicles_truncated = self._select_clip_tracks(
            context, requested, request.max_vehicles
        )
        rows = self._fetch_resolved_tracks(context, selected, request.max_points + 1)
        grouped, points_truncated = complete_track_groups(rows, request.max_points)
        features, clips_truncated = build_clip_features(
            grouped,
            all_intersections,
            set(requested),
            context,
            map_config.clip_feature_limit,
        )
        return create_map_feature_collection(
            features,
            operation="clip",
            detect_id=context.detect_id,
            refine_id=context.refine.refine_id,
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
            return
        row = self._fetchone(
            "SELECT current_database(), EXISTS(SELECT 1 FROM pg_database WHERE datname = %s)",
            (database_name,),
        )
        actual, exists = row
        if not exists:
            raise MapDatabaseNotFoundError(database_name, str(actual))

    def _query_database(self, operation: str, request: ReplayRequest | ClipRequest) -> dict[str, Any]:
        self._validate_database(request.database_name)
        with open_read_connection(request.database_name) as connection:
            try:
                require_current_schema(connection)
            except RuntimeError as exc:
                raise MapSchemaMismatchError({
                    "error": "database_schema_mismatch",
                    "message": "所选数据库不是兼容的当前 schema v8，请确认 QGIS 图层的 database 是否对应。",
                }) from exc
            result = getattr(PostgresMapStore(connection), operation)(
                request.model_copy(update={"database_name": None})
            )
            result.setdefault("metadata", {})["database_name"] = request.database_name
            return result

    def run_catalog(self) -> dict[str, Any]:
        return {
            "refine": [{"id": row[0], "started_at": row[1]} for row in self._fetchall(
                "SELECT id::text, started_at FROM refine_runs WHERE status = 'completed' ORDER BY finished_at DESC NULLS LAST, started_at DESC", ()
            )],
            "detect": [{"id": row[0], "refine_id": row[1], "started_at": row[2]} for row in self._fetchall(
                "SELECT id::text, refine_id::text, started_at FROM detect_runs WHERE status = 'completed' ORDER BY finished_at DESC NULLS LAST, started_at DESC", ()
            )],
            "fit": [{"id": row[0], "detect_id": row[1], "started_at": row[2]} for row in self._fetchall(
                "SELECT id::text, detect_id::text, started_at FROM fit_runs WHERE status = 'completed' ORDER BY finished_at DESC NULLS LAST, started_at DESC", ()
            )],
        }

    def validate_layer_context(
        self, refine_id: str, detect_id: str | None, fit_id: str | None
    ) -> None:
        if detect_id is None or self._fetchone(
            "SELECT 1 FROM detect_runs WHERE id = %s AND refine_id = %s",
            (detect_id, refine_id),
        ) is None:
            raise MapDataError("Detect ID 不属于所选 Refine ID。")
        if fit_id is not None and self._fetchone(
            "SELECT 1 FROM fit_runs WHERE id = %s AND detect_id = %s",
            (fit_id, detect_id),
        ) is None:
            raise MapDataError("Fit ID 不属于所选 Detect ID。")

    def _resolve_detect(self, detect_id: str | None) -> _DetectContext:
        clause = "d.id = %s" if detect_id else "TRUE"
        order = "" if detect_id else " ORDER BY d.finished_at DESC NULLS LAST, d.started_at DESC"
        row = self._fetchone(
            "SELECT d.id::text, c.id::text, c.source_table, c.source_fingerprint, "
            "c.source_srid, c.processing_srid, c.projection_config "
            "FROM detect_runs AS d JOIN refine_runs AS c ON c.id = d.refine_id "
            f"WHERE d.status = 'completed' AND c.status = 'completed' AND {clause}"
            + order
            + " LIMIT 1",
            (detect_id,) if detect_id else (),
        )
        if row is None:
            if detect_id:
                raise MapNotFoundError(f"unknown completed Detect Run: {detect_id}")
            raise MapNotFoundError("no completed Detect Run is available")
        refine = self._refine_context(str(row[1]), str(row[2]), row[3])
        if row[5] is None:
            raise MapDataError(f"Detect Run {row[0]} has no processing CRS")
        return _DetectContext(
            detect_id=str(row[0]),
            refine=refine,
            source_srid=int(row[4]),
            processing_srid=int(row[5]),
            projection_config=_json_object(row[6], "projection_config"),
        )

    def _resolve_refine(self, refine_id: str | None) -> _RefineContext:
        clause = "id = %s" if refine_id else "TRUE"
        order = "" if refine_id else " ORDER BY finished_at DESC NULLS LAST, started_at DESC"
        row = self._fetchone(
            "SELECT id::text, source_table, source_fingerprint FROM refine_runs "
            f"WHERE status = 'completed' AND {clause}" + order + " LIMIT 1",
            (refine_id,) if refine_id else (),
        )
        if row is None:
            raise MapNotFoundError("no completed Refine Run is available")
        return self._refine_context(str(row[0]), str(row[1]), row[2])

    def _refine_context(
        self,
        refine_id: str,
        source_table: str,
        source_fingerprint: Any,
    ) -> _RefineContext:
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
        return _RefineContext(
            refine_id=refine_id,
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

    def _verify_source_fingerprint(self, refine: _RefineContext) -> None:
        expected = refine.source_fingerprint
        if not expected:
            raise MapDataError(f"Refine Run {refine.refine_id} has no source fingerprint")
        try:
            contract = validate_source_table(self.connection, refine.source_table)
            actual = calculate_source_fingerprint(
                self.connection, refine.refine_id, contract
            )
        except ValueError as exc:
            raise MapDataError(
                f"source rows for Refine Run {refine.refine_id} cannot be verified; replay is refused"
            ) from exc
        if actual != expected:
            raise MapDataError(
                f"source rows for Refine Run {refine.refine_id} changed; replay is refused"
            )

    def _resolved_point_layer(
        self,
        context: _DetectContext,
        bbox: BBox | None,
        limit: int,
        offset: int = 0,
    ) -> list[Any]:
        if bbox is not None:
            return self._resolved_point_layer_in_bbox(context, bbox, limit, offset)

        base = self._resolved_base_sql(context.refine)
        return self._fetchall(
            base
            + " SELECT subtrack_index, vehicle_id, resolved_point_id, source_row_key, "
            "point_kind, crossing_id, intersection_id, crossing_type, "
            "ST_AsGeoJSON(geom)::jsonb FROM resolved WHERE detect_id = %s"
            + " ORDER BY subtrack_index, sequence_in_subtrack, resolved_point_id LIMIT %s OFFSET %s",
            (context.detect_id, context.detect_id, limit, offset),
        )

    def _resolved_point_layer_in_bbox(
        self,
        context: _DetectContext,
        bbox: BBox,
        limit: int,
        offset: int,
    ) -> list[Any]:
        """Read only resolved points whose persisted source geometry intersects bbox.

        Kept points get their geometry from the Refine source table, while injected
        points and crossings have their own stored geometry. Spatial candidate CTEs
        use those source geometries' GiST indexes before looking up resolved IDs.
        Vehicle IDs are then aggregated only for subtracks represented in the bbox.
        """
        sql = f"""
            WITH tile AS (
                SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS geom
            ), spatial_points AS MATERIALIZED (
                SELECT point.id AS resolved_point_id, point.subtrack_index
                  FROM {context.refine.quoted_source_table} AS source
                  JOIN kept_trajectory_points AS kept
                    ON source.id = kept.source_row_key::{context.refine.source_key_sql_type}
                  JOIN resolved_subtrack_points AS point
                    ON point.detect_id = %s AND point.kept_point_id = kept.id
                  CROSS JOIN tile
                 WHERE kept.refine_id = %s
                   AND source.geom && tile.geom
                   AND ST_Intersects(source.geom, tile.geom)
                UNION ALL
                SELECT point.id AS resolved_point_id, point.subtrack_index
                  FROM injected_points AS injected
                  JOIN resolved_subtrack_points AS point
                    ON point.detect_id = %s AND point.injected_point_id = injected.id
                  CROSS JOIN tile
                 WHERE injected.refine_id = %s
                   AND injected.geom && tile.geom
                   AND ST_Intersects(injected.geom, tile.geom)
                UNION ALL
                SELECT point.id AS resolved_point_id, point.subtrack_index
                  FROM candidate_entry_exit_points AS crossing
                  JOIN resolved_subtrack_points AS point
                    ON point.detect_id = %s AND point.crossing_point_id = crossing.id
                  CROSS JOIN tile
                 WHERE crossing.detect_id = %s
                   AND crossing.geom && tile.geom
                   AND ST_Intersects(crossing.geom, tile.geom)
            ), candidate_subtracks AS MATERIALIZED (
                SELECT DISTINCT subtrack_index FROM spatial_points
            ), subtrack_vehicle AS (
                SELECT member.subtrack_index,
                       MIN(COALESCE(kept.vehicle_id, injected.vehicle_id)) AS vehicle_id
                  FROM candidate_subtracks AS selected
                  JOIN resolved_subtrack_points AS member
                    ON member.detect_id = %s
                   AND member.subtrack_index = selected.subtrack_index
                  LEFT JOIN kept_trajectory_points AS kept
                    ON kept.id = member.kept_point_id
                  LEFT JOIN injected_points AS injected
                    ON injected.id = member.injected_point_id
                 GROUP BY member.subtrack_index
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
                  FROM spatial_points AS selected
                  JOIN resolved_subtrack_points AS point
                    ON point.id = selected.resolved_point_id AND point.detect_id = %s
                  JOIN subtrack_vehicle AS vehicle
                    ON vehicle.subtrack_index = point.subtrack_index
                  LEFT JOIN kept_trajectory_points AS kept
                    ON kept.id = point.kept_point_id
                  LEFT JOIN {context.refine.quoted_source_table} AS source
                    ON source.id = kept.source_row_key::{context.refine.source_key_sql_type}
                  LEFT JOIN injected_points AS injected
                    ON injected.id = point.injected_point_id
                  LEFT JOIN candidate_entry_exit_points AS crossing
                    ON crossing.id = point.crossing_point_id
                   AND crossing.detect_id = point.detect_id
                  LEFT JOIN intersections AS intersection
                    ON intersection.detect_id = crossing.detect_id
                   AND intersection.candidate_intersection_id = crossing.candidate_intersection_id
            )
            SELECT subtrack_index, vehicle_id, resolved_point_id, source_row_key,
                   point_kind, crossing_id, intersection_id, crossing_type,
                   ST_AsGeoJSON(geom)::jsonb
              FROM resolved
             ORDER BY subtrack_index, sequence_in_subtrack, resolved_point_id
             LIMIT %s OFFSET %s
        """
        return self._fetchall(
            sql,
            (
                *bbox,
                context.detect_id,
                context.refine.refine_id,
                context.detect_id,
                context.refine.refine_id,
                context.detect_id,
                context.detect_id,
                context.detect_id,
                context.detect_id,
                limit,
                offset,
            ),
        )

    def _intersection_layer(self, detect_id: str, bbox: BBox | None, limit: int, offset: int = 0) -> list[Any]:
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
            + " ORDER BY intersection.id LIMIT %s OFFSET %s",
            (detect_id, *bbox_params, limit, offset),
        )

    def _entry_exit_layer(self, detect_id: str, bbox: BBox | None, limit: int, offset: int = 0) -> list[Any]:
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
            + " ORDER BY resolved.subtrack_index, resolved.sequence_in_subtrack, crossing.id LIMIT %s OFFSET %s",
            (detect_id, *bbox_params, limit, offset),
        )

    def _centerline_layer(self, fit_id: str, bbox: BBox | None, limit: int, offset: int = 0) -> list[Any]:
        bbox_clause, bbox_params = _bbox_clause("centerline.geom", bbox)
        return self._fetchall(
            "SELECT centerline.id, centerline.detect_id::text, centerline.variant, "
            "centerline.from_intersection_id, centerline.to_intersection_id, "
            "centerline.length_m, centerline.forward_clip_count, "
            "centerline.reverse_clip_count, ST_AsGeoJSON(centerline.geom)::jsonb "
            "FROM centerlines AS centerline WHERE centerline.fit_id = %s"
            + bbox_clause
            + " ORDER BY centerline.id LIMIT %s OFFSET %s",
            (fit_id, *bbox_params, limit, offset),
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
        if resolved_point_ids is not None:
            rows = self._fetchall(
                "SELECT vehicle.vehicle_id, point.subtrack_index, point.id "
                "FROM resolved_subtrack_points point "
                "CROSS JOIN LATERAL ("
                " SELECT COALESCE(k.vehicle_id, i.vehicle_id) AS vehicle_id"
                " FROM resolved_subtrack_points anchor"
                " LEFT JOIN kept_trajectory_points k ON k.id = anchor.kept_point_id"
                " LEFT JOIN injected_points i ON i.id = anchor.injected_point_id"
                " WHERE anchor.detect_id = point.detect_id AND anchor.subtrack_index = point.subtrack_index"
                " AND (anchor.kept_point_id IS NOT NULL OR anchor.injected_point_id IS NOT NULL)"
                " ORDER BY anchor.sequence_in_subtrack LIMIT 1"
                ") vehicle WHERE point.detect_id = %s AND point.id = ANY(%s::bigint[])"
                " ORDER BY vehicle.vehicle_id, point.subtrack_index",
                (context.detect_id, resolved_point_ids),
            )
            return _limit_selected_tracks(rows, max_vehicles)
        if source_row_keys is not None:
            rows = self._fetchall(
                "SELECT kept.vehicle_id, point.subtrack_index, kept.source_row_key"
                " FROM kept_trajectory_points kept JOIN resolved_subtrack_points point"
                " ON point.kept_point_id = kept.id AND point.detect_id = %s"
                " WHERE kept.refine_id = %s AND kept.source_row_key = ANY(%s::bigint[])"
                " ORDER BY kept.vehicle_id, point.subtrack_index",
                (context.detect_id, context.refine.refine_id, source_row_keys),
            )
            return _limit_selected_tracks(rows, max_vehicles)
        raise MapDataError("Replay 必须使用准确 ID，不允许范围查询。")

    def _select_refine_tracks(
        self,
        refine: _RefineContext,
        *,
        source_row_keys: list[str] | None,
        bbox: BBox | None,
        max_vehicles: int,
    ) -> tuple[list[int], set[int | str], bool]:
        if source_row_keys is not None:
            rows = self._fetchall(
                "SELECT DISTINCT kept.vehicle_id, kept.subtrack_index, kept.source_row_key "
                "FROM kept_trajectory_points AS kept WHERE kept.refine_id = %s "
                "AND kept.source_row_key = ANY(%s::bigint[]) "
                "ORDER BY kept.vehicle_id, kept.subtrack_index",
                (refine.refine_id, source_row_keys),
            )
        else:
            raise MapDataError("Replay 必须使用准确来源键，不允许范围查询。")
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
        refine = context.refine
        return self._fetchall(
            "WITH selected AS MATERIALIZED ("
            " SELECT * FROM resolved_subtrack_points WHERE detect_id = %s"
            " AND subtrack_index = ANY(%s::integer[])"
            " ORDER BY subtrack_index, sequence_in_subtrack LIMIT %s"
            ") SELECT point.subtrack_index, vehicle.vehicle_id, point.id, kept.source_row_key,"
            " ST_X(COALESCE(source.geom, injected.geom, crossing.geom)),"
            " ST_Y(COALESCE(source.geom, injected.geom, crossing.geom)),"
            " CASE WHEN point.kept_point_id IS NOT NULL THEN 'kept'"
            " WHEN point.injected_point_id IS NOT NULL THEN 'injected' ELSE 'crossing' END,"
            " crossing.id, intersection.id, crossing.crossing_type"
            " FROM selected point"
            " LEFT JOIN LATERAL (SELECT * FROM kept_trajectory_points WHERE id = point.kept_point_id AND refine_id = %s LIMIT 1) kept ON TRUE"
            f" LEFT JOIN LATERAL (SELECT * FROM {refine.quoted_source_table} WHERE id = kept.source_row_key::{refine.source_key_sql_type} LIMIT 1) source ON TRUE"
            " LEFT JOIN LATERAL (SELECT * FROM injected_points WHERE id = point.injected_point_id AND refine_id = %s LIMIT 1) injected ON TRUE"
            " LEFT JOIN candidate_entry_exit_points crossing ON crossing.id = point.crossing_point_id AND crossing.detect_id = point.detect_id"
            " LEFT JOIN intersections intersection ON intersection.detect_id = crossing.detect_id"
            " AND intersection.candidate_intersection_id = crossing.candidate_intersection_id"
            " CROSS JOIN LATERAL ("
            " SELECT COALESCE(k.vehicle_id, i.vehicle_id) AS vehicle_id FROM resolved_subtrack_points anchor"
            " LEFT JOIN kept_trajectory_points k ON k.id = anchor.kept_point_id"
            " LEFT JOIN injected_points i ON i.id = anchor.injected_point_id"
            " WHERE anchor.detect_id = point.detect_id AND anchor.subtrack_index = point.subtrack_index"
            " AND (anchor.kept_point_id IS NOT NULL OR anchor.injected_point_id IS NOT NULL)"
            " ORDER BY anchor.sequence_in_subtrack LIMIT 1) vehicle"
            " ORDER BY point.subtrack_index, point.sequence_in_subtrack",
            (context.detect_id, subtrack_indexes, limit, refine.refine_id, refine.refine_id),
        )

    def _fetch_refine_tracks(
        self,
        refine: _RefineContext,
        subtrack_indexes: list[int],
        limit: int,
    ) -> list[Any]:
        if not subtrack_indexes:
            return []
        return self._fetchall(
            "SELECT subtrack_index, vehicle_id, NULL::bigint, source_row_key, "
            "lon, lat, point_kind, NULL::bigint, NULL::bigint, NULL::smallint FROM ("
            " SELECT kept.subtrack_index, kept.vehicle_id, kept.source_row_key, "
            " ST_X(source.geom) AS lon, ST_Y(source.geom) AS lat, 'kept'::text AS point_kind, "
            " kept.sequence_in_subtrack AS anchor_sequence, 0 AS kind_order, kept.id AS row_id "
            f" FROM kept_trajectory_points AS kept JOIN {refine.quoted_source_table} AS source "
            f" ON source.id = kept.source_row_key::{refine.source_key_sql_type} "
            " WHERE kept.refine_id = %s AND kept.subtrack_index = ANY(%s::integer[]) "
            " UNION ALL SELECT injected.subtrack_index, injected.vehicle_id, NULL::text, "
            " ST_X(injected.geom), ST_Y(injected.geom), 'injected', injected.after_sequence_in_subtrack, "
            " 1, injected.id FROM injected_points AS injected WHERE injected.refine_id = %s "
            " AND injected.subtrack_index = ANY(%s::integer[])"
            ") AS refine_points ORDER BY subtrack_index, anchor_sequence, kind_order, row_id LIMIT %s",
            (refine.refine_id, subtrack_indexes, refine.refine_id, subtrack_indexes, limit),
        )

    def _resolved_base_sql(self, refine: _RefineContext) -> str:
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
                  LEFT JOIN {refine.quoted_source_table} AS source
                    ON source.id = kept.source_row_key::{refine.source_key_sql_type}
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

    def _fetch_intersections(self, detect_id: str) -> list[ClipIntersection]:
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
            ClipIntersection(int(row[0]), float(row[1]), float(row[2]), float(row[3]))
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
        return create_map_feature(
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
        return create_map_feature(
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
        return create_map_feature(
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
    def _centerline_feature(row: Any, fit_id: str | None = None) -> dict[str, Any]:
        return create_map_feature(
            int(row[0]),
            _geometry(row[8]),
            centerline_id=int(row[0]),
            fit_id=fit_id,
            detect_id=str(row[1]),
            variant=str(row[2]),
            from_intersection_id=int(row[3]),
            to_intersection_id=int(row[4]),
            length_m=float(row[5]),
            forward_clip_count=int(row[6]),
            reverse_clip_count=int(row[7]),
        )

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

def _bbox_clause(column: str, bbox: BBox | None) -> tuple[str, tuple[Any, ...]]:
    if bbox is None:
        return "", ()
    return (
        f" AND {column} && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
        f" AND ST_Intersects({column}, ST_MakeEnvelope(%s, %s, %s, %s, 4326))",
        (*bbox, *bbox),
    )

def _quoted_identifier(value: str) -> str:
    parts = value.split(".")
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
    ):
        raise MapDataError(f"invalid source table name in Refine Run: {value!r}")
    return ".".join(f'"{part}"' for part in parts)

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
