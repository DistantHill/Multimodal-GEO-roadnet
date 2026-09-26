from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, pipeline_config, postgres_config, projection_config, refine_config, runtime_config)

import json
import os
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import duckdb
import pytest

from track.v2.artifacts import publish_dataset as publish_dataset
from track.v2.trajectories.results import export_to_parquet as refine_export
from track.v2.trajectories.results import parquet_schema as refine_schema
from track.v2.config import PostgresConfig
from track.v2.db.source_table import SourceTableContract
from track.v2.models import RuntimeConfig


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, parameters):
        self.executed.append((sql, parameters))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.cursor_instance = _Cursor(row)
        self.commit = Mock()
        self.rollback = Mock()

    def cursor(self):
        return self.cursor_instance


def _handle(tmp_path: Path):
    return publish_dataset.create_staging(
        tmp_path.resolve(), "refine", uuid4(), uuid4(),
    )


def test_v1_schema_has_fixed_order_types_and_non_null_contract():
    assert list(refine_schema.REFINE_PARQUET_V1) == ["kept_points", "injected_points"]
    assert [column.name for column in refine_schema.REFINE_PARQUET_V1["kept_points"]] == [
        "refine_id", "point_id", "vehicle_id", "subtrack_index",
        "refined_sequence_in_subtrack", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed", "source_row_key",
    ]
    assert [column.name for column in refine_schema.REFINE_PARQUET_V1["injected_points"]][-2:] == [
        "generated_reason", "after_sequence_in_subtrack",
    ]
    assert all(
        column.required and column.nullable
        for columns in refine_schema.REFINE_PARQUET_V1.values()
        for column in columns
    )


def test_postgres_query_is_run_scoped_safe_and_computes_interleaved_sequence():
    run_id = uuid4()
    contract = SourceTableContract(
        source_table="odd.schema", quoted_table='"odd"."schema"',
        key_sql_type="bigint", has_extra=True,
    )
    sql = refine_export._postgres_query(run_id, contract, 32650, '"run""schema"')

    assert sql.count(f"'{run_id}'::uuid") == 2
    assert 'JOIN "odd"."schema" s' in sql
    assert 'FROM "run""schema".kept_trajectory_points k' in sql
    assert 'ON s.id = k.source_row_key' in sql
    assert 'k.source_row_key::text AS source_row_key' in sql
    assert 'k.source_row_key::bigint' not in sql
    assert 'FROM "run""schema".injected_points i' in sql
    projected = "ST_Transform(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326), 32650)"
    assert sql.count(projected) == 2
    assert "ST_Transform(s.geom" not in sql
    assert "PARTITION BY subtrack_index" in sql
    assert "ORDER BY anchor_sequence, point_kind, point_id" in sql
    assert "row_number()" in sql
    assert "extra" not in sql

    with duckdb.connect() as connection:
        rows = connection.execute("""
            WITH points(anchor_sequence, point_kind, point_id) AS (
              VALUES (0, 0, 20), (1, 0, 10), (1, 1, 99), (2, 0, 30)
            )
            SELECT point_id, row_number() OVER (
              ORDER BY anchor_sequence, point_kind, point_id
            ) - 1 AS refined_sequence
            FROM points ORDER BY refined_sequence
        """).fetchall()
    assert rows == [(20, 0), (10, 1), (99, 2), (30, 3)]


def test_relation_uses_attached_postgres_without_install():
    relation = refine_export._relation("SELECT 'quoted'", "kept_points")

    assert "postgres_query(" in relation
    assert "INSTALL" not in relation.upper()
    assert "authoritative_pg" in relation
    assert "SELECT ''quoted''" in relation
    assert relation.endswith(
        "ORDER BY subtrack_index, refined_sequence_in_subtrack, point_id"
    )


