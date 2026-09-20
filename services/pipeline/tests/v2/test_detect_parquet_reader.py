from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import duckdb
import pytest

from track.v2 import artifact_publication
from track.v2.artifact_parquet import build_descriptor
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json
from track.v2.detect_parquet import DETECT_PARQUET_V1, DetectParquetError
from track.v2.detect_parquet_reader import DetectDatasetReader
from track.v2.models import RuntimeConfig


def _sql(table: str, owner: UUID, *, empty: bool = False) -> str:
    rows = {
        "resolved_points": [
            (10, 4, 0, "vehicle-a", 120.0, 30.0, "kept", 20, "source-20", None, None, None),
            (11, 4, 1, "vehicle-a", 120.1, 30.1, "crossing", None, None, 40, 30, 1),
            (12, 4, 2, "vehicle-a", 120.2, 30.2, "injected", 21, None, None, None, None),
            (20, 9, 0, "vehicle-b", 121.0, 31.0, "kept", 22, "source-22", None, None, None),
        ],
        "intersections": [(30, 120.1, 30.1, 12.0)],
        "entry_exit_points": [(40, 30, 11, 10, 1, 1.0, 2.0, 120.1, 30.1)],
    }[table]
    columns = [column.name for column in DETECT_PARQUET_V1[table]]
    types = [column.duckdb_type for column in DETECT_PARQUET_V1[table]]
    values = []
    for row in rows:
        if table == "resolved_points":
            resolved, subtrack, sequence, vehicle, lon, lat, kind, refined, source, crossing, intersection, crossing_type = row
            row = (owner, resolved, subtrack, sequence,
                   None if kind == "crossing" else sequence, vehicle, lon, lat, kind,
                   refined, source, crossing, intersection, crossing_type)
        else:
            row = (owner, *row)
        values.append("(" + ",".join(
            "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"
            for value in row
        ) + ")")
    relation = "VALUES " + ",".join(values)
    projection = ",".join(
        f"col{index}::{kind} AS {name}"
        for index, (name, kind) in enumerate(zip(columns, types, strict=True))
    )
    return f"SELECT {projection} FROM ({relation})" + (" WHERE false" if empty else "")


def _published(tmp_path: Path, *, empty: bool = False):
    handle = artifact_publication.create_staging(tmp_path.resolve(), "detect", uuid4(), uuid4())
    with duckdb.connect() as connection:
        for table in DETECT_PARQUET_V1:
            output = handle.path / table / "part.parquet"
            output.parent.mkdir()
            connection.execute(f"COPY ({_sql(table, handle.run_id, empty=empty)}) TO ? (FORMAT PARQUET)",
                               [str(output)])
    descriptor = build_descriptor(
        handle, {table: [f"{table}/part.parquet"] for table in DETECT_PARQUET_V1},
        {"version": 1},
    )
    artifact_publication.publish(handle)
    return descriptor


def _descriptor(descriptor, manifest):
    if manifest.get("version") != 1:
        changed = object.__new__(ArtifactDescriptor)
        for name in descriptor.__dataclass_fields__:
            object.__setattr__(changed, name, getattr(descriptor, name))
        object.__setattr__(changed, "manifest_json", json.dumps(manifest))
        return changed
    return ArtifactDescriptor.create(
        id=descriptor.id, stage=descriptor.stage, detect_id=descriptor.detect_id,
        relative_path=descriptor.relative_path,
        source_fingerprint=json.loads(descriptor.source_fingerprint_json), manifest=manifest,
    )


def _make_writable(descriptor, tmp_path):
    root = tmp_path / descriptor.relative_path
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)


def test_reader_builds_stable_clip_models_across_batches(tmp_path):
    descriptor = _published(tmp_path)
    with DetectDatasetReader(tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1)) as reader:
        assert [item.intersection_id for item in reader.intersections()] == [30]
        tracks = list(reader.iter_tracks(batch_size=1))
        assert [(track.vehicle_id, track.trip_id, track.subtrack_index,
                 [point.resolved_point_id for point in track.points]) for track in tracks] == [
            ("vehicle-a", "vehicle-a:subtrack-0004", 4, [10, 11, 12]),
            ("vehicle-b", "vehicle-b:subtrack-0009", 9, [20]),
        ]
        assert tracks[0].points[1].crossing_id == 40
        assert tracks[0].points[1].intersection_id == 30


def test_empty_detect_dataset_is_valid(tmp_path):
    descriptor = _published(tmp_path, empty=True)
    with DetectDatasetReader(tmp_path, descriptor, "present", RuntimeConfig()) as reader:
        assert reader.intersections() == ()
        assert list(reader.iter_tracks(batch_size=1)) == []


@pytest.mark.parametrize("mutation,message", [
    ("version", "unknown"), ("table", "logical tables"), ("column", "schema"),
])
def test_reader_rejects_unknown_manifest_shapes(tmp_path, mutation, message):
    descriptor = _published(tmp_path)
    manifest = json.loads(descriptor.manifest_json)
    if mutation == "version":
        manifest["version"] = 2
    elif mutation == "table":
        manifest["tables"][0]["name"] = "unknown"
    else:
        manifest["tables"][0]["schema"][0]["type"] = "VARCHAR"
        manifest["tables"][0]["schema_fingerprint"] = hashlib.sha256(
            canonical_json(manifest["tables"][0]["schema"]).encode()
        ).hexdigest()
    changed = _descriptor(descriptor, manifest)
    with pytest.raises(Exception, match=message):
        DetectDatasetReader(tmp_path, changed, "present", RuntimeConfig())


