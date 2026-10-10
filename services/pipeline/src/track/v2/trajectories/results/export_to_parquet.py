"""Export one running Refine Run into staged Parquet files."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb

from track.v2.artifacts.parquet_manifest import build_descriptor
from track.v2.artifacts.parquet_sql import quote_sql_literal
from track.v2.artifacts.publish_dataset import StagingDataset, _validate_handle
from track.v2.config import PostgresConfig
from track.v2.db.artifact_registry import ArtifactDescriptor
from track.v2.db.source_table import SourceTableContract, validate_source_table
from track.v2.models import RuntimeConfig, SpatialContext
from track.v2.projection import spatial_context_from_values

from . import RefineParquetError
from .parquet_schema import REFINE_PARQUET_V2, _validate_schema


def _quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise RefineParquetError("current PostgreSQL schema is unavailable")
    return '"' + value.replace('"', '""') + '"'


def _run_metadata(conn: Any, refine_id: UUID) -> tuple[str, SpatialContext, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, source_srid, processing_srid, projection_config, status, "
            "current_schema() "
            "FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RefineParquetError(f"unknown Refine Run: {refine_id}")
    if str(row[4]) != "running":
        raise RefineParquetError("Refine Run must be running during staging export")
    if not str(row[0]).strip():
        raise RefineParquetError("Refine Run source metadata is required")
    if not isinstance(row[5], str):
        raise RefineParquetError("current PostgreSQL schema is unavailable")
    result_schema = _quote_identifier(row[5])
    try:
        spatial_context = spatial_context_from_values(row[1], row[2], row[3])
    except (TypeError, ValueError) as exc:
        raise RefineParquetError("Refine Run processing_srid metadata is invalid") from exc
    return str(row[0]), spatial_context, result_schema


def _postgres_query(
    refine_id: UUID, contract: SourceTableContract, srid: int, result_schema: str,
) -> str:
    run = quote_sql_literal(str(refine_id))
    return f"""
WITH points AS (
 SELECT k.refine_id, k.id AS point_id, k.vehicle_id, k.subtrack_index,
        k.sequence_in_subtrack AS anchor_sequence, 0 AS point_kind,
        s.timestamp, s.lon, s.lat,
        k.x AS x,
        k.y AS y,
        k.heading_cal, s.heading, s.speed, k.source_row_key AS source_row_key, NULL::text AS generated_reason,
        NULL::integer AS after_sequence_in_subtrack
 FROM {result_schema}.kept_trajectory_points k JOIN {contract.quoted_table} s
   ON s.id = k.source_row_key
 WHERE k.refine_id = {run}::uuid
 UNION ALL
 SELECT i.refine_id, i.id, i.vehicle_id, i.subtrack_index,
        i.after_sequence_in_subtrack, 1, i.timestamp, i.lon, i.lat, i.x, i.y,
        i.heading_cal, i.heading, i.speed, NULL::bigint, i.generated_reason,
        i.after_sequence_in_subtrack
 FROM {result_schema}.injected_points i WHERE i.refine_id = {run}::uuid
), sequenced AS (
 SELECT *, row_number() OVER (
   PARTITION BY subtrack_index
   ORDER BY anchor_sequence, point_kind, point_id
 ) - 1 AS refined_sequence_in_subtrack
 FROM points
)
SELECT * FROM sequenced""".strip()


def _relation(postgres_sql: str, table: str) -> str:
    columns = REFINE_PARQUET_V2[table]
    projection = ", ".join(f"{column.name}::{column.duckdb_type} AS {column.name}" for column in columns)
    point_kind = 0 if table == "kept_points" else 1
    return (
        f"SELECT {projection} FROM postgres_query('authoritative_pg', {quote_sql_literal(postgres_sql)}) "
        f"WHERE point_kind = {point_kind} "
        "ORDER BY subtrack_index, refined_sequence_in_subtrack, point_id"
    )


def _open_duckdb(
    runtime: RuntimeConfig,
    temp_directory: Path,
    postgres_extension_path: Path,
) -> duckdb.DuckDBPyConnection:
    extension = postgres_extension_path
    if not extension.is_absolute() or not extension.is_file():
        raise RefineParquetError(
            "paths.duckdb_postgres_extension_path must name a bundled absolute file"
        )
    temp_directory.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=":memory:", config={
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "memory_limit": runtime.duckdb_memory_limit, "threads": str(runtime.duckdb_threads),
        "temp_directory": str(temp_directory),
        "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
    })
    connection.execute(f"LOAD {quote_sql_literal(str(extension))}")
    return connection


def _copy_table(
    connection: duckdb.DuckDBPyConnection, relation: str, output: Path,
    table: str, runtime: RuntimeConfig,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=False)
    options = ["FORMAT PARQUET"]
    if runtime.parquet_row_group_size_rows:
        options.append(f"ROW_GROUP_SIZE {runtime.parquet_row_group_size_rows}")
    connection.execute(f"COPY ({relation}) TO {quote_sql_literal(str(output))} ({', '.join(options)})")
    null_predicate = " OR ".join(f"{column.name} IS NULL" for column in REFINE_PARQUET_V2[table] if column.required)
    if connection.execute(
        f"SELECT count(*) FROM read_parquet({quote_sql_literal(str(output))}) WHERE {null_predicate}"
    ).fetchone()[0]:
        raise RefineParquetError(f"{table} contains null output fields")


def export_refine_parquet(
    conn: Any, postgres: PostgresConfig, handle: StagingDataset,
    runtime: RuntimeConfig, source_fingerprint: dict[str, object],
    *, data_root: Path, postgres_extension_path: Path,
    short_files: list[str] | None = None,
) -> ArtifactDescriptor:
    """Write and inspect staging only; never publish, commit, rollback, or finish.

    The Refine rows must already be committed and visible while the Run remains
    ``running``: postgres_scanner uses its own read-only PostgreSQL connection.
    C05b owns that pre-export commit and the later publication transaction.
    """
    _validate_handle(handle)
    if handle.stage != "refine":
        raise RefineParquetError("staging dataset must belong to the refine stage")
    source_table, spatial_context, result_schema = _run_metadata(conn, handle.run_id)
    srid = spatial_context.processing_srid
    if short_files is None:
        from .short_subtracks import ShortSubtrackWriter
        short_files = ShortSubtrackWriter(handle, runtime, spatial_context).finish()
    contract = validate_source_table(conn, source_table)
    postgres_sql = _postgres_query(handle.run_id, contract, srid, result_schema)
    outputs = {
        name: f"{name}/part-00000.parquet" for name in REFINE_PARQUET_V2
    }
    # C05a deliberately writes one file per logical table. Target file sizing
    # remains zero/engine-default until the benchmark introduces partitioning.
    with _open_duckdb(
        runtime,
        data_root / "duckdb" / str(handle.run_id),
        postgres_extension_path,
    ) as connection:
        connection.execute(
            f"ATTACH {quote_sql_literal(postgres.dsn())} AS authoritative_pg "
            "(TYPE postgres, READ_ONLY)"
        )
        for name, relative in outputs.items():
            _copy_table(
                connection, _relation(postgres_sql, name),
                handle.path / relative, name, runtime,
            )
    descriptor = build_descriptor(
        handle, {**{name: [relative] for name, relative in outputs.items()},
                 "short_subtrack": short_files}, source_fingerprint,
    )
    _validate_schema(descriptor)
    return descriptor
