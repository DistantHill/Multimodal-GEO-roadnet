"""Export one running Refine Run from PostgreSQL into staged Parquet v1."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import duckdb

from track.v2.artifact_parquet import build_descriptor, validate_published
from track.v2.artifact_publication import StagingDataset, _validate_handle
from track.v2.config import PostgresConfig
from track.v2.db.artifact_registry import ArtifactDescriptor
from track.v2.db.source_table import SourceTableContract, validate_source_table
from track.v2.models import PointRef, RefinedPointRef, RuntimeConfig, TrajectoryPoint
from track.v2.projection import spatial_context_from_values


class RefineParquetError(RuntimeError):
    """The authoritative Refine result cannot be exported safely."""


class RefineDatasetReader:
    """Own a validated, read-only DuckDB view of one Refine v1 dataset."""

    def __init__(
        self, publication_root: Path | str, descriptor: ArtifactDescriptor, state: str,
        runtime: RuntimeConfig, *, verify_sha256: bool = False,
    ) -> None:
        validate_published(
            publication_root, descriptor, state, verify_sha256=verify_sha256,
        )
        if descriptor.stage != "refine" or descriptor.refine_id is None:
            raise RefineParquetError("dataset must belong to the refine stage")
        _validate_schema(descriptor)
        self._descriptor = descriptor
        self._runtime = runtime
        self._root = Path(publication_root).resolve(strict=True)
        self._connection: duckdb.DuckDBPyConnection | None = None

    @property
    def descriptor(self) -> ArtifactDescriptor:
        """Return the validated immutable descriptor for provenance checks."""
        return self._descriptor

    def __enter__(self) -> RefineDatasetReader:
        if self._connection is not None:
            raise RefineParquetError("dataset reader is already open")
        run_id = self._descriptor.refine_id
        temp = self._root.parent / "duckdb" / str(run_id)
        temp.mkdir(parents=True, exist_ok=True)
        self._connection = duckdb.connect(database=":memory:", config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "memory_limit": self._runtime.duckdb_memory_limit,
            "threads": str(self._runtime.duckdb_threads),
            "temp_directory": str(temp),
            "max_temp_directory_size": self._runtime.duckdb_max_temp_directory_size,
        })
        try:
            manifest = json.loads(self._descriptor.manifest_json)
            final = self._root / self._descriptor.relative_path
            for table in manifest["tables"]:
                paths = [str(final / item["relative_path"]) for item in table["files"]]
                literals = ", ".join(_literal(path) for path in paths)
                self._connection.execute(
                    f"CREATE TEMP VIEW {table['name']} AS "
                    f"SELECT * FROM read_parquet([{literals}])"
                )
        except BaseException:
            self._connection.close()
            self._connection = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def iter_subtrack_batches(
        self, vehicle_id: str, subtrack_index: int, *, batch_size: int = 4096,
    ) -> Iterator[tuple[tuple[Any, ...], ...]]:
        """Yield bounded row batches ordered across kept and injected points."""
        if self._connection is None:
            raise RefineParquetError("dataset reader is not open")
        if not isinstance(vehicle_id, str) or not vehicle_id or isinstance(subtrack_index, bool):
            raise RefineParquetError("vehicle_id and subtrack_index are required")
        if (not isinstance(subtrack_index, int) or isinstance(batch_size, bool)
                or not isinstance(batch_size, int) or batch_size < 1):
            raise RefineParquetError("subtrack_index and batch_size are invalid")
        query = """
          SELECT refine_id, point_id, vehicle_id, subtrack_index,
                 refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                 heading, speed, point_kind, source_row_key, generated_reason,
                 after_sequence_in_subtrack
          FROM (
            SELECT *, 'kept'::VARCHAR AS point_kind,
                   NULL::VARCHAR AS generated_reason,
                   NULL::INTEGER AS after_sequence_in_subtrack
            FROM kept_points
            UNION ALL BY NAME
            SELECT *, 'injected'::VARCHAR AS point_kind,
                   NULL::VARCHAR AS source_row_key
            FROM injected_points
          ) points
          WHERE vehicle_id = ? AND subtrack_index = ?
          ORDER BY refined_sequence_in_subtrack, point_kind, point_id
        """
        result = self._connection.execute(query, [vehicle_id, subtrack_index])
        while rows := result.fetchmany(batch_size):
            yield tuple(rows)

    def extent(self) -> tuple[float, float, float, float] | None:
        """Return processing-CRS bounds without materializing dataset rows."""
        connection = self._require_open()
        row = connection.execute("""
          SELECT min(x), min(y), max(x), max(y) FROM (
            SELECT x, y FROM kept_points UNION ALL
            SELECT x, y FROM injected_points
          ) points
        """).fetchone()
        if row is None or row[0] is None:
            return None
        extent = tuple(float(value) for value in row)
        if not all(math.isfinite(value) for value in extent):
            raise RefineParquetError("dataset extent contains non-finite coordinates")
        return extent  # type: ignore[return-value]

    def iter_tracks(
        self, source_table: str, *, batch_size: int = 4096,
    ) -> Iterator[list[TrajectoryPoint]]:
        """Yield validated Detect input, retaining at most one track and one batch."""
        connection = self._require_open()
        if not isinstance(source_table, str) or not source_table.strip():
            raise RefineParquetError("source_table is required")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise RefineParquetError("batch_size is invalid")
        result = connection.execute("""
          SELECT refine_id, point_id, vehicle_id, subtrack_index,
                 refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                 heading, speed, point_kind, source_row_key, generated_reason,
                 after_sequence_in_subtrack,
                 count(DISTINCT vehicle_id) OVER (PARTITION BY subtrack_index)
          FROM (
            SELECT *, 'kept'::VARCHAR AS point_kind,
                   NULL::VARCHAR AS generated_reason,
                   NULL::INTEGER AS after_sequence_in_subtrack
            FROM kept_points
            UNION ALL BY NAME
            SELECT *, 'injected'::VARCHAR AS point_kind,
                   NULL::VARCHAR AS source_row_key
            FROM injected_points
          ) points
          ORDER BY vehicle_id, subtrack_index, refined_sequence_in_subtrack,
                   point_kind, point_id
        """)
        current_key: tuple[str, int] | None = None
        track: list[TrajectoryPoint] = []
        while rows := result.fetchmany(batch_size):
            for row in rows:
                point, key = self._decode_row(row, source_table, len(track), current_key)
                if current_key is not None and key != current_key:
                    yield track
                    track = []
                    point, key = self._decode_row(row, source_table, 0, None)
                current_key = key
                track.append(point)
        if track:
            yield track

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RefineParquetError("dataset reader is not open")
        return self._connection

    def _decode_row(
        self, row: tuple[Any, ...], source_table: str, expected_sequence: int,
        current_key: tuple[str, int] | None,
    ) -> tuple[TrajectoryPoint, tuple[str, int]]:
        if len(row) != 17 or row[0] != self._descriptor.refine_id:
            raise RefineParquetError("row owner differs from descriptor owner")
        kind, point_id, vehicle_id = row[12], row[1], row[2]
        if kind not in ("kept", "injected") or not isinstance(vehicle_id, str) or not vehicle_id:
            raise RefineParquetError("row identity is invalid")
        if isinstance(point_id, bool) or not isinstance(point_id, int):
            raise RefineParquetError("point identity is invalid")
        subtrack_index, sequence = row[3], row[4]
        if (isinstance(subtrack_index, bool) or not isinstance(subtrack_index, int)
                or subtrack_index < 0 or isinstance(sequence, bool)
                or not isinstance(sequence, int) or sequence < 0):
            raise RefineParquetError("subtrack identity is invalid")
        if row[16] != 1:
            raise RefineParquetError("subtrack contains multiple vehicles")
        key = (vehicle_id, subtrack_index)
        if current_key == key and sequence != expected_sequence:
            raise RefineParquetError("subtrack sequence is not contiguous from zero")
        if current_key != key and sequence != 0:
            raise RefineParquetError("subtrack sequence does not start at zero")
        try:
            values = tuple(float(row[index]) for index in range(5, 12))
        except (TypeError, ValueError) as exc:
            raise RefineParquetError("point numeric fields are invalid") from exc
        timestamp, lon, lat, x, y, heading, speed = values
        if not all(math.isfinite(value) for value in values):
            raise RefineParquetError("point contains non-finite numeric fields")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise RefineParquetError("point longitude/latitude is outside EPSG:4326")
        source_row_key, reason, after_sequence = row[13], row[14], row[15]
        if kind == "kept":
            if not isinstance(source_row_key, str) or not source_row_key or reason is not None or after_sequence is not None:
                raise RefineParquetError("kept point identity fields are invalid")
            point_ref = PointRef(source_table, source_row_key, True)
        else:
            if source_row_key is not None or not isinstance(reason, str) or not reason or isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
                raise RefineParquetError("injected point identity fields are invalid")
            point_ref = PointRef("public.injected_points", str(point_id), False,
                                 generated_reason=reason)
        return TrajectoryPoint(
            point_ref=point_ref, vehicle_id=vehicle_id, timestamp=timestamp,
            lon=lon, lat=lat, heading=heading, speed=speed, extra={}, x=x, y=y,
            refined_point_ref=RefinedPointRef(kind, point_id, subtrack_index, sequence),
        ), key


@dataclass(frozen=True, slots=True)
class ParquetColumn:
    name: str
    duckdb_type: str
    # DuckDB 1.5.5 writes flat COPY output as physically OPTIONAL Parquet fields.
    nullable: bool = True
    required: bool = True


_COMMON = (
    ParquetColumn("refine_id", "UUID"), ParquetColumn("point_id", "BIGINT"),
    ParquetColumn("vehicle_id", "VARCHAR"), ParquetColumn("subtrack_index", "INTEGER"),
    ParquetColumn("refined_sequence_in_subtrack", "BIGINT"),
    ParquetColumn("timestamp", "DOUBLE"), ParquetColumn("lon", "DOUBLE"),
    ParquetColumn("lat", "DOUBLE"), ParquetColumn("x", "DOUBLE"),
    ParquetColumn("y", "DOUBLE"), ParquetColumn("heading", "DOUBLE"),
    ParquetColumn("speed", "DOUBLE"),
)
REFINE_PARQUET_V1 = {
    "kept_points": (*_COMMON, ParquetColumn("source_row_key", "VARCHAR")),
    "injected_points": (
        *_COMMON, ParquetColumn("generated_reason", "VARCHAR"),
        ParquetColumn("after_sequence_in_subtrack", "INTEGER"),
    ),
}


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise RefineParquetError("current PostgreSQL schema is unavailable")
    return '"' + value.replace('"', '""') + '"'


def _run_metadata(conn: Any, refine_id: UUID) -> tuple[str, int, str]:
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
    return str(row[0]), spatial_context.processing_srid, result_schema


def _postgres_query(
    refine_id: UUID, contract: SourceTableContract, srid: int, result_schema: str,
) -> str:
    run = _literal(str(refine_id))
    return f"""
