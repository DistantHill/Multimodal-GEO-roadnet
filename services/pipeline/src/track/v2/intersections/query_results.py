"""Read persisted Detect intersections and resolved Clip track sequences."""

from __future__ import annotations

import json
from itertools import islice
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
               COALESCE(kept.x, injected.x, crossing.crossing_x) AS x,
               COALESCE(kept.y, injected.y, crossing.crossing_y) AS y
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
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    current_subtrack: int | None = None
    current_vehicle = ""
    chunks: list[tuple[np.ndarray, ...]] = []

    def build_track() -> ClipTrack:
        columns = tuple(np.concatenate([chunk[index] for chunk in chunks])
                        for index in range(10))
        return ClipTrack(
            vehicle_id=current_vehicle, trip_id=f"{current_vehicle}:subtrack-{current_subtrack:04d}",
            xy=np.column_stack((columns[7], columns[8])),
            lonlat=np.column_stack((columns[1], columns[2])),
            resolved_point_ids=columns[0], crossing_ids=columns[3],
            intersection_ids=columns[4], crossing_types=columns[5],
            crossing_valid=columns[6], intersection_valid=columns[9],
            subtrack_index=current_subtrack,
        )

    with conn.cursor(name=f"clip_tracks_{uuid.uuid4().hex}") as cur:
        cur.itersize = batch_size
        declare_full_scan_cursor(conn, cur, query, (detect_id,))
        rows_iterator = iter(cur)
        row_count = 0
        while rows := list(islice(rows_iterator, batch_size)):
            if any(row[4] is None or row[5] is None or row[9] is None or row[10] is None
                   or (row[3] is None and row[6] is None) for row in rows):
                raise ValueError(f"Detect Run {detect_id} has missing resolved point data")
            ids = np.fromiter((int(row[1]) for row in rows), dtype=np.int64, count=len(rows))
            owners = np.asarray([row[3] for row in rows], dtype=object)
            columns = (
                np.fromiter((int(row[0]) for row in rows), dtype=np.int64, count=len(rows)),
                np.fromiter((float(row[4]) for row in rows), dtype=np.float64, count=len(rows)),
                np.fromiter((float(row[5]) for row in rows), dtype=np.float64, count=len(rows)),
                np.fromiter((0 if row[6] is None else int(row[6]) for row in rows), dtype=np.int64, count=len(rows)),
                np.fromiter((intersection_ids.get(row[7], 0) for row in rows), dtype=np.int64, count=len(rows)),
                np.fromiter((0 if row[8] is None else int(row[8]) for row in rows), dtype=np.int8, count=len(rows)),
                np.fromiter((row[6] is not None for row in rows), dtype=bool, count=len(rows)),
                np.fromiter((float(row[9]) for row in rows), dtype=np.float64, count=len(rows)),
                np.fromiter((float(row[10]) for row in rows), dtype=np.float64, count=len(rows)),
                np.fromiter((row[7] in intersection_ids for row in rows), dtype=bool, count=len(rows)),
            )
            previous_count = row_count
            row_count += len(rows)
            del rows
            for count in range((previous_count // 10_000 + 1) * 10_000, row_count + 1, 10_000):
                heartbeat_progress(count)
            boundaries = np.r_[0, np.flatnonzero(ids[1:] != ids[:-1]) + 1, len(ids)]
            for start, stop in zip(boundaries[:-1], boundaries[1:]):
                subtrack_index = int(ids[start])
                if current_subtrack is not None and subtrack_index != current_subtrack:
                    if not current_vehicle:
                        raise ValueError(f"subtrack {current_subtrack} has no source vehicle")
                    track = build_track()
                    chunks = []
                    yield track
                    del track
                    current_vehicle = ""
                current_subtrack = subtrack_index
                vehicle_ids = {str(owner) for owner in owners[start:stop] if owner is not None}
                if current_vehicle:
                    vehicle_ids.add(current_vehicle)
                if len(vehicle_ids) > 1:
                    raise ValueError(f"subtrack {subtrack_index} contains multiple vehicles")
                if vehicle_ids:
                    current_vehicle = vehicle_ids.pop()
                chunks.append(tuple(column[start:stop] for column in columns))
            del columns, owners, ids
        if current_subtrack is not None:
            if not current_vehicle:
                raise ValueError(f"subtrack {current_subtrack} has no source vehicle")
            track = build_track()
            chunks = []
            yield track
            del track

def _decode_json_object(value: object) -> dict[str, object]:
    """Decode PostgreSQL JSONB values returned as dicts or JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("Refine Run parameters must be a JSON object")
