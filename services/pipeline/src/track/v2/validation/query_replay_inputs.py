"""Read frozen Refine points named by replay manifests."""

from __future__ import annotations

from typing import Any
import numpy as np

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..models import SpatialContext, RefinementSubtrack
from ..trajectories.subtrack_columns import refinement_subtrack_from_columns
from ..db.source_table import validate_source_table
from ..db._point_query_fragments import _INJECTED_SOURCE_ID


@observed_operation("读取 Refine 点序列")
def fetch_refine_points(
    conn: object,
    table: str,
    refine_id: str,
    *,
    spatial_context: SpatialContext,
) -> dict[tuple[str, int], RefinementSubtrack]:
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
) -> dict[tuple[str, int], RefinementSubtrack]:
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
) -> dict[tuple[str, int], RefinementSubtrack]:
    """Execute *sql* and locally order points within each subtrack.

    Avoiding a global PostgreSQL sort is material for large Refine runs: the join
    rows are wide and routinely spill hundreds of megabytes to temporary files.
    Subtracks are small, so ordering their members independently in Python has
    the same semantics with much lower sort pressure.
    """
    grouped_columns: dict[tuple[str, int], list[list]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row_count, row in enumerate(cur, 1):
            if row_count % 10_000 == 0:
                heartbeat_progress(row_count)
            if any(row[index] is None for index in range(7, 12)):
                raise ValueError(f"Refine Run {refine_id} references a missing source row")
            key = (str(row[0]), int(row[1]))
            columns = grouped_columns.setdefault(key, [[] for _ in range(16)])
            for column, value in zip(columns, row):
                column.append(value)
    grouped = {}
    for key, lists in grouped_columns.items():
        columns = [np.asarray(values, dtype=object) for values in lists]
        order = np.lexsort((columns[4].astype(np.int64), columns[3].astype(np.int64),
                            columns[2].astype(np.int64)))
        columns = [column[order] for column in columns]
        injected = columns[3].astype(np.int64) == 1
        grouped[key] = refinement_subtrack_from_columns(
            vehicle_id=key[0], subtrack_index=key[1], source_id=table_source(columns),
            xy=np.column_stack((columns[14], columns[15])).astype(np.float64),
            lonlat=np.column_stack((columns[8], columns[9])).astype(np.float64),
            timestamp=columns[7].astype(np.float64), heading=columns[10].astype(np.float64),
            speed=columns[11].astype(np.float64), kind=np.where(injected, 'i', 'p'),
            source_row_key=np.where(injected, 0, columns[6]).astype(np.int64),
            refine_point_id=columns[4].astype(np.int64),
            after_sequence=np.where(injected, columns[2], -1).astype(np.int64),
            generated_reason=columns[13])
    return grouped


def table_source(columns: list[np.ndarray]) -> str:
    kept = np.flatnonzero(columns[3].astype(np.int64) == 0)
    return str(columns[5][int(kept[0])]) if len(kept) else str(columns[5][0])