def test_reader_rejects_deleted_wrong_owner_missing_extra_and_writable(tmp_path):
    descriptor = _published(tmp_path)
    with pytest.raises(Exception, match="not present"):
        DetectDatasetReader(tmp_path, descriptor, "deleted", RuntimeConfig())
    wrong = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=uuid4(), relative_path=descriptor.relative_path,
        source_fingerprint={"version": 1}, manifest=json.loads(descriptor.manifest_json),
    )
    with pytest.raises(Exception):
        DetectDatasetReader(tmp_path, wrong, "present", RuntimeConfig())
    root = tmp_path / descriptor.relative_path
    _make_writable(descriptor, tmp_path)
    with pytest.raises(Exception, match="writable"):
        DetectDatasetReader(tmp_path, descriptor, "present", RuntimeConfig())
    extra = root / "extra.parquet"
    extra.write_bytes(b"bad")
    extra.chmod(0o444)
    for child in root.rglob("*"):
        child.chmod(0o555 if child.is_dir() else 0o444)
    root.chmod(0o555)
    with pytest.raises(Exception, match="manifest"):
        DetectDatasetReader(tmp_path, descriptor, "present", RuntimeConfig())
    root.chmod(0o755)
    extra.unlink()
    (root / "resolved_points/part.parquet").unlink()
    root.chmod(0o555)
    with pytest.raises(Exception, match="missing|manifest"):
        DetectDatasetReader(tmp_path, descriptor, "present", RuntimeConfig())


def test_relation_validation_rejects_owner_sequence_and_crossing_corruption(tmp_path):
    publication_root = tmp_path
    descriptor = _published(tmp_path)
    root = tmp_path / descriptor.relative_path
    cases = [
        ("resolved_points", "UPDATE data SET detect_id=? WHERE resolved_point_id=10", [str(uuid4())]),
        ("resolved_points", "UPDATE data SET sequence_in_subtrack=8 WHERE resolved_point_id=10", []),
        ("resolved_points", "UPDATE data SET vehicle_id='other' WHERE resolved_point_id=11", []),
        ("resolved_points", "UPDATE data SET lon='NaN' WHERE resolved_point_id=10", []),
        ("resolved_points", "UPDATE data SET resolved_point_id=NULL WHERE resolved_point_id=10", []),
        ("resolved_points", "UPDATE data SET point_kind=NULL WHERE resolved_point_id=10", []),
        ("entry_exit_points", "UPDATE data SET resolved_point_id=12 WHERE crossing_id=40", []),
        ("entry_exit_points", "UPDATE data SET segment_start_resolved_point_id=11 WHERE crossing_id=40", []),
        ("entry_exit_points", "UPDATE data SET segment_start_resolved_point_id=12 WHERE crossing_id=40", []),
        ("entry_exit_points", "UPDATE data SET segment_start_resolved_point_id=999 WHERE crossing_id=40", []),
        ("intersections", "UPDATE data SET radius_m=NULL WHERE intersection_id=30", []),
    ]
    for table, update, params in cases:
        _make_writable(descriptor, publication_root)
        path = root / table / "part.parquet"
        with duckdb.connect() as connection:
            connection.execute("CREATE TABLE data AS SELECT * FROM read_parquet(?)", [str(path)])
            connection.execute(update, params)
            connection.execute("COPY data TO ? (FORMAT PARQUET)", [str(path) + ".new"])
        path.unlink()
        Path(str(path) + ".new").rename(path)
        for child in root.rglob("*"):
            child.chmod(0o555 if child.is_dir() else 0o444)
        root.chmod(0o555)
        manifest = json.loads(descriptor.manifest_json)
        # Fast validation catches byte changes; rebuild only size metadata to reach relation checks.
        for item in manifest["tables"]:
            if item["name"] == table:
                item["files"][0]["byte_count"] = path.stat().st_size
        changed = _descriptor(descriptor, manifest)
        reader = DetectDatasetReader(publication_root, changed, "present", RuntimeConfig())
        with pytest.raises(DetectParquetError, match="Clip input semantics"):
            reader.__enter__()
        assert reader._connection is None
        case_root = tmp_path / uuid4().hex
        case_root.mkdir()
        descriptor = _published(case_root)
        publication_root = case_root
        root = case_root / descriptor.relative_path


def test_q44_reader_uses_duckdb_and_bounded_python_models_only():
    """Q44: DuckDB owns relations; Polars adds no benefit; Python builds one ClipTrack."""
    reader = object.__new__(DetectDatasetReader)
    owner = uuid4()
    reader._descriptor = Mock(detect_id=owner)
    result = Mock()
    result.fetchmany.side_effect = [[(1, 2, 0, "v", 120.0, 30.0, None, None, None)], []]
    connection = Mock()
    connection.execute.return_value = result
    reader._connection = connection
    assert next(reader.iter_tracks(batch_size=1)).trip_id == "v:subtrack-0002"
    assert not result.fetchall.called
