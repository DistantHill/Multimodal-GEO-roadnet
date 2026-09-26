from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import stat
from uuid import uuid4

import pytest

from track.v2.artifacts import publish_dataset as publication


def staging(tmp_path: Path) -> publication.StagingDataset:
    return publication.create_staging(tmp_path.resolve(), "refine", uuid4(), uuid4())


def test_publish_uses_one_rename_and_makes_tree_read_only(tmp_path, monkeypatch):
    handle = staging(tmp_path)
    table = handle.path / "kept_points"
    table.mkdir()
    data = table / "part-00000.parquet"
    data.write_bytes(b"PAR1dataPAR1")
    calls = []
    real_rename = os.rename
    def observed_rename(source, target):
        calls.append((source, target))
        real_rename(source, target)

    monkeypatch.setattr(os, "rename", observed_rename)

    result = publication.publish(handle)

    assert calls == [(handle.path, handle.final_path)]
    assert result.relative_path == f"refine/{handle.run_id}"
    assert result.file_relative_paths == ("kept_points/part-00000.parquet",)
    assert result.final_path == handle.final_path
    assert not any(path.name.endswith(".publication.json") for path in result.final_path.iterdir())
    published_data = result.final_path / "kept_points" / data.name
    assert stat.S_IMODE(published_data.stat().st_mode) == 0o444
    assert stat.S_IMODE(published_data.parent.stat().st_mode) == 0o555
    assert stat.S_IMODE(result.final_path.stat().st_mode) == 0o555


def test_empty_or_nonregular_staging_never_creates_final(tmp_path):
    empty = staging(tmp_path)
    with pytest.raises(publication.PublicationError, match="no data files"):
        publication.publish(empty)
    assert not empty.final_path.exists()

    if hasattr(os, "mkfifo"):
        fifo = staging(tmp_path)
        os.mkfifo(fifo.path / "partial.parquet")
        with pytest.raises(publication.PublicationError, match="non-regular"):
            publication.publish(fifo)
        assert not fifo.final_path.exists()


def test_existing_final_is_never_overwritten(tmp_path):
    handle = staging(tmp_path)
    (handle.path / "part.parquet").write_bytes(b"new")
    handle.final_path.mkdir(parents=True)
    existing = handle.final_path / "part.parquet"
    existing.write_bytes(b"old")
    with pytest.raises(publication.PublicationError, match="already exists"):
        publication.publish(handle)
    assert existing.read_bytes() == b"old"


def test_symlink_and_escaped_handle_are_rejected(tmp_path):
    handle = staging(tmp_path)
    (handle.path / "part.parquet").write_bytes(b"data")
    (handle.path / "link").symlink_to(handle.path / "part.parquet")
    with pytest.raises(publication.PublicationError, match="symlink"):
        publication.publish(handle)
    escaped = replace(handle, path=tmp_path.parent / "outside")
    with pytest.raises(publication.PublicationError, match="controlled layout"):
        publication.publish(escaped)


def test_cross_device_is_rejected_before_rename(tmp_path, monkeypatch):
    handle = staging(tmp_path)
    (handle.path / "part.parquet").write_bytes(b"data")
    real_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if path == handle.final_path.parent:
            values = list(value)
            values[2] = value.st_dev + 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(publication.PublicationError, match="different filesystems"):
        publication.publish(handle)
    assert not handle.final_path.exists()


def test_published_orphan_survives_later_database_error(tmp_path):
    handle = staging(tmp_path)
    (handle.path / "part.parquet").write_bytes(b"data")
    result = publication.publish(handle)
    with pytest.raises(RuntimeError, match="database"):
        raise RuntimeError("database transaction failed")
    assert (result.final_path / "part.parquet").read_bytes() == b"data"


def test_rename_failure_leaves_cleanable_staging_and_no_final(tmp_path, monkeypatch):
    handle = staging(tmp_path)
    data = handle.path / "part.parquet"
    data.write_bytes(b"data")

    def fail_rename(*_):
        raise OSError("failed")

    monkeypatch.setattr(os, "rename", fail_rename)
    with pytest.raises(publication.PublicationError, match="rename failed"):
        publication.publish(handle)
    assert not handle.final_path.exists()
    assert stat.S_IMODE(data.stat().st_mode) == 0o644
    assert publication.abandon_staging(handle) is True


def test_abandon_is_idempotent_and_cannot_remove_final_or_unowned(tmp_path):
    handle = staging(tmp_path)
    assert publication.abandon_staging(handle) is True
    assert publication.abandon_staging(handle) is False

    published = staging(tmp_path)
    (published.path / "part.parquet").write_bytes(b"data")
    publication.publish(published)
    assert publication.abandon_staging(published) is False
    assert published.final_path.exists()
    with pytest.raises(publication.PublicationError, match="controlled layout"):
        publication.abandon_staging(replace(published, path=published.final_path))

    unowned = staging(tmp_path)
    next(unowned.path.parent.glob(".*.publication.json")).unlink()
    (unowned.path / "part.parquet").write_bytes(b"data")
    with pytest.raises(publication.PublicationError, match="marker"):
        publication.abandon_staging(unowned)
    assert unowned.path.exists()


@pytest.mark.parametrize("stage", ["clip", "../refine", "REFINE"])
def test_stage_and_root_inputs_fail_closed(tmp_path, stage):
    with pytest.raises(ValueError, match="stage"):
        publication.create_staging(tmp_path.resolve(), stage, uuid4())
    with pytest.raises(ValueError, match="absolute"):
        publication.create_staging(Path("relative"), "refine", uuid4())


def test_symlink_root_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="not a symlink"):
        publication.create_staging(alias.absolute(), "refine", uuid4())
