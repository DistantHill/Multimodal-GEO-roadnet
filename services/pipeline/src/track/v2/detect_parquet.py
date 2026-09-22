"""Export committed results of one still-running Detect Run to staged Parquet v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb

from track.v2.artifact_parquet import build_descriptor
from track.v2.artifact_publication import StagingDataset, _validate_handle
from track.v2.config import PostgresConfig
from track.v2.db.artifact_registry import ArtifactDescriptor
from track.v2.db.source_table import validate_source_table
from track.v2.models import RuntimeConfig
from track.v2.refine_parquet import ParquetColumn, _literal


class DetectParquetError(RuntimeError):
    """The authoritative Detect result cannot be exported safely."""


DETECT_PARQUET_V1 = {
    "resolved_points": (
        ParquetColumn("detect_id", "UUID"),
        ParquetColumn("resolved_point_id", "BIGINT"),
        ParquetColumn("subtrack_index", "INTEGER"),
        ParquetColumn("sequence_in_subtrack", "BIGINT"),
        ParquetColumn("refined_sequence_in_subtrack", "BIGINT", required=False),
        ParquetColumn("vehicle_id", "VARCHAR"),
        ParquetColumn("lon", "DOUBLE"), ParquetColumn("lat", "DOUBLE"),
        ParquetColumn("point_kind", "VARCHAR"),
        ParquetColumn("refined_point_id", "BIGINT", required=False),
        ParquetColumn("source_row_key", "VARCHAR", required=False),
        ParquetColumn("crossing_id", "BIGINT", required=False),
        ParquetColumn("intersection_id", "BIGINT", required=False),
        ParquetColumn("crossing_type", "SMALLINT", required=False),
    ),
    "intersections": (
        ParquetColumn("detect_id", "UUID"),
        ParquetColumn("intersection_id", "BIGINT"),
        ParquetColumn("lon", "DOUBLE"), ParquetColumn("lat", "DOUBLE"),
        ParquetColumn("radius_m", "DOUBLE"),
    ),
    "entry_exit_points": (
        ParquetColumn("detect_id", "UUID"),
        ParquetColumn("crossing_id", "BIGINT"),
        ParquetColumn("intersection_id", "BIGINT"),
        ParquetColumn("resolved_point_id", "BIGINT"),
        ParquetColumn("segment_start_resolved_point_id", "BIGINT"),
        ParquetColumn("crossing_type", "SMALLINT"),
        ParquetColumn("x", "DOUBLE"), ParquetColumn("y", "DOUBLE"),
        ParquetColumn("lon", "DOUBLE"), ParquetColumn("lat", "DOUBLE"),
    ),
}


def _quote(value: str) -> str:
    if not value or "\x00" in value:
        raise DetectParquetError("current PostgreSQL schema is unavailable")
    return '"' + value.replace('"', '""') + '"'


def _metadata(conn: Any, detect_id: UUID) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.source_table, current_schema(), d.status "
            "FROM detect_runs d JOIN refine_runs r ON r.id=d.refine_id WHERE d.id=%s",
            (detect_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise DetectParquetError(f"unknown Detect Run: {detect_id}")
    if row[2] != "running":
        raise DetectParquetError("Detect Run must remain running during staging export")
    if not isinstance(row[1], str):
        raise DetectParquetError("current PostgreSQL schema is unavailable")
    return str(row[0]), _quote(row[1])


def _queries(detect_id: UUID, schema: str, source: str, key_type: str) -> dict[str, str]:
    run = _literal(str(detect_id)) + "::uuid"
    base = f"{schema}.resolved_subtrack_points"
    kept = f"{schema}.kept_trajectory_points"
    injected = f"{schema}.injected_points"
    crossing = f"{schema}.candidate_entry_exit_points"
    intersections = f"{schema}.intersections"
    candidates = f"{schema}.candidate_intersections"
    detects = f"{schema}.detect_runs"
    resolved = f"""
SELECT r.detect_id, r.id AS resolved_point_id, r.subtrack_index,
 r.sequence_in_subtrack, r.refined_sequence_in_subtrack,
 COALESCE(k.vehicle_id, ip.vehicle_id, sk.vehicle_id, sip.vehicle_id) AS vehicle_id,
 COALESCE(s.lon, ip.lon, c.crossing_lon) AS lon,
 COALESCE(s.lat, ip.lat, c.crossing_lat) AS lat,
 CASE WHEN r.kept_point_id IS NOT NULL THEN 'kept'
      WHEN r.injected_point_id IS NOT NULL THEN 'injected' ELSE 'crossing' END AS point_kind,
 COALESCE(r.kept_point_id, r.injected_point_id) AS refined_point_id,
 k.source_row_key, c.id AS crossing_id, i.id AS intersection_id, c.crossing_type