def test_copy_writes_real_typed_files_including_empty_injected(tmp_path):
    handle = _handle(tmp_path)
    run_id = handle.run_id
    common = (
        f"'{run_id}'::UUID AS refine_id, 7::BIGINT AS point_id, 'v'::VARCHAR AS vehicle_id, "
        "1::INTEGER AS subtrack_index, 0::BIGINT AS refined_sequence_in_subtrack, "
        "1.5::DOUBLE AS timestamp, 120.0::DOUBLE AS lon, 30.0::DOUBLE AS lat, "
        "10.0::DOUBLE AS x, 20.0::DOUBLE AS y, 90.0::DOUBLE AS heading, 4.0::DOUBLE AS speed"
    )
    with duckdb.connect() as connection:
        refine_export._copy_table(
            connection, f"SELECT {common}, 'source-7'::VARCHAR AS source_row_key",
            handle.path / "kept_points/part-00000.parquet", "kept_points", runtime_config(),
        )
        refine_export._copy_table(
            connection,
            f"SELECT {common}, 'orthogonal'::VARCHAR AS generated_reason, "
            "0::INTEGER AS after_sequence_in_subtrack WHERE false",
            handle.path / "injected_points/part-00000.parquet", "injected_points", runtime_config(),
        )

    descriptor = refine_export.build_descriptor(
        handle,
        {
            "kept_points": ["kept_points/part-00000.parquet"],
            "injected_points": ["injected_points/part-00000.parquet"],
        },
        {"version": 1, "sha256": "a" * 64},
    )
    manifest = json.loads(descriptor.manifest_json)
    tables = {table["name"]: table for table in manifest["tables"]}
    assert tables["kept_points"]["row_count"] == 1
    assert tables["injected_points"]["row_count"] == 0
    assert [field["name"] for field in tables["kept_points"]["schema"]] == [
        column.name for column in refine_schema.REFINE_PARQUET_V1["kept_points"]
    ]
    refine_schema._validate_schema(descriptor)


@pytest.mark.parametrize(
    "row, message",
    [(None, "unknown"),
     (("public.source", 4326, 32650, {"coordinate_semantics_version": 2}, "completed", "public"),
      "running"),
     (("public.source", 4326, None, {"coordinate_semantics_version": 2}, "running", "public"),
      "processing_srid"),
     (("public.source", 4326, 32650, {"coordinate_semantics_version": 2}, "running", None),
      "schema")],
)
def test_export_rejects_invalid_run_before_opening_duckdb(tmp_path, row, message, monkeypatch):
    monkeypatch.setattr(refine_export, "validate_source_table", Mock())
    opened = Mock()
    monkeypatch.setattr(refine_export, "_open_duckdb", opened)
    conn = _Conn(row)

    with pytest.raises(refine_export.RefineParquetError, match=message):
        refine_export.export_refine_parquet(
            conn, postgres_config(), _handle(tmp_path), runtime_config(), {"version": 1},
            data_root=tmp_path,
            postgres_extension_path=pipeline_config().paths.duckdb_postgres_extension_path,
        )

    opened.assert_not_called()
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


def test_open_duckdb_requires_absolute_bundled_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_POSTGRES_EXTENSION_PATH", "relative.extension")
    with pytest.raises(refine_export.RefineParquetError, match="absolute"):
        refine_export._open_duckdb(runtime_config(), tmp_path, Path("relative.extension"))


def test_open_duckdb_loads_bundled_extension_with_network_install_disabled(tmp_path):
    extension = os.environ.get("DUCKDB_POSTGRES_EXTENSION_PATH")
    if not extension:
        pytest.skip("test requires the Pipeline image's bundled extension")
    with refine_export._open_duckdb(
        runtime_config(duckdb_memory_limit="128MB", duckdb_threads=1), tmp_path,
        Path(extension),
    ) as connection:
        loaded = connection.execute(
            "SELECT loaded FROM duckdb_extensions() "
            "WHERE extension_name = 'postgres_scanner'"
        ).fetchone()[0]
        settings = dict(connection.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN "
            "('autoinstall_known_extensions', 'autoload_known_extensions', 'threads')"
        ).fetchall())
    assert loaded
    assert settings == {
        "autoload_known_extensions": "false",
        "autoinstall_known_extensions": "false",
        "threads": "1",
    }
