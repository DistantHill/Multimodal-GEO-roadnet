from dataclasses import FrozenInstanceError, replace
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import os
from threading import Barrier
from types import SimpleNamespace

import pytest

from track.v2 import patch_spool as spool


def write(root, chunks=(b"abc", b"def"), **kwargs):
    options = dict(
        run_id="run-1", task_id="task-1", attempt_id="attempt-1",
        chunks=chunks, attempt_byte_limit=100,
    )
    options.update(kwargs)
    return spool.write_patch_artifact(root, **options)


def attempt_path(root, attempt_id="attempt-1"):
    return root / "run-1" / "task-1" / attempt_id


def test_stream_roundtrip_and_immutable_descriptor(tmp_path):
    artifact = write(tmp_path)
    assert artifact.relative_path == "run-1/task-1/attempt-1/result.bin"
    assert artifact.byte_count == 6
    assert artifact.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert list(spool.iter_artifact_chunks(tmp_path, artifact, chunk_size=2)) == [b"ab", b"cd", b"ef"]
    assert spool.verify_artifact(tmp_path, artifact) is None
    assert not (attempt_path(tmp_path) / ".partial").exists()
    with pytest.raises(FrozenInstanceError):
        artifact.byte_count = 3


def test_empty_artifact_and_zero_allowance(tmp_path):
    artifact = write(tmp_path, chunks=(b"",), attempt_byte_limit=0)
    assert artifact.byte_count == 0
    assert list(spool.iter_artifact_chunks(tmp_path, artifact)) == []


def test_producer_is_lazy_and_final_name_only_visible_after_rename(tmp_path, monkeypatch):
    events = []
    original_fsync = os.fsync
    original_rename = os.rename

    def fsync(fd):
        events.append("fsync")
        return original_fsync(fd)

    def rename(*args, **kwargs):
        assert events[-1] == "fsync"
        assert (attempt_path(tmp_path) / ".partial").read_bytes() == b"abcdef"
        assert not (attempt_path(tmp_path) / "result.bin").exists()
        result = original_rename(*args, **kwargs)
        events.append("rename")
        return result

    def chunks():
        assert (attempt_path(tmp_path) / ".partial").exists()
        yield b"abc"
        assert (attempt_path(tmp_path) / ".partial").stat().st_size == 3
        assert not (attempt_path(tmp_path) / "result.bin").exists()
        yield b"def"

    monkeypatch.setattr(spool.os, "fsync", fsync)
    monkeypatch.setattr(spool.os, "rename", rename)
    artifact = write(tmp_path, chunks=chunks())
    assert events[-2:] == ["rename", "fsync"]
    spool.verify_artifact(tmp_path, artifact)


@pytest.mark.parametrize("error", [RuntimeError("producer failed"), KeyboardInterrupt()])
def test_producer_failure_cleans_only_owned_files_and_fences_identity(tmp_path, error):
    other = write(tmp_path, attempt_id="attempt-other")

    def chunks():
        yield b"abc"
        raise error

    with pytest.raises(type(error)):
        write(tmp_path, chunks=chunks())
    assert list(attempt_path(tmp_path).iterdir()) == []
    spool.verify_artifact(tmp_path, other)
    with pytest.raises(FileExistsError):
        write(tmp_path)


def test_duplicate_attempt_does_not_consume_producer_or_overwrite(tmp_path):
    artifact = write(tmp_path)

    def chunks():
        pytest.fail("duplicate attempt consumed its producer")
        yield b"ignored"

    with pytest.raises(FileExistsError):
        write(tmp_path, chunks=chunks())
    assert (tmp_path / artifact.relative_path).read_bytes() == b"abcdef"


def test_concurrent_duplicate_attempt_has_exactly_one_owner(tmp_path):
    ready = Barrier(2)

    def attempt():
        ready.wait(timeout=5)
        try:
            return write(tmp_path)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    artifacts = [result for result in results if result is not None]
    assert len(artifacts) == 1
    spool.verify_artifact(tmp_path, artifacts[0])


