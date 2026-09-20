from __future__ import annotations

import hashlib
import json
import math
from uuid import UUID, uuid4

import pytest

from track.v2.db.artifact_registry import (
    ArtifactDescriptor,
    ArtifactRegistryReadError,
    append_deleted,
    canonical_json,
    find_refine_artifact,
    insert_present,
)


SHA_A = "a" * 64


def _manifest(*, version: int = 1) -> dict[str, object]:
    schema = [
        {"name": "lon", "type": "float64", "nullable": False},
        {"name": "kind", "type": "string", "nullable": True},
    ]
    fingerprint = hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()
    return {
        "version": version,
        "tables": [{
            "name": "kept_points",
            "schema": schema,
            "schema_fingerprint": fingerprint,
            "row_count": 7,
            "files": [
                {"relative_path": "kept/part-0.parquet", "byte_count": 101, "sha256": SHA_A},
                {"relative_path": "kept/part-1.parquet", "byte_count": 99, "sha256": "b" * 64},
            ],
        }],
    }


def _descriptor(stage: str = "refine", **changes: object) -> ArtifactDescriptor:
    values: dict[str, object] = {
        "id": uuid4(),
        "stage": stage,
        "relative_path": "refine/run-1",
        "source_fingerprint": {"table": "points", "count": 8},
        "manifest": _manifest(),
        "refine_id" if stage == "refine" else "detect_id": uuid4(),
    }
    values.update(changes)
    return ArtifactDescriptor.create(**values)  # type: ignore[arg-type]


class _Cursor:
    def __init__(self, fail_at: int | None = None, rows=()) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_at = fail_at
        self.committed = False
        self.rows = list(rows)

    def execute(self, statement: str, params: tuple[object, ...]) -> None:
        self.calls.append((" ".join(statement.split()), params))
        if self.fail_at == len(self.calls):
            raise RuntimeError("database probe")

    def commit(self) -> None:
        self.committed = True

    def fetchmany(self, size: int):
        return self.rows[:size]


def test_descriptor_computes_canonical_manifest_hash_and_totals() -> None:
    manifest = _manifest()
    descriptor = _descriptor(
        manifest=manifest,
        row_count=7,
        byte_count=200,
        manifest_sha256=hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest(),
    )

    assert descriptor.row_count == 7
    assert descriptor.byte_count == 200
    assert descriptor.manifest_sha256 == hashlib.sha256(
        descriptor.manifest_json.encode("utf-8")
    ).hexdigest()
    assert json.loads(descriptor.manifest_json) == manifest
    assert descriptor.source_fingerprint_json == '{"count":8,"table":"points"}'


@pytest.mark.parametrize("stage", ["refine", "detect"])
def test_insert_present_maps_owner_and_preserves_sql_order(stage: str) -> None:
    descriptor = _descriptor(stage)
    cur = _Cursor()
    insert_present(cur, descriptor)

    assert len(cur.calls) == 2
    assert cur.calls[0][0].startswith("INSERT INTO pipeline_artifacts")
    assert cur.calls[1][0].startswith("INSERT INTO pipeline_artifact_events")
    params = cur.calls[0][1]
    assert params[:5] == (
        descriptor.id, stage, descriptor.refine_id, descriptor.detect_id,
        descriptor.relative_path,
    )
    assert cur.calls[1][1] == (descriptor.id,)
    assert not cur.committed


def test_distinct_runs_are_isolated_in_parameterized_inserts() -> None:
    first, second = _descriptor(), _descriptor()
    cur = _Cursor()
    insert_present(cur, first)
    insert_present(cur, second)
    assert cur.calls[0][1][0:3] == (first.id, "refine", first.refine_id)
    assert cur.calls[2][1][0:3] == (second.id, "refine", second.refine_id)
    assert first.refine_id != second.refine_id
    assert "%s" in cur.calls[0][0]


def test_duplicate_publish_is_left_to_database_uniqueness_constraints() -> None:
    descriptor = _descriptor()
    cur = _Cursor()
    insert_present(cur, descriptor)
    insert_present(cur, descriptor)
    assert len(cur.calls) == 4
    assert all("SELECT" not in statement for statement, _params in cur.calls)
    assert cur.calls[0][1] == cur.calls[2][1]


def test_second_insert_failure_propagates_without_commit() -> None:
    cur = _Cursor(fail_at=2)
    with pytest.raises(RuntimeError, match="database probe"):
        insert_present(cur, _descriptor())
    assert len(cur.calls) == 2
    assert not cur.committed


def test_append_deleted_validates_and_does_not_commit() -> None:
    artifact_id = uuid4()
    cur = _Cursor()
    append_deleted(cur, artifact_id, "retention expired")
    assert cur.calls[0][1] == (artifact_id, "retention expired")
    assert not cur.committed
    with pytest.raises(ValueError, match="reason"):
        append_deleted(cur, artifact_id, " ")


@pytest.mark.parametrize("path", ["/absolute", ".", "..", "a/./b", "a/../b", "a//b", "a/"])
def test_dataset_paths_must_be_normalized_relative_paths(path: str) -> None:
    with pytest.raises(ValueError, match="path|segments"):
        _descriptor(relative_path=path)


