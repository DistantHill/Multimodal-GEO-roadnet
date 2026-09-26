"""Read frozen Refine points named by replay manifests."""

from __future__ import annotations

from typing import Any

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..models import PointRef, RefinedPointRef, SpatialContext, TrajectoryPoint
from ..db.source_table import validate_source_table
from ..db._point_query_fragments import _INJECTED_SOURCE_ID


@observed_operation("读取 Refine 点序列")
def fetch_refine_points(
    conn: object,
    table: str,
    refine_id: str,
    *,
    spatial_context: SpatialContext,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load one Refine Run's frozen kept members and injected points."""
    contract = validate_source_table(conn, table)
    processing_srid = int(spatial_context.processing_srid)
    extra = "tp.extra" if contract.has_extra else "'{}'::jsonb"
    sql = (
        "SELECT vehicle_id, subtrack_index, anchor_sequence, point_kind, row_id, "
        "source_id, source_row_key, timestamp, lon, lat, heading, speed, extra, "
        "generated_reason, x, y "
        "FROM ("
        "  SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack AS anchor_sequence, "
        "         0 AS point_kind, ktp.id AS row_id, "
        "         %s::text AS source_id, ktp.source_row_key::text AS source_row_key, "
        f"         tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, {extra} AS extra, "
        "         ''::text AS generated_reason, "
        f"         ST_X(ST_Transform(tp.geom, {processing_srid})) AS x, "
        f"         ST_Y(ST_Transform(tp.geom, {processing_srid})) AS y "
        "  FROM kept_trajectory_points AS ktp "
        f"  JOIN {contract.quoted_table} AS tp "
        f"    ON tp.id = ktp.source_row_key "
        "  WHERE ktp.refine_id = %s "
        "  UNION ALL "
        "  SELECT ip.vehicle_id, ip.subtrack_index, ip.after_sequence_in_subtrack, "
        "         1 AS point_kind, ip.id AS row_id, "
        "         %s::text AS source_id, ip.id::text AS source_row_key, "
        "         ip.timestamp, ip.lon, ip.lat, ip.heading, ip.speed, '{}'::jsonb, "
        "         ip.generated_reason, ip.x, ip.y "
        "  FROM injected_points AS ip WHERE ip.refine_id = %s"
        ") AS refine_points"
    )
    return _group_points(
        conn,
        sql,
        (table, refine_id, _INJECTED_SOURCE_ID, refine_id),
        refine_id,
    )

@observed_operation("批量读取 Refine 子轨迹")
def fetch_refine_points_for_subtracks(
    conn: object,
    table: str,
    refine_id: str,
    subtracks: set[tuple[str, int]],
    *,
    spatial_context: SpatialContext,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load only the frozen Refine Run subtracks named by a replay manifest."""
    if not subtracks:
        return {}
    requested = sorted(subtracks)
    vehicle_ids = [vehicle_id for vehicle_id, _ in requested]
    subtrack_indexes = [subtrack_index for _, subtrack_index in requested]
    contract = validate_source_table(conn, table)
    processing_srid = int(spatial_context.processing_srid)
    extra = "tp.extra" if contract.has_extra else "'{}'::jsonb"
    sql = (
        "WITH requested(vehicle_id, subtrack_index) AS ("
        "  SELECT * FROM unnest(%s::text[], %s::integer[])"
        ") "
        "SELECT vehicle_id, subtrack_index, anchor_sequence, point_kind, row_id, "
        "source_id, source_row_key, timestamp, lon, lat, heading, speed, extra, "
        "generated_reason, x, y "
        "FROM ("
        "  SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack AS anchor_sequence, "
        "         0 AS point_kind, ktp.id AS row_id, "
        "         %s::text AS source_id, ktp.source_row_key::text AS source_row_key, "
        f"         tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, {extra} AS extra, "
        "         ''::text AS generated_reason, "
        f"         ST_X(ST_Transform(tp.geom, {processing_srid})) AS x, "
        f"         ST_Y(ST_Transform(tp.geom, {processing_srid})) AS y "
        "  FROM kept_trajectory_points AS ktp "
        "  JOIN requested AS r USING (vehicle_id, subtrack_index) "
        f"  JOIN {contract.quoted_table} AS tp "
        f"    ON tp.id = ktp.source_row_key "
        "  WHERE ktp.refine_id = %s "
        "  UNION ALL "
        "  SELECT ip.vehicle_id, ip.subtrack_index, ip.after_sequence_in_subtrack, "
        "         1 AS point_kind, ip.id AS row_id, "
        "         %s::text AS source_id, ip.id::text AS source_row_key, "
        "         ip.timestamp, ip.lon, ip.lat, ip.heading, ip.speed, '{}'::jsonb, "
        "         ip.generated_reason, ip.x, ip.y "
        "  FROM injected_points AS ip "
        "  JOIN requested AS r USING (vehicle_id, subtrack_index) "
        "  WHERE ip.refine_id = %s"
        ") AS refine_points"
    )
    return _group_points(
        conn,
        sql,
        (
            vehicle_ids,
            subtrack_indexes,
            table,
            refine_id,
            _INJECTED_SOURCE_ID,
            refine_id,
        ),
        refine_id,
    )

def _group_points(
    conn: object,
    sql: str,
    params: tuple[object, ...],
    refine_id: str,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Execute *sql* and locally order points within each subtrack.

    Avoiding a global PostgreSQL sort is material for large Refine runs: the join
    rows are wide and routinely spill hundreds of megabytes to temporary files.
    Subtracks are small, so ordering their members independently in Python has
    the same semantics with much lower sort pressure.
    """
    grouped_rows: dict[
        tuple[str, int],
        list[tuple[int, int, int, TrajectoryPoint]],
    ] = {}
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(sql, params)
        for row_count, row in enumerate(cur, 1):
            if row_count % 10_000 == 0:
                heartbeat_progress(row_count)
            vehicle_id = str(row[0])
            subtrack_index = int(row[1])
            if row[5] is None or row[6] is None:
                raise ValueError(
                    f"Refine Run {refine_id} has missing source references"
                )
            if any(row[index] is None for index in range(7, 12)):
                raise ValueError(
                    f"Refine Run {refine_id} references a missing source row"
                )
            generated_reason = str(row[13])
            is_injected = int(row[3]) == 1
            point = TrajectoryPoint(
                point_ref=PointRef(
                    "public.injected_points" if is_injected else str(row[5]),
                    str(row[4]) if is_injected else str(row[6]),
                    stable=not generated_reason,
                    generated_reason=generated_reason,
                ),
                vehicle_id=vehicle_id,
                timestamp=float(row[7]),
                lon=float(row[8]),
                lat=float(row[9]),
                heading=float(row[10]),
                speed=float(row[11]),
                extra=row[12] if isinstance(row[12], dict) else {},
                x=float(row[14]),
                y=float(row[15]),
            )
            grouped_rows.setdefault((vehicle_id, subtrack_index), []).append(
                (int(row[2]), int(row[3]), int(row[4]), point)
            )
    grouped: dict[tuple[str, int], list[TrajectoryPoint]] = {}
    for key, rows in grouped_rows.items():
        ordered = sorted(rows, key=lambda item: item[:3])
        grouped[key] = [
            TrajectoryPoint(
                point_ref=item[3].point_ref,
                vehicle_id=item[3].vehicle_id,
                timestamp=item[3].timestamp,
                lon=item[3].lon,
                lat=item[3].lat,
                heading=item[3].heading,
                speed=item[3].speed,
                extra=item[3].extra,
                x=item[3].x,
                y=item[3].y,
                refined_point_ref=RefinedPointRef(
                    point_kind="injected" if item[1] == 1 else "kept",
                    point_id=item[2],
                    subtrack_index=key[1],
                    refined_sequence_in_subtrack=sequence,
                ),
            )
            for sequence, item in enumerate(ordered)
        ]
    return grouped