WITH points AS (
 SELECT k.refine_id, k.id AS point_id, k.vehicle_id, k.subtrack_index,
        k.sequence_in_subtrack AS anchor_sequence, 0 AS point_kind,
        s.timestamp, s.lon, s.lat,
        ST_X(ST_Transform(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), {srid})) AS x,
        ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), {srid})) AS y,
        s.heading, s.speed, k.source_row_key, NULL::text AS generated_reason,
        NULL::integer AS after_sequence_in_subtrack
 FROM {result_schema}.kept_trajectory_points k JOIN {contract.quoted_table} s
   ON s.id = k.source_row_key::{contract.key_sql_type}
 WHERE k.refine_id = {run}::uuid
 UNION ALL
 SELECT i.refine_id, i.id, i.vehicle_id, i.subtrack_index,
        i.after_sequence_in_subtrack, 1, i.timestamp, i.lon, i.lat, i.x, i.y,
        i.heading, i.speed, NULL::text, i.generated_reason,
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
    columns = REFINE_PARQUET_V1[table]
    projection = ", ".join(f"{column.name}::{column.duckdb_type} AS {column.name}" for column in columns)
    point_kind = 0 if table == "kept_points" else 1
    return (
        f"SELECT {projection} FROM postgres_query('authoritative_pg', {_literal(postgres_sql)}) "
        f"WHERE point_kind = {point_kind} "
        "ORDER BY subtrack_index, refined_sequence_in_subtrack, point_id"
    )