FROM {base} r JOIN {detects} d ON d.id=r.detect_id
LEFT JOIN {kept} k ON k.id=r.kept_point_id AND k.refine_id=d.refine_id
LEFT JOIN {source} s ON s.id=k.source_row_key::{key_type}
LEFT JOIN {injected} ip ON ip.id=r.injected_point_id AND ip.refine_id=d.refine_id
LEFT JOIN {crossing} c ON c.id=r.crossing_point_id AND c.detect_id=r.detect_id
LEFT JOIN {base} sr ON sr.id=c.segment_start_point_id AND sr.detect_id=r.detect_id
LEFT JOIN {kept} sk ON sk.id=sr.kept_point_id AND sk.refine_id=d.refine_id
LEFT JOIN {injected} sip ON sip.id=sr.injected_point_id AND sip.refine_id=d.refine_id
LEFT JOIN {intersections} i ON i.detect_id=c.detect_id
 AND i.candidate_intersection_id=c.candidate_intersection_id
WHERE r.detect_id={run}
""".strip()
    return {
        "resolved_points": resolved,
        "intersections": f"""
SELECT i.detect_id, i.id AS intersection_id, c.centroid_lon AS lon,
 c.centroid_lat AS lat, i.clip_radius_m AS radius_m
FROM {intersections} i JOIN {candidates} c
 ON c.id=i.candidate_intersection_id AND c.detect_id=i.detect_id
WHERE i.detect_id={run}
""".strip(),
        "entry_exit_points": f"""
SELECT c.detect_id, c.id AS crossing_id, i.id AS intersection_id,
 r.id AS resolved_point_id, c.segment_start_point_id AS segment_start_resolved_point_id,
 c.crossing_type, c.crossing_x AS x, c.crossing_y AS y,
 c.crossing_lon AS lon, c.crossing_lat AS lat
FROM {crossing} c JOIN {intersections} i
 ON i.detect_id=c.detect_id AND i.candidate_intersection_id=c.candidate_intersection_id
