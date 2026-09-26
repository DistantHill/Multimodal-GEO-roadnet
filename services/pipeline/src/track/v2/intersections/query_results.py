"""Read persisted Detect intersections and resolved Clip track sequences."""

from __future__ import annotations

import json
import math
import numpy as np
import uuid
from dataclasses import dataclass
from typing import Iterator

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..models import ClipIntersection, ClipTrack, SpatialContext
from ..projection import CoordinateTransformer, spatial_context_from_values
from ..db.source_table import validate_source_table
from ..db.plan_full_scan_cursor import declare_full_scan_cursor


@dataclass(frozen=True, slots=True)
class _RunRow:
    """One Detect Run together with its parent Refine Run metadata."""

    refine_id: str
    source_table: str
    parameters: dict[str, object]
    status: str
    spatial_context: SpatialContext

def fetch_detect_row(conn: object, detect_id: str) -> _RunRow:
    """Fetch a Detect Run and the source metadata of its parent Refine Run."""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT d.refine_id, c.source_table, c.parameters, d.status, "
            "c.source_srid, c.processing_srid, c.projection_config "
            "FROM detect_runs AS d JOIN refine_runs AS c ON c.id = d.refine_id "
            "WHERE d.id = %s",
            (detect_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Detect Run: {detect_id}")
    refine_id, source_table, raw_parameters, status, source_srid, processing_srid, projection_config = row
    return _RunRow(
        refine_id=str(refine_id),
        source_table=str(source_table),
        parameters=_decode_json_object(raw_parameters),
        status=str(status),
        spatial_context=spatial_context_from_values(
            source_srid,
            processing_srid,
            projection_config,
        ),
    )

def fetch_intersections(conn: object, detect_id: str) -> list[ClipIntersection]:
    """Load verified intersections belonging to one Detect Run."""
    intersections: list[ClipIntersection] = []
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT i.id, ci.centroid_lon, ci.centroid_lat, i.clip_radius_m "
            "FROM intersections AS i "
            "JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id AND ci.detect_id = i.detect_id "
            "WHERE i.detect_id = %s "
            "ORDER BY i.id",
            (detect_id,),
        )
        for intersection_id, lon, lat, radius_m in cur:
            if radius_m is None:
                raise ValueError(
                    f"Detect Run {detect_id} is missing its Clip radius"
                )
            if not math.isfinite(float(radius_m)) or float(radius_m) <= 0.0:
                raise ValueError(f"Detect Run {detect_id} has an invalid Clip radius")
            intersections.append(
                ClipIntersection(
                    intersection_id=int(intersection_id),
                    lon=float(lon),
                    lat=float(lat),
                    radius_m=float(radius_m),
                )
            )
    return intersections