def _open_duckdb(runtime: RuntimeConfig, temp_directory: Path) -> duckdb.DuckDBPyConnection:
    extension = Path(os.environ.get("DUCKDB_POSTGRES_EXTENSION_PATH", ""))
    if not extension.is_absolute() or not extension.is_file():
        raise RefineParquetError("DUCKDB_POSTGRES_EXTENSION_PATH must name a bundled absolute file")
    temp_directory.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=":memory:", config={
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "memory_limit": runtime.duckdb_memory_limit, "threads": str(runtime.duckdb_threads),
        "temp_directory": str(temp_directory),
        "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
    })
    connection.execute(f"LOAD {_literal(str(extension))}")
    return connection


def _copy_table(
    connection: duckdb.DuckDBPyConnection, relation: str, output: Path,
    table: str, runtime: RuntimeConfig,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=False)
    options = ["FORMAT PARQUET"]
    if runtime.parquet_row_group_size_rows:
        options.append(f"ROW_GROUP_SIZE {runtime.parquet_row_group_size_rows}")
    connection.execute(f"COPY ({relation}) TO {_literal(str(output))} ({', '.join(options)})")
    null_predicate = " OR ".join(f"{column.name} IS NULL" for column in REFINE_PARQUET_V1[table])
    if connection.execute(
        f"SELECT count(*) FROM read_parquet({_literal(str(output))}) WHERE {null_predicate}"
    ).fetchone()[0]:
        raise RefineParquetError(f"{table} contains null output fields")