JOIN {base} r ON r.detect_id=c.detect_id AND r.crossing_point_id=c.id
WHERE c.detect_id={run}
""".strip(),
    }


def _relation(query: str, table: str) -> str:
    projection = ", ".join(
        f"{column.name}::{column.duckdb_type} AS {column.name}"
        for column in DETECT_PARQUET_V1[table]
    )
    order = {
        "resolved_points": "subtrack_index, sequence_in_subtrack, resolved_point_id",
        "intersections": "intersection_id",
        "entry_exit_points": "intersection_id, crossing_id",
    }[table]
    return (
        f"SELECT {projection} FROM postgres_query('authoritative_pg', {_literal(query)}) "
        f"ORDER BY {order}"
    )


def _open(
    runtime: RuntimeConfig,
    temp: Path,
    postgres_extension_path: Path,
) -> duckdb.DuckDBPyConnection:
    extension = postgres_extension_path
    if not extension.is_absolute() or not extension.is_file():
        raise DetectParquetError(
            "paths.duckdb_postgres_extension_path must name a bundled absolute file"
        )
    temp.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=":memory:", config={
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "memory_limit": runtime.duckdb_memory_limit, "threads": str(runtime.duckdb_threads),
        "temp_directory": str(temp),
        "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
    })
    connection.execute(f"LOAD {_literal(str(extension))}")
    return connection


def _copy(connection: duckdb.DuckDBPyConnection, relation: str, output: Path,
          table: str, runtime: RuntimeConfig) -> None:
    output.parent.mkdir(parents=True, exist_ok=False)
    options = ["FORMAT PARQUET"]
    if runtime.parquet_row_group_size_rows:
        options.append(f"ROW_GROUP_SIZE {runtime.parquet_row_group_size_rows}")
    connection.execute(f"COPY ({relation}) TO {_literal(str(output))} ({', '.join(options)})")
    view = f"read_parquet({_literal(str(output))})"
    required = [column.name for column in DETECT_PARQUET_V1[table] if column.required]
    if connection.execute(
        f"SELECT count(*) FROM {view} WHERE " + " OR ".join(f"{name} IS NULL" for name in required)
    ).fetchone()[0]:
        raise DetectParquetError(f"{table} contains null required fields")


def _validate_data(connection: duckdb.DuckDBPyConnection, outputs: dict[str, Path]) -> None:
    for name, path in outputs.items():
        connection.execute(
            f"CREATE TEMP VIEW {name} AS SELECT * FROM read_parquet({_literal(str(path))})"
        )
    invalid = connection.execute("""
      SELECT count(*) FROM resolved_points
      WHERE NOT isfinite(lon) OR NOT isfinite(lat) OR lon NOT BETWEEN -180 AND 180
         OR lat NOT BETWEEN -90 AND 90 OR point_kind NOT IN ('kept','injected','crossing')
         OR (point_kind='crossing') != (crossing_id IS NOT NULL AND intersection_id IS NOT NULL
             AND crossing_type IN (0,1) AND refined_point_id IS NULL
             AND refined_sequence_in_subtrack IS NULL AND source_row_key IS NULL)
         OR (point_kind IN ('kept','injected')) != (refined_point_id IS NOT NULL
             AND refined_sequence_in_subtrack IS NOT NULL AND crossing_id IS NULL
             AND intersection_id IS NULL AND crossing_type IS NULL)
         OR (point_kind='kept') != (source_row_key IS NOT NULL)
    """).fetchone()[0]
    invalid += connection.execute("""
      WITH checks AS (
        SELECT min(sequence_in_subtrack) lo, max(sequence_in_subtrack) hi,
               count(*) n, count(DISTINCT vehicle_id) vehicles
        FROM resolved_points GROUP BY subtrack_index)
      SELECT count(*) FROM checks WHERE lo<>0 OR hi<>n-1 OR vehicles<>1
    """).fetchone()[0]
    invalid += connection.execute("""
      SELECT count(*) FROM intersections WHERE NOT isfinite(lon) OR NOT isfinite(lat)
        OR lon NOT BETWEEN -180 AND 180 OR lat NOT BETWEEN -90 AND 90
        OR NOT isfinite(radius_m) OR radius_m<=0
    """).fetchone()[0]
    invalid += connection.execute("""
      SELECT count(*) FROM entry_exit_points e
      LEFT JOIN resolved_points r ON r.crossing_id=e.crossing_id
      LEFT JOIN intersections i ON i.intersection_id=e.intersection_id
      WHERE r.resolved_point_id IS NULL OR r.resolved_point_id<>e.resolved_point_id
         OR i.intersection_id IS NULL OR e.crossing_type NOT IN (0,1)
         OR NOT isfinite(e.x) OR NOT isfinite(e.y) OR NOT isfinite(e.lon) OR NOT isfinite(e.lat)
         OR e.lon NOT BETWEEN -180 AND 180 OR e.lat NOT BETWEEN -90 AND 90
    """).fetchone()[0]
    invalid += connection.execute("""
      SELECT (SELECT count(*) FROM resolved_points WHERE crossing_id IS NOT NULL)
           <> (SELECT count(*) FROM entry_exit_points)
    """).fetchone()[0]
    if invalid:
        raise DetectParquetError("Detect output violates the v1 Clip input semantics")


def _validate_schema(descriptor: ArtifactDescriptor) -> None:
    manifest = json.loads(descriptor.manifest_json)
    if manifest.get("version") != 1:
        raise DetectParquetError("unknown Detect Parquet manifest version")
    tables = {table["name"]: table for table in manifest["tables"]}
    if set(tables) != set(DETECT_PARQUET_V1):
        raise DetectParquetError("Detect descriptor has unexpected logical tables")
    for name, columns in DETECT_PARQUET_V1.items():
        expected = [{"name": c.name, "type": c.duckdb_type, "nullable": c.nullable} for c in columns]
        if tables[name]["schema"] != expected:
            raise DetectParquetError(f"{name} Parquet schema differs from v1")


def export_detect_parquet(conn: Any, postgres: PostgresConfig, handle: StagingDataset,
                          runtime: RuntimeConfig,
                          source_fingerprint: dict[str, object], *, data_root: Path,
                          postgres_extension_path: Path) -> ArtifactDescriptor:
    """Write and inspect staging only; never publish, finish, or control transactions.

    Detect result rows must already be committed and visible to postgres_scanner,
    while the Run remains ``running`` until C08b publishes and registers the
    artifact and completes the Run in its final PostgreSQL transaction.
    """
    _validate_handle(handle)
    if handle.stage != "detect":
        raise DetectParquetError("staging dataset must belong to the detect stage")
    source_table, schema = _metadata(conn, handle.run_id)
    contract = validate_source_table(conn, source_table)
    queries = _queries(handle.run_id, schema, contract.quoted_table, contract.key_sql_type)
    outputs = {name: handle.path / name / "part-00000.parquet" for name in DETECT_PARQUET_V1}
    with _open(
        runtime,
        data_root / "duckdb" / str(handle.run_id),
        postgres_extension_path,
    ) as connection:
        connection.execute(
            f"ATTACH {_literal(postgres.dsn())} AS authoritative_pg (TYPE postgres, READ_ONLY)"
        )
        for name, output in outputs.items():
            _copy(connection, _relation(queries[name], name), output, name, runtime)
        _validate_data(connection, outputs)
    descriptor = build_descriptor(
        handle,
        {name: [f"{name}/part-00000.parquet"] for name in DETECT_PARQUET_V1},
        source_fingerprint,
    )
    _validate_schema(descriptor)
    return descriptor
