"""Column definitions and validation for Refine Parquet v1."""

from __future__ import annotations

import json

from track.v2.artifacts.parquet_schema import ParquetColumn
from track.v2.db.artifact_registry import ArtifactDescriptor

from . import RefineParquetError


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
    "kept_points": (*_COMMON, ParquetColumn("source_row_key", "BIGINT")),
    "injected_points": (
        *_COMMON, ParquetColumn("generated_reason", "VARCHAR"),
        ParquetColumn("after_sequence_in_subtrack", "INTEGER"),
    ),
}


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
