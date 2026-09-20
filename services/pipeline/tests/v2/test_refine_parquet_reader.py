from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import duckdb
import pytest

from track.v2 import artifact_publication, refine_parquet
from track.v2.artifact_parquet import build_descriptor
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json
from track.v2.models import RuntimeConfig


def _published(tmp_path: Path):
    handle = artifact_publication.create_staging(
        tmp_path.resolve(), "refine", uuid4(), uuid4(),
    )
    common = (
        f"'{handle.run_id}'::UUID AS refine_id, {{point}}::BIGINT AS point_id, "
        "'{vehicle}'::VARCHAR AS vehicle_id, 4::INTEGER AS subtrack_index, "
        "{sequence}::BIGINT AS refined_sequence_in_subtrack, "
        "1.0::DOUBLE AS \"timestamp\", 120.0::DOUBLE AS lon, 30.0::DOUBLE AS lat, "
        "10.0::DOUBLE AS x, 20.0::DOUBLE AS y, 90.0::DOUBLE AS heading, "
        "4.0::DOUBLE AS speed"
    )
    with duckdb.connect() as connection:
        for table in refine_parquet.REFINE_PARQUET_V1:
            directory = handle.path / table
            directory.mkdir()
            output = directory / "part.parquet"
            if table == "kept_points":
                rows = [common.format(point=1, vehicle="v1", sequence=0),
                        common.format(point=3, vehicle="v1", sequence=2),
                        common.format(point=8, vehicle="v2", sequence=0)]
                sql = " UNION ALL ".join(
                    f"SELECT {row}, ('source-' || point_id)::VARCHAR source_row_key"
                    for row in rows
                )
            else:
                sql = (
                    f"SELECT {common.format(point=2, vehicle='v1', sequence=1)}, "
                    "'orthogonal'::VARCHAR generated_reason, "
                    "0::INTEGER after_sequence_in_subtrack"
                )
            connection.execute(f"COPY ({sql}) TO ? (FORMAT PARQUET)", [str(output)])
    descriptor = build_descriptor(
        handle,
        {name: [f"{name}/part.parquet"] for name in refine_parquet.REFINE_PARQUET_V1},
        {"version": 1},
    )
    artifact_publication.publish(handle)
    return descriptor


def test_reader_uses_only_manifest_files_and_merges_one_vehicle_subtrack(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        batches = list(reader.iter_subtrack_batches("v1", 4, batch_size=2))
        assert [len(batch) for batch in batches] == [2, 1]
        rows = [row for batch in batches for row in batch]
        assert [(row[1], row[12], row[2]) for row in rows] == [
            (1, "kept", "v1"), (2, "injected", "v1"), (3, "kept", "v1"),
        ]
        assert [row[4] for row in rows] == [0, 1, 2]

    extra = tmp_path / descriptor.relative_path / "kept_points" / "undeclared.parquet"
    extra.parent.chmod(0o755)
    extra.write_bytes(b"not consumed")
    extra.chmod(0o444)
    extra.parent.chmod(0o555)
    with pytest.raises(Exception, match="manifest"):
        refine_parquet.RefineDatasetReader(tmp_path, descriptor, "present", RuntimeConfig())


def test_reader_rejects_non_present_unknown_tables_and_schema(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with pytest.raises(Exception, match="not present"):
        refine_parquet.RefineDatasetReader(tmp_path, descriptor, "deleted", RuntimeConfig())
    manifest = json.loads(descriptor.manifest_json)
    manifest["tables"][0]["name"] = "unknown_points"
    changed = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=descriptor.refine_id,
        relative_path=descriptor.relative_path,
        source_fingerprint=json.loads(descriptor.source_fingerprint_json), manifest=manifest,
    )
    with pytest.raises(refine_parquet.RefineParquetError, match="logical tables"):
        refine_parquet.RefineDatasetReader(tmp_path, changed, "present", RuntimeConfig())
    manifest = json.loads(descriptor.manifest_json)
    manifest["tables"][0]["schema"][0]["type"] = "VARCHAR"
    manifest["tables"][0]["schema_fingerprint"] = hashlib.sha256(
        canonical_json(manifest["tables"][0]["schema"]).encode()
    ).hexdigest()
    schema_changed = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=descriptor.refine_id,
        relative_path=descriptor.relative_path,
        source_fingerprint=json.loads(descriptor.source_fingerprint_json), manifest=manifest,
    )
    with pytest.raises(Exception, match="schema"):
        refine_parquet.RefineDatasetReader(
            tmp_path, schema_changed, "present", RuntimeConfig(),
        )


def test_batch_api_uses_bounded_fetchmany_not_fetchall() -> None:
    reader = object.__new__(refine_parquet.RefineDatasetReader)
    result = Mock()
    result.fetchmany.side_effect = [[("first",)], [("second",)], []]
    connection = Mock()
    connection.execute.return_value = result
    reader._connection = connection

    assert list(reader.iter_subtrack_batches("vehicle", 4, batch_size=17)) == [
        (("first",),), (("second",),),
    ]
    assert connection.execute.call_args.args[1] == ["vehicle", 4]
    assert result.fetchmany.call_count == 3
    result.fetchmany.assert_called_with(17)
    assert not result.fetchall.called


def test_reader_closes_connection_when_view_creation_fails(tmp_path, monkeypatch) -> None:
    descriptor = _published(tmp_path)
    reader = refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(),
    )
    connection = Mock()
    connection.execute.side_effect = duckdb.IOException("view probe")
    monkeypatch.setattr(refine_parquet.duckdb, "connect", Mock(return_value=connection))

    with pytest.raises(duckdb.IOException, match="view probe"):
        reader.__enter__()

    connection.close.assert_called_once_with()
    assert reader._connection is None
