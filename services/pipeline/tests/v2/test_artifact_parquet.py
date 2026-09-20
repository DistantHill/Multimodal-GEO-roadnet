from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from track.v2 import artifact_parquet, artifact_publication


def _write(path: Path, query: str = "SELECT 1::BIGINT AS id, 'a'::VARCHAR AS payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as connection:
        connection.execute(f"COPY ({query}) TO ? (FORMAT PARQUET)", [str(path)])


def _staging(tmp_path: Path, stage: str = "refine"):
    return artifact_publication.create_staging(tmp_path.resolve(), stage, uuid4(), uuid4())


def _built(tmp_path: Path):
    handle = _staging(tmp_path)
    _write(handle.path / "points/part-0.parquet")
    _write(handle.path / "points/part-1.parquet", "SELECT 2::BIGINT AS id, NULL::VARCHAR AS payload")
    descriptor = artifact_parquet.build_descriptor(
        handle, {"points": ["points/part-0.parquet", "points/part-1.parquet"]},
        {"source": "postgres", "count": 2},
    )
    return handle, descriptor


def test_build_descriptor_reads_real_footer_schema_hash_and_totals(tmp_path):
    handle, descriptor = _built(tmp_path)
    manifest = json.loads(descriptor.manifest_json)
    table = manifest["tables"][0]

    assert descriptor.id == handle.artifact_id
    assert descriptor.refine_id == handle.run_id
    assert descriptor.relative_path == f"refine/{handle.run_id}"
    assert descriptor.row_count == 2
    assert descriptor.byte_count == sum(item["byte_count"] for item in table["files"])
    assert [field["name"] for field in table["schema"]] == ["id", "payload"]
    assert [field["type"] for field in table["schema"]] == ["BIGINT", "VARCHAR"]
    assert all(len(item["sha256"]) == 64 for item in table["files"])


def test_manifest_is_canonical_across_table_and_file_input_order(tmp_path):
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first, second = _staging(tmp_path / "first"), _staging(tmp_path / "second")
    for handle in (first, second):
        _write(handle.path / "z/b.parquet")
        _write(handle.path / "z/a.parquet")
        _write(handle.path / "a/part.parquet")
    left = artifact_parquet.build_descriptor(
        first, {"z": ["z/b.parquet", "z/a.parquet"], "a": ["a/part.parquet"]}, {},
    )
    right = artifact_parquet.build_descriptor(
        second, {"a": ["a/part.parquet"], "z": ["z/a.parquet", "z/b.parquet"]}, {},
    )
    assert left.manifest_json == right.manifest_json
    assert left.manifest_sha256 == right.manifest_sha256


@pytest.mark.parametrize(
    "files, message",
    [
        ({}, "empty"),
        ({"points": []}, "declare files"),
        ({"points": ["points/part-0.parquet"]}, "exactly match"),
        ({"points": ["points/part-0.parquet", "points/part-0.parquet", "points/part-1.parquet"]},
         "only once"),
        ({"points": ["points/part-0.csv", "points/part-1.parquet"]}, ".parquet"),
    ],
)
def test_build_rejects_incomplete_duplicate_or_non_parquet_manifest(tmp_path, files, message):
    handle = _staging(tmp_path)
    _write(handle.path / "points/part-0.parquet")
    _write(handle.path / "points/part-1.parquet")
    with pytest.raises(artifact_parquet.ParquetArtifactError, match=message):
        artifact_parquet.build_descriptor(handle, files, {})


def test_build_rejects_non_sequence_file_list(tmp_path):
    handle = _staging(tmp_path)
    _write(handle.path / "part.parquet")
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="explicit"):
        artifact_parquet.build_descriptor(handle, {"points": 1}, {})  # type: ignore[arg-type]


def test_build_rejects_bad_footer_schema_change_and_nested_schema(tmp_path):
    bad = _staging(tmp_path)
    (bad.path / "bad.parquet").write_bytes(b"not parquet")
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="footer"):
        artifact_parquet.build_descriptor(bad, {"points": ["bad.parquet"]}, {})

    changed = _staging(tmp_path)
    _write(changed.path / "a.parquet")
    _write(changed.path / "b.parquet", "SELECT 1::INTEGER AS id, 'a'::VARCHAR AS payload")
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="different schemas"):
        artifact_parquet.build_descriptor(changed, {"points": ["a.parquet", "b.parquet"]}, {})

    nested = _staging(tmp_path)
    _write(nested.path / "nested.parquet", "SELECT {'x': 1} AS value")
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="nested"):
        artifact_parquet.build_descriptor(nested, {"points": ["nested.parquet"]}, {})


