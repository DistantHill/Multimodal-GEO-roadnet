"""Bounded, attempt-isolated disk artifacts for a central transactional writer.

This module enforces an already allocated *per-attempt* byte allowance, not a
global quota. The scheduler must reserve concurrent allowances centrally. Free
space checks are advisory under concurrent external disk use; ENOSPC still fails
the attempt. Producers own the memory used to construct each input bytes chunk.
No artifact is automatically deleted after publication or database submission.

POSIX directory descriptors and O_NOFOLLOW reject symlink traversal, including
the runtime root's ancestors. The runtime tree must be owned by the application;
this is not a sandbox against another process with permission to rename its
directories or mutate open files.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Iterator


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_FINAL_NAME = "result.bin"
_PARTIAL_NAME = ".partial"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SpoolLimitError(OSError):
    """The attempt allowance or required free disk reserve would be exceeded."""


class ArtifactIntegrityError(ValueError):
    """Artifact content does not match its immutable descriptor."""


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("identity must be a safe ASCII component of 1 to 128 characters")


def _integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True, slots=True)
class SpoolArtifact:
    run_id: str
    task_id: str
    attempt_id: str
    relative_path: str
    byte_count: int
    sha256: str

    def __post_init__(self) -> None:
        for component in (self.run_id, self.task_id, self.attempt_id):
            _identifier(component)
        expected = "/".join((self.run_id, self.task_id, self.attempt_id, _FINAL_NAME))
        if self.relative_path != expected:
            raise ValueError("artifact path must exactly match its attempt identity")
        _integer(self.byte_count, "byte_count")
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase SHA256 hex digest")


@contextmanager
def _root_directory(runtime_root: str | Path) -> Iterator[int]:
    root = Path(runtime_root)
    if not root.is_absolute() or ".." in root.parts or root == Path("/"):
        raise ValueError("runtime_root must be an explicit absolute non-root directory")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in root.parts[1:]:
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _child_directory(parent_fd: int, name: str, *, create: bool = False) -> Iterator[int]:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        else:
            os.fsync(parent_fd)
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _check_disk_reserve(directory_fd: int, additional_bytes: int, reserve: int) -> None:
    usage = os.fstatvfs(directory_fd)
    if usage.f_bavail * usage.f_frsize - additional_bytes < reserve:
        raise SpoolLimitError("attempt write would breach the free disk reserve")


def write_patch_artifact(
    runtime_root: str | Path,
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    chunks: Iterable[bytes],
    attempt_byte_limit: int,
    disk_reserve_bytes: int = 0,
) -> SpoolArtifact:
    """Publish one artifact only after streaming writes, fsync, and atomic rename.

    The caller reserves ``attempt_byte_limit`` before dispatch. It is not a
    global quota. Identity directories are exclusive and never reused, including
    after a failed attempt; retries must have a new attempt_id. Only files this
    invocation created are removed on failure. Empty identity directories remain
    as fences. The runtime root must already exist. Filesystem errors propagate.
    """
    for component in (run_id, task_id, attempt_id):
        _identifier(component)
    _integer(attempt_byte_limit, "attempt_byte_limit")
    _integer(disk_reserve_bytes, "disk_reserve_bytes")
    with _root_directory(runtime_root) as root_fd:
        with _child_directory(root_fd, run_id, create=True) as run_fd:
            with _child_directory(run_fd, task_id, create=True) as task_fd:
                os.mkdir(attempt_id, mode=0o700, dir_fd=task_fd)
                os.fsync(task_fd)
                with _child_directory(task_fd, attempt_id) as attempt_fd:
                    return _write_attempt(
                        attempt_fd, run_id, task_id, attempt_id, chunks,
                        attempt_byte_limit, disk_reserve_bytes,
                    )


def _write_attempt(
    attempt_fd: int,
    run_id: str,
    task_id: str,
    attempt_id: str,
    chunks: Iterable[bytes],
    attempt_byte_limit: int,
    disk_reserve_bytes: int,
) -> SpoolArtifact:
    owned_name: str | None = None
    fd: int | None = None
    try:
        _check_disk_reserve(attempt_fd, 0, disk_reserve_bytes)
        fd = os.open(
            _PARTIAL_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=attempt_fd,
        )
        owned_name = _PARTIAL_NAME
        digest = hashlib.sha256()
        byte_count = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("artifact producers must yield bytes chunks")
            if len(chunk) > attempt_byte_limit - byte_count:
                raise SpoolLimitError("attempt byte allowance exceeded")
            _check_disk_reserve(attempt_fd, len(chunk), disk_reserve_bytes)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("artifact write made no progress")
                remaining = remaining[written:]
            digest.update(chunk)
            byte_count += len(chunk)
            # Allocation units may exceed len(chunk); detect that actual cost too.
            _check_disk_reserve(attempt_fd, 0, disk_reserve_bytes)
        os.fsync(fd)
        _check_disk_reserve(attempt_fd, 0, disk_reserve_bytes)
        os.close(fd)
        fd = None
        os.rename(_PARTIAL_NAME, _FINAL_NAME, src_dir_fd=attempt_fd, dst_dir_fd=attempt_fd)
        owned_name = _FINAL_NAME
        os.fsync(attempt_fd)
        _check_disk_reserve(attempt_fd, 0, disk_reserve_bytes)
        return SpoolArtifact(
            run_id=run_id, task_id=task_id, attempt_id=attempt_id,
            relative_path="/".join((run_id, task_id, attempt_id, _FINAL_NAME)),
            byte_count=byte_count, sha256=digest.hexdigest(),
        )
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if owned_name is not None:
            try:
                os.unlink(owned_name, dir_fd=attempt_fd)
            except OSError:
                pass
        raise


def iter_artifact_chunks(
    runtime_root: str | Path,
    artifact: SpoolArtifact,
    *,
    chunk_size: int = 65_536,
) -> Iterator[bytes]:
    """Read at most chunk_size bytes at once; validate integrity on exhaustion.

    Consumers must exhaust the iterator successfully before committing results.
    Early chunks are provisional, not individually verified. Early termination
    must close the iterator to release descriptors. A separate verification pass
    does not make subsequent reads safe against concurrent mutation.
    """
    _integer(chunk_size, "chunk_size", minimum=1)
    if not isinstance(artifact, SpoolArtifact):
        raise TypeError("artifact must be a SpoolArtifact")
    artifact.__post_init__()
    with _root_directory(runtime_root) as root_fd:
        with _child_directory(root_fd, artifact.run_id) as run_fd:
            with _child_directory(run_fd, artifact.task_id) as task_fd:
                with _child_directory(task_fd, artifact.attempt_id) as attempt_fd:
                    fd = os.open(
                        _FINAL_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=attempt_fd,
                    )
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode):
                            raise ArtifactIntegrityError("artifact must be a regular file")
                        if info.st_size != artifact.byte_count:
                            raise ArtifactIntegrityError("artifact byte count mismatch")
                        digest = hashlib.sha256()
                        byte_count = 0
                        while chunk := os.read(fd, chunk_size):
                            byte_count += len(chunk)
                            if byte_count > artifact.byte_count:
                                raise ArtifactIntegrityError("artifact byte count mismatch")
                            digest.update(chunk)
                            yield chunk
                        if byte_count != artifact.byte_count or digest.hexdigest() != artifact.sha256:
                            raise ArtifactIntegrityError("artifact digest or byte count mismatch")
                    finally:
                        os.close(fd)


def verify_artifact(
    runtime_root: str | Path,
    artifact: SpoolArtifact,
    *,
    chunk_size: int = 65_536,
) -> None:
    """Exhaust a bounded read without retaining chunks; raise on invalid content."""
    for _ in iter_artifact_chunks(runtime_root, artifact, chunk_size=chunk_size):
        pass