def test_failure_does_not_remove_foreign_file_inside_attempt(tmp_path):
    def chunks():
        yield b"abc"
        (attempt_path(tmp_path) / "foreign.txt").write_text("retain")
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError):
        write(tmp_path, chunks=chunks())
    assert [path.name for path in attempt_path(tmp_path).iterdir()] == ["foreign.txt"]


def test_exact_allowance_and_one_byte_overflow(tmp_path):
    write(tmp_path, attempt_byte_limit=6)
    with pytest.raises(spool.SpoolLimitError, match="allowance"):
        write(tmp_path, attempt_id="attempt-over", attempt_byte_limit=5)
    assert list(attempt_path(tmp_path, "attempt-over").iterdir()) == []


def test_disk_reserve_checked_for_every_chunk(tmp_path, monkeypatch):
    free = iter([20, 20, 20, 5])
    monkeypatch.setattr(spool.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=next(free), f_frsize=1))
    with pytest.raises(spool.SpoolLimitError, match="reserve"):
        write(tmp_path, disk_reserve_bytes=3)
    assert list(attempt_path(tmp_path).iterdir()) == []


def test_disk_allocation_block_cost_checked_after_write(tmp_path, monkeypatch):
    free = iter([4096, 4096, 0])
    monkeypatch.setattr(spool.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=next(free), f_frsize=1))
    with pytest.raises(spool.SpoolLimitError):
        write(tmp_path, chunks=[b"a"], disk_reserve_bytes=4095)
    assert list(attempt_path(tmp_path).iterdir()) == []


def test_empty_artifact_also_checks_disk_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(spool.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=0, f_frsize=1))
    with pytest.raises(spool.SpoolLimitError):
        write(tmp_path, chunks=(), disk_reserve_bytes=1)
    assert list(attempt_path(tmp_path).iterdir()) == []


def test_external_disk_exhaustion_propagates_and_cleans_partial(tmp_path, monkeypatch):
    def fail(fd, data):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(spool.os, "write", fail)
    with pytest.raises(OSError) as error:
        write(tmp_path)
    assert error.value.errno == errno.ENOSPC
    assert list(attempt_path(tmp_path).iterdir()) == []


def test_partial_os_writes_are_completed_without_copying_all_output(tmp_path, monkeypatch):
    original_write = os.write
    calls = []

    def short_write(fd, data):
        calls.append(len(data))
        return original_write(fd, data[:1])

    monkeypatch.setattr(spool.os, "write", short_write)
    artifact = write(tmp_path)
    assert calls == [3, 2, 1, 3, 2, 1]
    spool.verify_artifact(tmp_path, artifact)