@pytest.mark.parametrize("path", ["/part.parquet", "../part.parquet", "x//part.parquet"])
def test_file_paths_must_be_normalized_relative_paths(path: str) -> None:
    manifest = _manifest()
    manifest["tables"][0]["files"][0]["relative_path"] = path  # type: ignore[index]
    with pytest.raises(ValueError, match="path|segments"):
        _descriptor(manifest=manifest)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"stage": "clip"}, "stage"),
        ({"stage": "refine", "refine_id": None}, "owner"),
        ({"stage": "refine", "detect_id": UUID(int=2)}, "owner"),
        ({"id": "not-a-uuid"}, "UUID"),
        ({"row_count": -1}, "row_count"),
        ({"byte_count": 201}, "byte_count"),
        ({"manifest_sha256": SHA_A}, "manifest_sha256"),
    ],
)
def test_descriptor_rejects_invalid_identity_owner_totals_and_hash(
    change: dict[str, object], message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _descriptor(**change)


def test_manifest_rejects_unknown_version_and_keys() -> None:
    with pytest.raises(ValueError, match="version"):
        _descriptor(manifest=_manifest(version=2))
    manifest = _manifest()
    manifest["unknown"] = True
    with pytest.raises(ValueError, match="exactly"):
        _descriptor(manifest=manifest)


def test_manifest_rejects_empty_and_duplicate_tables_and_files() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        _descriptor(manifest={"version": 1, "tables": []})
    manifest = _manifest()
    manifest["tables"].append(dict(manifest["tables"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ValueError, match="duplicate logical tables|duplicate files"):
        _descriptor(manifest=manifest)
    manifest = _manifest()
    files = manifest["tables"][0]["files"]  # type: ignore[index]
    files.append(dict(files[0]))
    with pytest.raises(ValueError, match="duplicate files"):
        _descriptor(manifest=manifest)
    manifest = _manifest()
    manifest["tables"][0]["files"] = []  # type: ignore[index]
    with pytest.raises(ValueError, match="nonempty"):
        _descriptor(manifest=manifest)


def test_schema_requires_known_envelope_unique_fields_and_matching_fingerprint() -> None:
    manifest = _manifest()
    manifest["tables"][0]["schema"][0]["unknown"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="exactly"):
        _descriptor(manifest=manifest)
    manifest = _manifest()
    fields = manifest["tables"][0]["schema"]  # type: ignore[index]
    fields.append(dict(fields[0]))
    with pytest.raises(ValueError, match="duplicate field"):
        _descriptor(manifest=manifest)
    manifest = _manifest()
    manifest["tables"][0]["schema_fingerprint"] = "c" * 64  # type: ignore[index]
    with pytest.raises(ValueError, match="schema_fingerprint"):
        _descriptor(manifest=manifest)


@pytest.mark.parametrize("bad", [math.nan, math.inf, object(), {1: "not a string key"}])
def test_source_fingerprint_rejects_non_json_values(bad: object) -> None:
    with pytest.raises(ValueError, match="JSON"):
        _descriptor(source_fingerprint={"bad": bad})


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64])
def test_file_digest_is_strict_lowercase_sha256(digest: str) -> None:
    manifest = _manifest()
    manifest["tables"][0]["files"][0]["sha256"] = digest  # type: ignore[index]
    with pytest.raises(ValueError, match="SHA-256"):
        _descriptor(manifest=manifest)


def test_frozen_descriptor_cannot_bypass_validation_by_direct_construction() -> None:
    valid = _descriptor()
    with pytest.raises(ValueError, match="canonical JSON"):
        ArtifactDescriptor(
            valid.id, valid.stage, valid.refine_id, valid.detect_id, valid.relative_path,
            '{"z":1, "a":2}', valid.manifest_json, valid.manifest_sha256,
            valid.row_count, valid.byte_count,
        )


def _registry_row(descriptor: ArtifactDescriptor, state: str = "present") -> tuple[object, ...]:
    return (
        descriptor.id, descriptor.stage, descriptor.refine_id, descriptor.detect_id,
        descriptor.relative_path, 1, json.loads(descriptor.manifest_json),
        descriptor.manifest_sha256, json.loads(descriptor.source_fingerprint_json),
        descriptor.row_count, descriptor.byte_count, state,
    )


def test_find_refine_artifact_strictly_rebuilds_latest_snapshot_without_commit() -> None:
    descriptor = _descriptor()
    descriptor = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=descriptor.refine_id,
        relative_path=f"refine/{descriptor.refine_id}",
        source_fingerprint=json.loads(descriptor.source_fingerprint_json),
        manifest=json.loads(descriptor.manifest_json),
    )
    cur = _Cursor(rows=[_registry_row(descriptor, "deleted")])

    snapshot = find_refine_artifact(cur, descriptor.refine_id)

    assert snapshot.descriptor == descriptor
    assert snapshot.state == "deleted"
    assert "ORDER BY e.id DESC LIMIT 1" in cur.calls[0][0]
    assert cur.calls[0][1] == (descriptor.refine_id,)
    assert not cur.committed


@pytest.mark.parametrize("rows,message", [([], "unknown"), ([object(), object()], "duplicate")])
def test_find_refine_artifact_rejects_unknown_and_duplicates(rows, message) -> None:
    with pytest.raises(ArtifactRegistryReadError, match=message):
        find_refine_artifact(_Cursor(rows=rows), uuid4())


def test_find_refine_artifact_rejects_missing_event_and_corrupt_metadata() -> None:
    descriptor = _descriptor()
    missing_event = list(_registry_row(descriptor))
    missing_event[-1] = None
    with pytest.raises(ArtifactRegistryReadError, match="malformed"):
        find_refine_artifact(_Cursor(rows=[tuple(missing_event)]), descriptor.refine_id)
    corrupt = list(_registry_row(descriptor))
    corrupt[9] += 1
    with pytest.raises(ArtifactRegistryReadError, match="descriptor"):
        find_refine_artifact(_Cursor(rows=[tuple(corrupt)]), descriptor.refine_id)
