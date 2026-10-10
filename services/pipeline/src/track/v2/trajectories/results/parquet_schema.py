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


REFINE_PARQUET_V2 = {
    name: (*columns, ParquetColumn("heading_cal", "DOUBLE", required=False))
    for name, columns in REFINE_PARQUET_V1.items()
}
SHORT_SUBTRACK_COLUMNS = (
    ParquetColumn("refine_id", "VARCHAR"), ParquetColumn("source_id", "VARCHAR"),
    ParquetColumn("vehicle_id", "VARCHAR"), ParquetColumn("subtrack_index", "BIGINT"),
    ParquetColumn("source_partition_index", "BIGINT"),
    ParquetColumn("sequence_in_subtrack", "BIGINT"), ParquetColumn("source_row_key", "BIGINT"),
    ParquetColumn("timestamp", "DOUBLE"), ParquetColumn("lon", "DOUBLE"),
    ParquetColumn("lat", "DOUBLE"), ParquetColumn("x", "DOUBLE"), ParquetColumn("y", "DOUBLE"),
    ParquetColumn("heading", "DOUBLE"), ParquetColumn("speed", "DOUBLE"),
    ParquetColumn("geometry", "BLOB"),
)


def _validate_schema(descriptor: ArtifactDescriptor) -> int:
    """Accept only explicitly registered v1/v2 layouts; v1 has no headings."""
    tables = {table["name"]: table for table in json.loads(descriptor.manifest_json)["tables"]}
    schemas = ((1, REFINE_PARQUET_V1),
               (2, {**REFINE_PARQUET_V2, "short_subtrack": SHORT_SUBTRACK_COLUMNS}))
    for version, schema in schemas:
        if set(tables) != set(schema):
            continue
        if all(tables[name]["schema"] == [
            {"name": column.name, "type": column.duckdb_type, "nullable": column.nullable}
            for column in columns
        ] for name, columns in schema.items()):
            return version
    raise RefineParquetError("Refine descriptor has unexpected logical tables or Parquet schema differs from v1/v2")
