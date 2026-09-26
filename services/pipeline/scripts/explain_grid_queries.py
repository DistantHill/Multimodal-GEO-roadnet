#!/usr/bin/env python3
"""Compare direct UTM-index grid reads with the existing two-stage query."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from track.v2.cli._startup import load_startup_config
from track.v2.db.source_table import validate_source_table
from track.v2.grid_cross_track_candidates import create_grid
from track.v2.models import SpatialContext
from track.v2.projection import CoordinateTransformer

REFINE_ID = UUID("6c14e329-04c7-45d1-87fe-8ab4ff7b767e")
PROCESSING_SRID = 32649
SOURCE_SRID = 4326
GRID_SIZE_M = 5000.0
STATEMENT_TIMEOUT_MS = 60_000
GRID_EXTENT = (298285.89554442314, 3829533.5790700926, 443285.89554442314, 4024533.5790700926)
TARGETS = [(3, 6), (4, 7), (7, 0), (7, 1), (2, 0), (3, 1), (3, 6), (4, 7)]
SPATIAL_CONTEXT = SpatialContext(
    source_srid=SOURCE_SRID,
    processing_srid=PROCESSING_SRID,
    projection_config={
        "source_srid": SOURCE_SRID,
        "processing_srid": PROCESSING_SRID,
        "enforce_area_of_use": True,
        "max_scale_error": 0.01,
        "coordinate_semantics_version": 2,
    },
)


def neighbour(direction: str, source: Any) -> str:
    comparator = "<" if direction == "prev" else ">"
    order = "DESC" if direction == "prev" else "ASC"
    srid = PROCESSING_SRID
    return f"""
    LEFT JOIN LATERAL (
      SELECT candidate.anchor, candidate.kind, candidate.point_id,
             candidate.source_row_key, candidate.x, candidate.y,
             candidate.heading, candidate.timestamp, candidate.speed,
             candidate.lon, candidate.lat, candidate.generated_reason,
             candidate.refined_sequence, candidate.source_id
      FROM (
        (SELECT k.sequence_in_subtrack AS anchor, 0 AS kind,
               k.id AS point_id, k.source_row_key,
               ST_X(ST_Transform(src.geom, {srid})) AS x,
               ST_Y(ST_Transform(src.geom, {srid})) AS y,
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
          ON src.id = k.source_row_key::{source.key_sql_type}
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
      ORDER BY candidate.anchor {order}, candidate.kind {order}, candidate.point_id {order}
      LIMIT 1
    ) {direction} ON TRUE
    """


def result_select(source: Any, hits_table: str = "hits") -> str:
    return f"""
    SELECT h.vehicle_id, h.subtrack_index, h.anchor, h.kind, h.point_id,
           h.source_row_key, h.x, h.y, h.heading, h.timestamp, h.speed,
           h.lon, h.lat, h.generated_reason, h.refined_sequence, h.source_id,
           prev.anchor, prev.kind, prev.point_id, prev.source_row_key,
           prev.x, prev.y, prev.heading, prev.timestamp, prev.speed,
           prev.lon, prev.lat, prev.generated_reason, prev.refined_sequence, prev.source_id,
           nxt.anchor, nxt.kind, nxt.point_id, nxt.source_row_key,
           nxt.x, nxt.y, nxt.heading, nxt.timestamp, nxt.speed,
           nxt.lon, nxt.lat, nxt.generated_reason, nxt.refined_sequence, nxt.source_id
    FROM {hits_table} h
    {neighbour('prev', source)}
    {neighbour('nxt', source)}
    ORDER BY h.vehicle_id, h.subtrack_index, h.anchor, h.kind, h.point_id
    """


def hits_cte(source: Any, box: tuple[float, float, float, float], strategy: str) -> tuple[str, tuple[Any, ...]]:
    x_min, y_min, x_max, y_max = box
    lon_min, lat_min, lon_max, lat_max = CoordinateTransformer(SPATIAL_CONTEXT).inverse_bounds(box)
    projected = f"ST_Transform(s.geom, {PROCESSING_SRID})"
    if strategy == "utm_direct":
        source_filter = f"ST_Intersects({projected}, ST_MakeEnvelope(%s, %s, %s, %s, {PROCESSING_SRID}))"
        source_params: tuple[Any, ...] = (x_min, y_min, x_max, y_max)
    elif strategy == "source_bbox":
        source_filter = (
            f"s.geom && ST_MakeEnvelope(%s, %s, %s, %s, {SOURCE_SRID}) "
            f"AND ST_X({projected}) >= %s AND ST_Y({projected}) >= %s "
            f"AND ST_X({projected}) <= %s AND ST_Y({projected}) <= %s"
        )
        source_params = (lon_min, lat_min, lon_max, lat_max, x_min, y_min, x_max, y_max)
    else:
        raise ValueError(f"unknown strategy: {strategy}")
    cte = f"""
    WITH hits AS (
      SELECT k.vehicle_id, k.subtrack_index, k.sequence_in_subtrack AS anchor,
             0 AS kind, k.id AS point_id, k.source_row_key,
             ST_X({projected}) AS x, ST_Y({projected}) AS y,
             s.heading, s.timestamp, s.speed, s.lon, s.lat, NULL::text AS generated_reason,
             k.sequence_in_subtrack + (
               SELECT COUNT(*) FROM injected_points rank_i
               WHERE rank_i.refine_id = k.refine_id
                 AND rank_i.subtrack_index = k.subtrack_index
                 AND rank_i.after_sequence_in_subtrack < k.sequence_in_subtrack
             ) AS refined_sequence,
             '{source.source_table}'::text AS source_id
      FROM {source.quoted_table} s
      JOIN kept_trajectory_points k
        ON s.id = k.source_row_key::{source.key_sql_type} AND k.refine_id = %s
      WHERE {source_filter}
      UNION ALL
      SELECT i.vehicle_id, i.subtrack_index, i.after_sequence_in_subtrack,
             1, i.id, NULL::text, i.x, i.y, i.heading, i.timestamp, i.speed,
             i.lon, i.lat, i.generated_reason,
             i.after_sequence_in_subtrack + (
               SELECT COUNT(*) FROM injected_points rank_i
               WHERE rank_i.refine_id = i.refine_id
                 AND rank_i.subtrack_index = i.subtrack_index
                 AND rank_i.after_sequence_in_subtrack <= i.after_sequence_in_subtrack
             ) AS refined_sequence,
             'public.injected_points'::text AS source_id
      FROM injected_points i
      WHERE i.refine_id = %s AND i.geom && ST_MakeEnvelope(%s, %s, %s, %s, {SOURCE_SRID})
        AND i.x >= %s AND i.y >= %s AND i.x <= %s AND i.y <= %s
    )
    """
    params = (REFINE_ID, *source_params, REFINE_ID, lon_min, lat_min, lon_max, lat_max,
              x_min, y_min, x_max, y_max)
    return cte, params


def digest_rows(cursor: Any) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    while rows := cursor.fetchmany(4096):
        for row in rows:
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
    return {"row_count": count, "sha256": digest.hexdigest()}


def fetch_named(conn: Any, query: str, params: tuple[Any, ...]) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    with conn.cursor(name="grid_result_" + uuid.uuid4().hex) as cursor:
        cursor.itersize = 4096
        cursor.execute(query, params)
        result = digest_rows(cursor)
    return result, time.perf_counter() - started


def explain(conn: Any, query: str, params: tuple[Any, ...]) -> dict[str, Any]:
    started = time.perf_counter()
    with conn.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) " + query, params)
        plan = cursor.fetchone()[0]
    return {"client_elapsed_s": time.perf_counter() - started, "plan": plan}


def walk_plan(node: dict[str, Any], output: list[dict[str, Any]]) -> None:
    output.append({
        "node_type": node.get("Node Type"), "relation": node.get("Relation Name"),
        "index": node.get("Index Name"), "actual_rows": node.get("Actual Rows"),
        "actual_loops": node.get("Actual Loops"),
        "shared_hit_blocks": node.get("Shared Hit Blocks"), "shared_read_blocks": node.get("Shared Read Blocks"),
        "temp_read_blocks": node.get("Temp Read Blocks"), "temp_written_blocks": node.get("Temp Written Blocks"),
    })
    for child in node.get("Plans", []):
        walk_plan(child, output)


def assert_target(conn: Any, source_name: str) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_schema()")
        database, schema = cursor.fetchone()
        cursor.execute("""
          SELECT index.indexrelid::regclass::text, pg_get_indexdef(index.indexrelid),
                 index.indisvalid, index.indisready, pg_get_expr(index.indexprs, index.indrelid)
          FROM pg_index index
          JOIN pg_class table_rel ON table_rel.oid = index.indrelid
          JOIN pg_namespace table_ns ON table_ns.oid = table_rel.relnamespace
          WHERE index.indexrelid = to_regclass('public.idx_day01_geom_utm32649')
            AND table_ns.nspname = 'public' AND table_rel.relname = 'day01'
        """)
        row = cursor.fetchone()
    if str(database) != "roadnet_test" or str(schema) != "public":
        raise RuntimeError(f"expected roadnet_test.public, got {database}.{schema}")
    if source_name not in {"public.day01", "day01"}:
        raise RuntimeError(f"Refine source is {source_name!r}, expected public.day01")
    if row is None or not bool(row[2]) or not bool(row[3]):
        raise RuntimeError("expected valid public.idx_day01_geom_utm32649 is missing")
    expression = str(row[4] or "").lower().replace(" ", "")
    if "gist" not in str(row[1]).lower() or "st_transform" not in expression or "32649" not in expression:
        raise RuntimeError(f"index is not GiST(ST_Transform(geom, 32649)): {row[1]}")
    return {"database": str(database), "schema": str(schema), "name": str(row[0]),
            "definition": str(row[1]), "expression": str(row[4]), "valid": bool(row[2]), "ready": bool(row[3])}


def run_target(conn: Any, ordinal: int, grid_index: tuple[int, int], box: tuple[float, float, float, float], source: Any) -> dict[str, Any]:
    staged_hits, staged_params = hits_cte(source, box, "source_bbox")
    direct_hits, direct_params = hits_cte(source, box, "utm_direct")
    temp_name = "cross_track_hits_" + uuid.uuid4().hex
    staged_query = result_select(source, temp_name)
    direct_query = direct_hits + result_select(source)
    neighbour_params = (REFINE_ID,) * 4
    order = ("direct", "two_stage") if ordinal % 2 else ("two_stage", "direct")
    measured: dict[str, Any] = {}
    for strategy in order:
        if strategy == "two_stage":
            total_started = time.perf_counter()
            create_started = time.perf_counter()
            with conn.cursor() as cursor:
                cursor.execute(f"CREATE TEMP TABLE {temp_name} ON COMMIT DROP AS " + staged_hits + " SELECT * FROM hits", staged_params)
            create_elapsed = time.perf_counter() - create_started
            result, cursor_elapsed = fetch_named(conn, staged_query, neighbour_params)
            measured[strategy] = {"hits_temp_create_wall_s": create_elapsed,
                                  "cursor_fetch_wall_s": cursor_elapsed,
                                  "total_wall_s": time.perf_counter() - total_started,
                                  "result": result}
        else:
            result, elapsed = fetch_named(conn, direct_query, direct_params + neighbour_params)
            measured[strategy] = {"total_wall_s": elapsed, "result": result}
    if measured["direct"]["result"] != measured["two_stage"]["result"]:
        raise RuntimeError(f"ordered output mismatch: direct={measured['direct']['result']} two_stage={measured['two_stage']['result']}")

    staged_hits_plan = explain(conn, staged_hits + " SELECT * FROM hits", staged_params)
    staged_cursor_plan = explain(conn, staged_query, neighbour_params)
    direct_plan = explain(conn, direct_query, direct_params + neighbour_params)
    for item in (staged_hits_plan, staged_cursor_plan, direct_plan):
        item["plan_nodes"] = []
        walk_plan(item["plan"][0]["Plan"], item["plan_nodes"])
    direct_plan["utm_gist_index_used"] = any(
        node.get("index") == "idx_day01_geom_utm32649"
        for node in direct_plan["plan_nodes"]
    )
    direct_plan["utm_gist_index_nodes"] = [
        node for node in direct_plan["plan_nodes"]
        if node.get("index") == "idx_day01_geom_utm32649"
    ]
    return {
        "ordinal": ordinal, "grid": grid_index, "aabb_utm_m": box,
        "measured_order": order, "output_equal_ordered_rows": True,
        "utm_gist_index_used": direct_plan["utm_gist_index_used"],
        "output": measured["direct"]["result"],
        "timings": {
            "direct_named_cursor_fetch_wall_s": measured["direct"]["total_wall_s"],
            "two_stage_temp_hits_wall_s": measured["two_stage"]["hits_temp_create_wall_s"],
            "two_stage_named_cursor_fetch_wall_s": measured["two_stage"]["cursor_fetch_wall_s"],
            "two_stage_total_wall_s": measured["two_stage"]["total_wall_s"],
        },
        "explain_analyze": {
            "two_stage_hits_select_outside_named_cursor": staged_hits_plan,
            "two_stage_temp_table_cursor_select_outside_named_cursor": staged_cursor_plan,
            "direct_complete_select_outside_named_cursor": direct_plan,
        },
        "queries": {"two_stage_hits_select": staged_hits + " SELECT * FROM hits",
                    "two_stage_cursor_select": staged_query, "direct_complete_select": direct_query},
    }


def main() -> int:
    os.environ["PIPELINE_CONFIG_PATH"] = str(Path(__file__).resolve().parents[1] / "config/pipeline-roadnet-test.toml")
    config = load_startup_config()
    grids = create_grid(GRID_EXTENT, GRID_SIZE_M)
    output_dir = Path(__file__).resolve().parents[1] / "generated/cross-track-explain"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "refine_id": str(REFINE_ID), "targets": TARGETS,
        "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
        "methodology": {
            "timed_paths": "Both complete result sets are streamed and hashed with server-side named cursors; client wall time includes execute and all fetches.",
            "explain_analyze": "Separate EXPLAIN ANALYZE executions use ordinary unnamed cursors. Actual Loops/Rows and buffers are server plan data, not named-cursor fetch timings.",
            "two_stage": "Time CREATE TEMP TABLE AS hits and the following named-cursor neighbor query separately and together.",
            "equality": "SHA-256 over length-framed JSON encodings of every complete ordered output row, including neighbors and injected points.",
            "index": "Verify the pre-existing public.idx_day01_geom_utm32649 definition and validity; never create or rebuild it.",
        },
        "index": None, "results": [], "complete": False,
    }
    try:
        with psycopg.connect(config.postgres.dsn()) as connection:
            with connection.transaction():
                connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(STATEMENT_TIMEOUT_MS),))
                with connection.cursor() as cursor:
                    cursor.execute("SELECT source_table FROM refine_runs WHERE id = %s", (REFINE_ID,))
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError(f"unknown Refine Run: {REFINE_ID}")
                source_name = str(row[0])
                summary["index"] = assert_target(connection, source_name)
                source = validate_source_table(connection, source_name)
                for ordinal, grid_index in enumerate(TARGETS, start=1):
                    box = tuple(float(value) for value in grids[grid_index[0]][grid_index[1]])
                    print(f"[{ordinal}/{len(TARGETS)}] grid={grid_index} aabb_utm_m={box}", flush=True)
                    result = run_target(connection, ordinal, grid_index, box, source)
                    summary["results"].append(result)
                    path = output_dir / f"grid_compare_{ordinal}_{grid_index[0]}_{grid_index[1]}.json"
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                    print("  rows={row_count} sha256={sha256} direct_s={direct:.3f} two_stage_s={staged:.3f} equality=PASS".format(
                        **result["output"], direct=result["timings"]["direct_named_cursor_fetch_wall_s"],
                        staged=result["timings"]["two_stage_total_wall_s"]), flush=True)
        summary["utm_gist_index_used_targets"] = sum(
            bool(item["utm_gist_index_used"]) for item in summary["results"]
        )
        summary["complete"] = True
    except BaseException as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        path = output_dir / "grid_compare_summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"ERROR: {summary['error']}", file=sys.stderr, flush=True)
        print(f"partial_report={path}", file=sys.stderr, flush=True)
        return 1
    path = output_dir / "grid_compare_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"comparison=PASS targets={len(summary['results'])} report={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
