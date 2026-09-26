from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import duckdb
import pytest

from track.v2.artifacts import publish_dataset as publish_dataset
from track.v2.intersections.results import export_to_parquet as detect_parquet_export
from track.v2.models import RuntimeConfig


def _handle(tmp_path: Path):
    return publish_dataset.create_staging(
        tmp_path.resolve(), "detect", uuid4(), uuid4(),
    )


def _select(table: str, *, empty: bool = False) -> str:
    values = {
        "resolved_points": [
            "?::UUID", "10::BIGINT", "1::INTEGER", "0::BIGINT", "0::BIGINT",
            "'vehicle-a'::VARCHAR", "120.0::DOUBLE", "30.0::DOUBLE", "'kept'::VARCHAR",
            "20::BIGINT", "'source-20'::VARCHAR", "NULL::BIGINT", "NULL::BIGINT",
            "NULL::SMALLINT",
        ],
        "intersections": [
            "?::UUID", "30::BIGINT", "120.0::DOUBLE", "30.0::DOUBLE", "12.0::DOUBLE",
        ],
        "entry_exit_points": [
            "?::UUID", "40::BIGINT", "30::BIGINT", "11::BIGINT", "10::BIGINT",
            "1::SMALLINT", "1.0::DOUBLE", "2.0::DOUBLE", "120.0::DOUBLE", "30.0::DOUBLE",
        ],
    }[table]
    columns = [column.name for column in detect_parquet_export.DETECT_PARQUET_V1[table]]
    query = "SELECT " + ", ".join(
        f"{value} AS {name}" for value, name in zip(values, columns, strict=True)
    )
    return query + (" WHERE false" if empty else "")


def test_v1_schema_is_flat_explicit_and_clip_scoped():
    assert list(detect_parquet_export.DETECT_PARQUET_V1) == [
        "resolved_points", "intersections", "entry_exit_points",
    ]
    assert [column.name for column in detect_parquet_export.DETECT_PARQUET_V1["resolved_points"]] == [
        "detect_id", "resolved_point_id", "subtrack_index", "sequence_in_subtrack",
        "refined_sequence_in_subtrack", "vehicle_id", "lon", "lat", "point_kind",
        "refined_point_id", "source_row_key", "crossing_id", "intersection_id",
        "crossing_type",
    ]
    assert "candidate_intersections" not in detect_parquet_export.DETECT_PARQUET_V1
    assert "turn_events" not in detect_parquet_export.DETECT_PARQUET_V1


def test_queries_are_owner_scoped_stable_and_preserve_clip_relations():
    run_id = uuid4()
    queries = detect_parquet_export._queries(run_id, '"run"', '"source"', "bigint")
    assert all(f"'{run_id}'::uuid" in query for query in queries.values())
    assert "COALESCE(k.vehicle_id, ip.vehicle_id, sk.vehicle_id, sip.vehicle_id)" in queries[
        "resolved_points"
    ]
    assert "k.source_row_key::text AS source_row_key" in queries["resolved_points"]
    assert "s.id=k.source_row_key" in queries["resolved_points"]
    assert "k.source_row_key::bigint" not in queries["resolved_points"]
    assert "r.crossing_point_id=c.id" in queries["entry_exit_points"]
    assert detect_parquet_export._relation(queries["resolved_points"], "resolved_points").endswith(
        "ORDER BY subtrack_index, sequence_in_subtrack, resolved_point_id"
    )
    assert "postgres_query('authoritative_pg'" in detect_parquet_export._relation(
        queries["intersections"], "intersections",
    )


def test_copy_writes_every_table_even_when_empty_and_builds_v1_manifest(tmp_path):
    handle = _handle(tmp_path)
    outputs = {}
    with duckdb.connect() as connection:
        for name in detect_parquet_export.DETECT_PARQUET_V1:
            output = handle.path / name / "part-00000.parquet"
            relation = _select(name, empty=True).replace("?", f"'{handle.run_id}'")
            detect_parquet_export._copy(connection, relation, output, name, runtime_config())
            outputs[name] = output
        detect_parquet_export._validate_data(connection, outputs)
    descriptor = detect_parquet_export.build_descriptor(
        handle,
        {name: [f"{name}/part-00000.parquet"] for name in outputs},
        {"version": 1, "sha256": "a" * 64},
    )
    detect_parquet_export._validate_schema(descriptor)
    manifest = json.loads(descriptor.manifest_json)
    assert {table["name"]: table["row_count"] for table in manifest["tables"]} == {
        "entry_exit_points": 0, "intersections": 0, "resolved_points": 0,
    }


def test_validation_rejects_noncontiguous_sequence(tmp_path):
    handle = _handle(tmp_path)
    outputs = {}
    with duckdb.connect() as connection:
        for name in detect_parquet_export.DETECT_PARQUET_V1:
            output = handle.path / name / "part-00000.parquet"
            relation = _select(name, empty=name != "resolved_points").replace(
                "?", f"'{handle.run_id}'",
            )
            if name == "resolved_points":
                relation = relation.replace("0::BIGINT AS sequence_in_subtrack", "1::BIGINT AS sequence_in_subtrack")
            detect_parquet_export._copy(connection, relation, output, name, runtime_config())
            outputs[name] = output
        with pytest.raises(detect_parquet_export.DetectParquetError, match="Clip input semantics"):
            detect_parquet_export._validate_data(connection, outputs)


def test_unknown_manifest_version_is_rejected():
    descriptor = Mock(manifest_json=json.dumps({"version": 2, "tables": []}))
    with pytest.raises(detect_parquet_export.DetectParquetError, match="unknown"):
        detect_parquet_export._validate_schema(descriptor)


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_metadata_requires_running_without_transaction_control(status):
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=None)
    cursor.fetchone.return_value = ("public.source", "public", status)
    conn = Mock()
    conn.cursor.return_value = cursor
    with pytest.raises(detect_parquet_export.DetectParquetError, match="remain running"):
        detect_parquet_export._metadata(conn, uuid4())
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


def test_q44_export_is_pure_duckdb_sql_without_python_rows():
    """Q44: relational export/validation needs neither Polars nor Python row objects."""
    source = Path(detect_parquet_export.__file__).read_text(encoding="utf-8")
    assert "fetchall(" not in source
    assert "polars" not in source.lower()
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "publish(" not in source