def _validate_schema(descriptor: ArtifactDescriptor) -> None:
    tables = {table["name"]: table for table in json.loads(descriptor.manifest_json)["tables"]}
    if set(tables) != set(REFINE_PARQUET_V1):
        raise RefineParquetError("Refine descriptor has unexpected logical tables")
    for name, columns in REFINE_PARQUET_V1.items():
        expected = [
            {"name": column.name, "type": column.duckdb_type, "nullable": column.nullable}
            for column in columns
        ]
        if tables[name]["schema"] != expected:
            raise RefineParquetError(f"{name} Parquet schema differs from v1")


def export_refine_parquet(
    conn: Any, postgres: PostgresConfig, handle: StagingDataset,
    runtime: RuntimeConfig, source_fingerprint: dict[str, object],
) -> ArtifactDescriptor:
    """Write and inspect staging only; never publish, commit, rollback, or finish.

    The Refine rows must already be committed and visible while the Run remains
    ``running``: postgres_scanner uses its own read-only PostgreSQL connection.
    C05b owns that pre-export commit and the later publication transaction.
    """
    _validate_handle(handle)
    if handle.stage != "refine":
        raise RefineParquetError("staging dataset must belong to the refine stage")
    source_table, srid, result_schema = _run_metadata(conn, handle.run_id)
    contract = validate_source_table(conn, source_table)
    postgres_sql = _postgres_query(handle.run_id, contract, srid, result_schema)
    data_root = Path(os.environ.get("DATA_ROOT", "/data"))
    outputs = {
        name: f"{name}/part-00000.parquet" for name in REFINE_PARQUET_V1
    }
    # C05a deliberately writes one file per logical table. Target file sizing
    # remains zero/engine-default until the benchmark introduces partitioning.
    with _open_duckdb(runtime, data_root / "duckdb" / str(handle.run_id)) as connection:
        connection.execute(
            f"ATTACH {_literal(postgres.dsn())} AS authoritative_pg "
            "(TYPE postgres, READ_ONLY)"
        )
        for name, relative in outputs.items():
            _copy_table(
                connection, _relation(postgres_sql, name),
                handle.path / relative, name, runtime,
            )
    descriptor = build_descriptor(
        handle, {name: [relative] for name, relative in outputs.items()}, source_fingerprint,
    )
    _validate_schema(descriptor)
    return descriptor
