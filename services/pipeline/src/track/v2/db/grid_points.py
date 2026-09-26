"""Read one Detect grid's Refine points and their subtrack neighbours."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from ..models import SpatialContext
from ..projection import CoordinateTransformer
from .source_table import validate_source_table


def fetch_points_in_grid(
    conn: Any,
    grid_index: tuple[int, int],
    grids: Any,
    refine_id: UUID,
    spatial_context: SpatialContext,
    *,
    batch_size: int = 4096,
) -> Iterator[dict[str, tuple[Any, ...]]]:
    """Yield bounded column batches of in-grid points and adjacent Refine points.

    ``grids[col][row]`` supplies ``(x_min, y_min, x_max, y_max)`` in the
    processing CRS. Each row represents one point in that closed rectangle;
    ``prev_*`` and ``next_*`` are its immediate neighbours in the same
    subtrack, including neighbours outside the rectangle. Missing neighbours
    have null columns. The caller owns ``conn`` and its transaction.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    col, row = grid_index
    if (
        not isinstance(col, int) or not isinstance(row, int)
        or col < 0 or col >= len(grids)
        or row < 0 or row >= len(grids[col])
    ):
        raise IndexError("grid_index is outside grids")
    x_min, y_min, x_max, y_max = (float(v) for v in grids[col][row])
    if not all(math.isfinite(v) for v in (x_min, y_min, x_max, y_max)):
        raise ValueError("grid AABB must be finite")
    if x_min > x_max or y_min > y_max:
        raise ValueError("grid AABB bounds are reversed")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, processing_srid FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        metadata = cur.fetchone()
    if metadata is None:
        raise ValueError(f"unknown Refine Run: {refine_id}")
    if int(metadata[1]) != int(spatial_context.processing_srid):
        raise ValueError("Refine Run processing CRS differs from spatial_context")
    source = validate_source_table(conn, str(metadata[0]))
    lon_min, lat_min, lon_max, lat_max = CoordinateTransformer(
        spatial_context
    ).inverse_bounds((x_min, y_min, x_max, y_max))
    envelope = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
    projected = f"ST_Transform(s.geom, {int(spatial_context.processing_srid)})"
    # Refine order is (anchor_sequence, point_kind, point_id), with injected
    # points following the kept point at their anchor (refine_parquet.py).
    def neighbour(direction: str) -> str:
        comparator = "<" if direction == "prev" else ">"
        order = "DESC" if direction == "prev" else "ASC"
        return f"""
        LEFT JOIN LATERAL (
          SELECT candidate.anchor, candidate.kind, candidate.point_id,
                 candidate.source_row_key, candidate.x, candidate.y,
                 candidate.heading, candidate.timestamp, candidate.speed,
                 candidate.lon, candidate.lat, candidate.generated_reason,
                 candidate.refined_sequence, candidate.source_id
          FROM (
            (SELECT k.sequence_in_subtrack AS anchor, 0 AS kind,
                   k.id AS point_id, k.source_row_key::text AS source_row_key,
                   ST_X(ST_Transform(src.geom, {int(spatial_context.processing_srid)})) AS x,
                   ST_Y(ST_Transform(src.geom, {int(spatial_context.processing_srid)})) AS y,
                   src.heading, src.timestamp, src.speed, src.lon, src.lat,
                   NULL::text AS generated_reason,
                   k.sequence_in_subtrack + (
                     SELECT COUNT(*) FROM injected_points rank_i
                     WHERE rank_i.refine_id = k.refine_id
                       AND rank_i.subtrack_index = k.subtrack_index
                       AND rank_i.after_sequence_in_subtrack < k.sequence_in_subtrack
                   ) AS refined_sequence,
                   '{source.source_table}'::text AS source_id
            FROM kept_trajectory_points k
            JOIN {source.quoted_table} src
              ON src.id = k.source_row_key
            WHERE k.refine_id = %s AND k.subtrack_index = h.subtrack_index
              AND ROW(k.sequence_in_subtrack, 0, k.id)
                  {comparator} ROW(h.anchor, h.kind, h.point_id)
            ORDER BY k.sequence_in_subtrack {order}, k.id {order}
            LIMIT 1)
            UNION ALL
            (SELECT i.after_sequence_in_subtrack, 1, i.id, NULL::text,
                 i.x, i.y, i.heading, i.timestamp, i.speed, i.lon, i.lat,
                 i.generated_reason,
                 i.after_sequence_in_subtrack + (
                   SELECT COUNT(*) FROM injected_points rank_i
                   WHERE rank_i.refine_id = i.refine_id
                     AND rank_i.subtrack_index = i.subtrack_index
                     AND rank_i.after_sequence_in_subtrack <= i.after_sequence_in_subtrack
                 ) AS refined_sequence,
                 'public.injected_points'::text AS source_id
          FROM injected_points i
          WHERE i.refine_id = %s AND i.subtrack_index = h.subtrack_index
            AND ROW(i.after_sequence_in_subtrack, 1, i.id)
                {comparator} ROW(h.anchor, h.kind, h.point_id)
          ORDER BY i.after_sequence_in_subtrack {order}, i.id {order}
            LIMIT 1)
        ) candidate
        ORDER BY candidate.anchor {order}, candidate.kind {order},
                 candidate.point_id {order}
        LIMIT 1
        ) {direction} ON TRUE
        """

    # Parentheses keep each branch's ORDER BY / LIMIT local to its index probe.
    hits_cte = f"""
    WITH hits AS (
      SELECT k.vehicle_id, k.subtrack_index,
             k.sequence_in_subtrack AS anchor, 0 AS kind, k.id AS point_id,
             k.source_row_key::text AS source_row_key, ST_X({projected}) AS x,
             ST_Y({projected}) AS y, s.heading, s.timestamp, s.speed,
             s.lon, s.lat, NULL::text AS generated_reason,
             k.sequence_in_subtrack + (
               SELECT COUNT(*) FROM injected_points rank_i
               WHERE rank_i.refine_id = k.refine_id
                 AND rank_i.subtrack_index = k.subtrack_index
                 AND rank_i.after_sequence_in_subtrack < k.sequence_in_subtrack
             ) AS refined_sequence,
             '{source.source_table}'::text AS source_id
      FROM {source.quoted_table} s
      JOIN kept_trajectory_points k
        ON s.id = k.source_row_key AND k.refine_id = %s
      WHERE s.geom && {envelope}
        AND ST_X({projected}) >= %s AND ST_Y({projected}) >= %s
        AND ST_X({projected}) <= %s AND ST_Y({projected}) <= %s
      UNION ALL
      SELECT i.vehicle_id, i.subtrack_index, i.after_sequence_in_subtrack,
             1, i.id, NULL::text, i.x, i.y, i.heading,
             i.timestamp, i.speed, i.lon, i.lat, i.generated_reason,
             i.after_sequence_in_subtrack + (
               SELECT COUNT(*) FROM injected_points rank_i
               WHERE rank_i.refine_id = i.refine_id
                 AND rank_i.subtrack_index = i.subtrack_index
                 AND rank_i.after_sequence_in_subtrack <= i.after_sequence_in_subtrack
             ) AS refined_sequence,
             'public.injected_points'::text AS source_id
      FROM injected_points i
      WHERE i.refine_id = %s AND i.geom && {envelope}
        AND i.x >= %s AND i.y >= %s AND i.x <= %s AND i.y <= %s
    )
    """
    # A server-side cursor is planned for a small initial fetch. For this
    # query that can choose a nested loop which rescans the entire Refine kept
    # set for every spatial hit. Build hits with a regular statement first;
    # the cursor then streams only the neighbour lookups.
    hits_table = f"cross_track_hits_{uuid.uuid4().hex}"
    query = f"""
    SELECT h.vehicle_id, h.subtrack_index, h.anchor, h.kind, h.point_id,
           h.source_row_key, h.x, h.y, h.heading, h.timestamp, h.speed,
           h.lon, h.lat, h.generated_reason, h.refined_sequence, h.source_id,
           prev.anchor, prev.kind, prev.point_id, prev.source_row_key,
           prev.x, prev.y, prev.heading, prev.timestamp, prev.speed,
           prev.lon, prev.lat, prev.generated_reason, prev.refined_sequence,
           prev.source_id,
           nxt.anchor, nxt.kind, nxt.point_id, nxt.source_row_key,
           nxt.x, nxt.y, nxt.heading, nxt.timestamp, nxt.speed,
           nxt.lon, nxt.lat, nxt.generated_reason, nxt.refined_sequence,
           nxt.source_id
    FROM {hits_table} h
    {neighbour('prev')}
    {neighbour('nxt')}
    ORDER BY h.vehicle_id, h.subtrack_index, h.anchor, h.kind, h.point_id
    """
    bounds = (lon_min, lat_min, lon_max, lat_max)
    xy = (x_min, y_min, x_max, y_max)
    hit_params = (
        refine_id, *bounds, *xy, refine_id, *bounds, *xy,
    )
    neighbour_params = (refine_id,) * 4
    names = (
        "vehicle_id", "subtrack_index", "anchor_sequence", "point_kind",
        "point_id", "source_row_key", "x", "y", "heading", "timestamp",
        "speed", "lon", "lat", "generated_reason", "refined_sequence_in_subtrack",
        "source_id",
        "prev_anchor_sequence", "prev_point_kind", "prev_point_id",
        "prev_source_row_key", "prev_x", "prev_y", "prev_heading",
        "prev_timestamp", "prev_speed", "prev_lon", "prev_lat",
        "prev_generated_reason", "prev_refined_sequence_in_subtrack", "prev_source_id",
        "next_anchor_sequence", "next_point_kind", "next_point_id",
        "next_source_row_key", "next_x", "next_y", "next_heading",
        "next_timestamp", "next_speed", "next_lon", "next_lat",
        "next_generated_reason", "next_refined_sequence_in_subtrack", "next_source_id",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE {hits_table} ON COMMIT DROP AS "
            + hits_cte + " SELECT * FROM hits",
            hit_params,
        )
    with conn.cursor(name=f"cross_track_grid_{uuid.uuid4().hex}") as cur:
        cur.execute(query, neighbour_params)
        while rows := cur.fetchmany(batch_size):
            yield {name: tuple(row[index] for row in rows)
                   for index, name in enumerate(names)}
