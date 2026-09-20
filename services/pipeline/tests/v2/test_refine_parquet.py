from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import duckdb
import pytest

from track.v2 import artifact_publication, refine_parquet
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
    return artifact_publication.create_staging(
        tmp_path.resolve(), "refine", uuid4(), uuid4(),
    )


def test_v1_schema_has_fixed_order_types_and_non_null_contract():
    assert list(refine_parquet.REFINE_PARQUET_V1) == ["kept_points", "injected_points"]
    assert [column.name for column in refine_parquet.REFINE_PARQUET_V1["kept_points"]] == [
        "refine_id", "point_id", "vehicle_id", "subtrack_index",
        "refined_sequence_in_subtrack", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed", "source_row_key",
    ]
    assert [column.name for column in refine_parquet.REFINE_PARQUET_V1["injected_points"]][-2:] == [
        "generated_reason", "after_sequence_in_subtrack",
    ]
    assert all(
        column.required and column.nullable
        for columns in refine_parquet.REFINE_PARQUET_V1.values()
        for column in columns
    )


def test_postgres_query_is_run_scoped_safe_and_computes_interleaved_sequence():
    run_id = uuid4()
    contract = SourceTableContract(
        source_table="odd.schema", quoted_table='"odd"."schema"',
        key_sql_type="bigint", has_extra=True,
    )
    sql = refine_parquet._postgres_query(run_id, contract, 32650)

    assert sql.count(f"'{run_id}'::uuid") == 2
    assert 'JOIN "odd"."schema" s' in sql
    assert "ST_Transform(s.geom, 32650)" in sql
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


def test_relation_escapes_credentials_and_contains_no_install():
    postgres = PostgresConfig(
        host="db host", database="road/net", user="o'reilly", password="p'a ss",
    )
    relation = refine_parquet._relation(postgres.dsn(), "SELECT 'quoted'", "kept_points")

    assert "postgres_query(" in relation
    assert "INSTALL" not in relation.upper()
    assert "o%27reilly" in relation
    assert "p%27a+ss" in relation
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
        refine_parquet._copy_table(
            connection, f"SELECT {common}, 'source-7'::VARCHAR AS source_row_key",
            handle.path / "kept_points/part-00000.parquet", "kept_points", RuntimeConfig(),
        )
        refine_parquet._copy_table(
            connection,
            f"SELECT {common}, 'orthogonal'::VARCHAR AS generated_reason, "
            "0::INTEGER AS after_sequence_in_subtrack WHERE false",
            handle.path / "injected_points/part-00000.parquet", "injected_points", RuntimeConfig(),
        )

    descriptor = refine_parquet.build_descriptor(
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
        column.name for column in refine_parquet.REFINE_PARQUET_V1["kept_points"]
    ]
    refine_parquet._validate_schema(descriptor)


@pytest.mark.parametrize(
    "row, message",
    [(None, "unknown"),
     (("public.source", 4326, 32650, {"coordinate_semantics_version": 2}, "completed"),
      "running"),
     (("public.source", 4326, None, {"coordinate_semantics_version": 2}, "running"),
      "processing_srid")],
)
def test_export_rejects_invalid_run_before_opening_duckdb(tmp_path, row, message, monkeypatch):
    monkeypatch.setattr(refine_parquet, "validate_source_table", Mock())
    opened = Mock()
    monkeypatch.setattr(refine_parquet, "_open_duckdb", opened)
    conn = _Conn(row)

    with pytest.raises(refine_parquet.RefineParquetError, match=message):
        refine_parquet.export_refine_parquet(
            conn, PostgresConfig(), _handle(tmp_path), RuntimeConfig(), {"version": 1},
        )

    opened.assert_not_called()
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


def test_open_duckdb_requires_absolute_bundled_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_POSTGRES_EXTENSION_PATH", "relative.extension")
    with pytest.raises(refine_parquet.RefineParquetError, match="absolute"):
        refine_parquet._open_duckdb(RuntimeConfig(), tmp_path)


def test_open_duckdb_loads_bundled_extension_with_network_install_disabled(tmp_path):
    extension = os.environ.get("DUCKDB_POSTGRES_EXTENSION_PATH")
    if not extension:
        pytest.skip("test requires the Pipeline image's bundled extension")
    with refine_parquet._open_duckdb(
        RuntimeConfig(duckdb_memory_limit="128MB", duckdb_threads=1), tmp_path,
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