def test_published_quick_validation_and_lifecycle_gate(tmp_path):
    handle, descriptor = _built(tmp_path)
    artifact_publication.publish(handle)
    artifact_parquet.validate_published(tmp_path.resolve(), descriptor, "present")
    artifact_parquet.validate_published(
        tmp_path.resolve(), descriptor, "present", verify_sha256=True,
    )
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="not present"):
        artifact_parquet.validate_published(tmp_path.resolve(), descriptor, "deleted")


@pytest.mark.parametrize("mutation", ["missing", "extra", "writable", "symlink"])
def test_published_validation_rejects_tree_mutations(tmp_path, mutation):
    handle, descriptor = _built(tmp_path)
    result = artifact_publication.publish(handle)
    final, data = result.final_path, result.final_path / "points/part-0.parquet"
    if mutation == "missing":
        data.chmod(0o644)
        data.unlink()
    elif mutation == "extra":
        final.chmod(0o755)
        extra = final / "extra.parquet"
        extra.write_bytes(b"extra")
        extra.chmod(0o444)
        final.chmod(0o555)
    elif mutation == "writable":
        data.chmod(0o644)
    else:
        final.chmod(0o755)
        (final / "link.parquet").symlink_to(data)
        final.chmod(0o555)
    with pytest.raises(artifact_parquet.ParquetArtifactError):
        artifact_parquet.validate_published(tmp_path.resolve(), descriptor, "present")


def test_published_validation_rechecks_size_footer_and_schema(tmp_path):
    for kind in ("size", "footer", "schema"):
        root = tmp_path / kind
        root.mkdir()
        handle, descriptor = _built(root)
        result = artifact_publication.publish(handle)
        target = result.final_path / "points/part-0.parquet"
        target.chmod(0o644)
        if kind == "size":
            target.write_bytes(target.read_bytes() + b"x")
        elif kind == "footer":
            target.write_bytes(b"x" * target.stat().st_size)
        else:
            replacement = root / "replacement.parquet"
            _write(replacement, "SELECT 1::INTEGER AS id, 'a'::VARCHAR AS payload")
            target.write_bytes(replacement.read_bytes())
        target.chmod(0o444)
        with pytest.raises(artifact_parquet.ParquetArtifactError):
            artifact_parquet.validate_published(root.resolve(), descriptor, "present")


def test_full_validation_detects_same_size_content_change(tmp_path):
    handle, descriptor = _built(tmp_path)
    replacement = tmp_path / "replacement.parquet"
    _write(replacement, "SELECT 9::BIGINT AS id, 'z'::VARCHAR AS payload")
    target = handle.path / "points/part-0.parquet"
    assert replacement.stat().st_size == target.stat().st_size
    artifact_publication.publish(handle)
    published = handle.final_path / "points/part-0.parquet"
    published.chmod(0o644)
    published.write_bytes(replacement.read_bytes())
    published.chmod(0o444)
    artifact_parquet.validate_published(tmp_path.resolve(), descriptor, "present")
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="SHA-256"):
        artifact_parquet.validate_published(
            tmp_path.resolve(), descriptor, "present", verify_sha256=True,
        )


def test_find_orphans_ignores_staging_and_rejects_malformed_final_entries(tmp_path):
    registered = _staging(tmp_path)
    orphan = _staging(tmp_path, "detect")
    _write(registered.path / "part.parquet")
    _write(orphan.path / "part.parquet")
    registered_result = artifact_publication.publish(registered)
    orphan_result = artifact_publication.publish(orphan)
    _staging(tmp_path)  # An unfinished staging dataset is ignored.

    assert artifact_parquet.find_orphans(
        tmp_path.resolve(), [registered_result.relative_path]
    ) == (orphan_result.relative_path,)
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="registered"):
        artifact_parquet.find_orphans(tmp_path.resolve(), ["refine/not-a-uuid"])

    stage = tmp_path / "refine"
    stage.chmod(0o755)
    (stage / "not-a-uuid").mkdir()
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="malformed"):
        artifact_parquet.find_orphans(tmp_path.resolve(), [])


def test_published_root_and_final_symlinks_fail_closed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="root"):
        artifact_parquet.find_orphans(alias.absolute(), [])

    handle, descriptor = _built(real)
    artifact_publication.publish(handle)
    moved = real / "saved"
    descriptor_path = real / descriptor.relative_path
    descriptor_path.rename(moved)
    descriptor_path.symlink_to(moved, target_is_directory=True)
    with pytest.raises(artifact_parquet.ParquetArtifactError, match="real directory"):
        artifact_parquet.validate_published(real.resolve(), descriptor, "present")
