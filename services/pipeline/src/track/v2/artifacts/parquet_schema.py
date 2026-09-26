"""Shared column descriptor for immutable Parquet artifact schemas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParquetColumn:
    name: str
    duckdb_type: str
    # DuckDB 1.5.5 writes flat COPY output as physically OPTIONAL Parquet fields.
    nullable: bool = True
    required: bool = True