def test_zero_progress_write_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(spool.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        write(tmp_path)
    assert list(attempt_path(tmp_path).iterdir()) == []


@pytest.mark.parametrize("point", ["file_fsync", "rename", "directory_fsync"])
def test_publication_errors_never_leave_valid_result(tmp_path, monkeypatch, point):
    original_fsync = os.fsync
    original_rename = os.rename
    renamed = False

    def rename(*args, **kwargs):
        nonlocal renamed
        if point == "rename":
            raise OSError("rename failed")
        result = original_rename(*args, **kwargs)
        renamed = True
        return result

    def fsync(fd):
        if point == "file_fsync" and (attempt_path(tmp_path) / ".partial").exists():
            raise OSError("file fsync failed")
        if point == "directory_fsync" and renamed:
            raise OSError("directory fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(spool.os, "fsync", fsync)
    monkeypatch.setattr(spool.os, "rename", rename)
    with pytest.raises(OSError, match="failed"):
        write(tmp_path)
    assert list(attempt_path(tmp_path).iterdir()) == []


@pytest.mark.parametrize("field", ["run_id", "task_id", "attempt_id"])
@pytest.mark.parametrize("value", ["", ".", "..", "../escape", "/absolute", "a/b", "a\\b", "中文", "a" * 129, None])
def test_unsafe_identity_rejected_before_creating_files(tmp_path, field, value):
    with pytest.raises(ValueError, match="identity"):
        write(tmp_path, **{field: value})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["attempt_byte_limit", "disk_reserve_bytes"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "1", None])
def test_invalid_allowance_rejected(tmp_path, field, value):
    with pytest.raises(ValueError):
        write(tmp_path, **{field: value})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("chunk", ["text", bytearray(b"a"), memoryview(b"a"), None])
def test_chunks_must_be_bytes(tmp_path, chunk):
    with pytest.raises(TypeError, match="bytes"):
        write(tmp_path, chunks=[chunk])
    assert list(attempt_path(tmp_path).iterdir()) == []


@pytest.mark.parametrize("root", ["relative", "/", "/tmp/../tmp"])
def test_invalid_root_rejected(root):
    with pytest.raises(ValueError, match="runtime_root"):
        write(root)


def test_root_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        write(tmp_path / "missing")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("component", ["root", "run", "task", "attempt"])
def test_writer_rejects_symlink_components(tmp_path, component):
    external = tmp_path / "external"
    external.mkdir()
    runtime = tmp_path / "runtime"
    if component == "root":
        runtime.symlink_to(external, target_is_directory=True)
    else:
        runtime.mkdir()
        if component == "run":
            link = runtime / "run-1"
        elif component == "task":
            (runtime / "run-1").mkdir()
            link = runtime / "run-1" / "task-1"
        else:
            (runtime / "run-1" / "task-1").mkdir(parents=True)
            link = attempt_path(runtime)
        link.symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        write(runtime)
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("content", [b"badbad", b"abc", b"abcdefg"])
def test_corruption_rejected(tmp_path, content):
    artifact = write(tmp_path)
    (tmp_path / artifact.relative_path).write_bytes(content)
    with pytest.raises(spool.ArtifactIntegrityError):
        spool.verify_artifact(tmp_path, artifact, chunk_size=1)


def test_digest_failure_at_exhaustion_not_per_chunk(tmp_path):
    artifact = write(tmp_path)
    altered = replace(artifact, sha256="0" * 64)
    reader = spool.iter_artifact_chunks(tmp_path, altered, chunk_size=6)
    assert next(reader) == b"abcdef"
    with pytest.raises(spool.ArtifactIntegrityError):
        next(reader)


def test_reader_early_close_releases_file_descriptor(tmp_path, monkeypatch):
    artifact = write(tmp_path)
    closed = []
    original_close = os.close

    def close(fd):
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(spool.os, "close", close)
    reader = spool.iter_artifact_chunks(tmp_path, artifact, chunk_size=1)
    assert next(reader) == b"a"
    count = len(closed)
    reader.close()
    assert len(closed) - count == 5  # artifact, attempt, task, run, runtime root


def test_reader_rejects_final_symlink_and_fifo(tmp_path):
    artifact = write(tmp_path)
    path = tmp_path / artifact.relative_path
    path.unlink()
    target = tmp_path / "other.bin"
    target.write_bytes(b"abcdef")
    path.symlink_to(target)
    with pytest.raises(OSError):
        spool.verify_artifact(tmp_path, artifact)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(spool.ArtifactIntegrityError, match="regular"):
        spool.verify_artifact(tmp_path, artifact)


def test_reader_rejects_directory_symlink(tmp_path):
    artifact = write(tmp_path)
    old = attempt_path(tmp_path)
    moved = tmp_path / "moved"
    old.rename(moved)
    old.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        spool.verify_artifact(tmp_path, artifact)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_reader_rejects_invalid_chunk_size(tmp_path, value):
    artifact = write(tmp_path)
    with pytest.raises(ValueError):
        spool.verify_artifact(tmp_path, artifact, chunk_size=value)


@pytest.mark.parametrize("changes", [
    {"relative_path": "../escape"}, {"relative_path": "/absolute"},
    {"task_id": "other-task"}, {"byte_count": -1}, {"byte_count": True},
    {"sha256": "ABC"}, {"sha256": "A" * 64},
])
def test_descriptor_validation(tmp_path, changes):
    with pytest.raises(ValueError):
        replace(write(tmp_path), **changes)