@observed_operation("读取 Clip 解析轨迹")
def fetch_resolved_clip_tracks(
    conn: object,
    detect_id: str,
    *,
    batch_size: int,
) -> Iterator[ClipTrack]:
    """Stream complete subtracks from the final p/i/c sequence for Clip without reconstructing roots."""
    run = fetch_detect_row(conn, detect_id)
    if run.status != "completed":
        raise ValueError(f"Detect Run {detect_id} is not completed")
    contract = validate_source_table(conn, run.source_table)
    # Completed intersection metadata is small; avoid joining it once per
    # resolved point when a newly written Run is estimated as one row.
    with conn.cursor() as metadata:
        metadata.execute(
            "SELECT candidate_intersection_id, id FROM intersections WHERE detect_id = %s",
            (detect_id,),
        )
        intersection_ids = {int(candidate): int(intersection) for candidate, intersection in metadata}
    query = f"""
        SELECT resolved.id,
               resolved.subtrack_index,
               resolved.sequence_in_subtrack,
               COALESCE(kept.vehicle_id, injected.vehicle_id) AS vehicle_id,
               COALESCE(source.lon, injected.lon, crossing.crossing_lon) AS lon,
               COALESCE(source.lat, injected.lat, crossing.crossing_lat) AS lat,
               crossing.id AS crossing_id,
               crossing.candidate_intersection_id,
               crossing.crossing_type,
               COALESCE(ST_X(ST_Transform(ST_SetSRID(ST_MakePoint(source.lon, source.lat), 4326), {run.spatial_context.processing_srid})), injected.x, crossing.crossing_x) AS x,
               COALESCE(ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint(source.lon, source.lat), 4326), {run.spatial_context.processing_srid})), injected.y, crossing.crossing_y) AS y
          FROM resolved_subtrack_points AS resolved
          LEFT JOIN kept_trajectory_points AS kept
            ON kept.id = resolved.kept_point_id
          LEFT JOIN {contract.quoted_table} AS source
            ON source.id = kept.source_row_key
          LEFT JOIN injected_points AS injected
            ON injected.id = resolved.injected_point_id
          LEFT JOIN candidate_entry_exit_points AS crossing
            ON crossing.id = resolved.crossing_point_id
           AND crossing.detect_id = resolved.detect_id
         WHERE resolved.detect_id = %s
         ORDER BY resolved.subtrack_index, resolved.sequence_in_subtrack
    """
    current_subtrack: int | None = None
    current_vehicle = ""
    points: list[tuple[object, ...]] = []

    def build_track() -> ClipTrack:
        lonlat = np.asarray([(row[4], row[5]) for row in points], dtype=np.float64)
        valid = np.asarray([row[6] is not None for row in points], dtype=np.bool_)
        return ClipTrack(
            vehicle_id=current_vehicle, trip_id=f"{current_vehicle}:subtrack-{current_subtrack:04d}",
            xy=np.asarray([(row[9],row[10]) for row in points], dtype=np.float64), lonlat=lonlat,
            resolved_point_ids=np.asarray([row[0] for row in points], dtype=np.int64),
            crossing_ids=np.asarray([row[6] if row[6] is not None else 0 for row in points], dtype=np.int64),
            intersection_ids=np.asarray([intersection_ids.get(row[7], 0) for row in points], dtype=np.int64),
            crossing_types=np.asarray([row[8] if row[8] is not None else 0 for row in points], dtype=np.int8),
            crossing_valid=valid, intersection_valid=np.asarray([row[7] in intersection_ids for row in points], dtype=np.bool_), subtrack_index=current_subtrack,
        )
    with conn.cursor(name=f"clip_tracks_{uuid.uuid4().hex}") as cur:
        cur.itersize = batch_size
        declare_full_scan_cursor(conn, cur, query, (detect_id,))
        for row_count, row in enumerate(cur, 1):
            if row_count % 10_000 == 0:
                heartbeat_progress(row_count)
            if row[4] is None or row[5] is None or (row[3] is None and row[6] is None):
                raise ValueError(f"Detect Run {detect_id} has missing resolved point data")
            subtrack_index = int(row[1])
            vehicle_id = None if row[3] is None else str(row[3])
            if current_subtrack is not None and subtrack_index != current_subtrack:
                if not current_vehicle:
                    raise ValueError(f"subtrack {current_subtrack} has no source vehicle")
                yield build_track()
                points = []
                current_vehicle = ""
            elif (
                current_subtrack is not None
                and vehicle_id is not None
                and current_vehicle
                and current_vehicle != vehicle_id
            ):
                raise ValueError(f"subtrack {subtrack_index} contains multiple vehicles")
            current_subtrack = subtrack_index
            if vehicle_id is not None:
                current_vehicle = vehicle_id
            points.append(row)
        if current_subtrack is not None:
            if not current_vehicle:
                raise ValueError(f"subtrack {current_subtrack} has no source vehicle")
            yield build_track()

def _decode_json_object(value: object) -> dict[str, object]:
    """Decode PostgreSQL JSONB values returned as dicts or JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("Refine Run parameters must be a JSON object")
