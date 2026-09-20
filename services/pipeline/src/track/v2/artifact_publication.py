"""Safe filesystem publication for immutable stage datasets."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
from uuid import UUID, uuid4


_STAGING = ".artifact-staging"
_MARKER_SUFFIX = ".publication.json"
_STAGES = frozenset({"refine", "detect"})


class PublicationError(RuntimeError):
    """A dataset cannot be safely published or abandoned."""


@dataclass(frozen=True, slots=True)
class StagingDataset:
    root: Path
    stage: str
    run_id: UUID
    artifact_id: UUID
    path: Path
    final_path: Path


@dataclass(frozen=True, slots=True)
class PublicationResult:
    root: Path
    relative_path: str
    final_path: Path
    file_relative_paths: tuple[str, ...]


def _uuid(value: UUID | str, name: str) -> UUID:
    try:
        return value if type(value) is UUID else UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _root(path: Path | str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("publication root must be absolute")
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise ValueError("publication root must already exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("publication root must be a real directory, not a symlink")
    return candidate.resolve(strict=True)


def _inside(root: Path, path: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PublicationError("publication path escapes its root") from exc


def _ensure_real_parents(root: Path, path: Path) -> None:
    _inside(root, path)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() and stat.S_ISLNK(current.lstat().st_mode):
            raise PublicationError("publication path contains a symlink")


def _marker(handle: StagingDataset) -> dict[str, str]:
    return {
        "artifact_id": str(handle.artifact_id),
        "run_id": str(handle.run_id),
        "stage": handle.stage,
    }


def _marker_path(handle: StagingDataset) -> Path:
    return handle.path.parent / f".{handle.artifact_id}{_MARKER_SUFFIX}"


def _validate_handle(handle: StagingDataset, *, require_marker: bool = True) -> None:
    if type(handle) is not StagingDataset:
        raise PublicationError("a StagingDataset handle is required")
    root = _root(handle.root)
    expected = root / _STAGING / handle.stage / str(handle.run_id) / str(handle.artifact_id)
    final = root / handle.stage / str(handle.run_id)
    if handle.stage not in _STAGES or handle.path != expected or handle.final_path != final:
        raise PublicationError("staging handle does not match the controlled layout")
    _ensure_real_parents(root, expected)
    if require_marker:
        marker = _marker_path(handle)
        try:
            contents = json.loads(marker.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise PublicationError("staging ownership marker is missing or invalid") from exc
        if contents != _marker(handle) or stat.S_ISLNK(marker.lstat().st_mode):
            raise PublicationError("staging ownership marker does not match its handle")


def create_staging(
    root: Path | str, stage: str, run_id: UUID | str, artifact_id: UUID | str | None = None,
) -> StagingDataset:
    """Create a unique, owned staging directory and return its constrained handle."""
    base = _root(root)
    if stage not in _STAGES:
        raise ValueError("stage must be refine or detect")
    run = _uuid(run_id, "run_id")
    artifact = uuid4() if artifact_id is None else _uuid(artifact_id, "artifact_id")
    path = base / _STAGING / stage / str(run) / str(artifact)
    final = base / stage / str(run)
    _ensure_real_parents(base, path)
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise PublicationError("staging directory already exists") from exc
    handle = StagingDataset(base, stage, run, artifact, path, final)
    marker = _marker_path(handle)
    marker.write_text(json.dumps(_marker(handle), sort_keys=True), encoding="utf-8")
    return handle


def _scan_and_sync(handle: StagingDataset) -> tuple[str, ...]:
    files: list[str] = []
    directories: list[Path] = []
    for current, names, filenames in os.walk(handle.path, topdown=True, followlinks=False):
        directory = Path(current)
        directories.append(directory)
        for name in names:
            child = directory / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise PublicationError("staging contains a symlink or non-directory entry")
            _inside(handle.root, child)
        for name in filenames:
            child = directory / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise PublicationError("staging contains a symlink or non-regular file")
            _inside(handle.root, child)
            with child.open("rb") as stream:
                os.fsync(stream.fileno())
            files.append(child.relative_to(handle.path).as_posix())
    if not files:
        raise PublicationError("staging dataset contains no data files")
    for directory in reversed(directories):
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return tuple(sorted(files))


def _set_tree_modes(path: Path, *, readonly: bool) -> None:
    file_mode, directory_mode = (0o444, 0o555) if readonly else (0o644, 0o755)
    for current, names, filenames in os.walk(path, topdown=False):
        directory = Path(current)
        for name in filenames:
            child = directory / name
            child.chmod(file_mode)
            with child.open("rb") as stream:
                os.fsync(stream.fileno())
        for name in names:
            (directory / name).chmod(directory_mode)
        directory.chmod(directory_mode)
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def publish(handle: StagingDataset) -> PublicationResult:
    """Make an owned dataset read-only, then atomically rename it into final."""
    _validate_handle(handle)
    files = _scan_and_sync(handle)
    handle.final_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_real_parents(handle.root, handle.final_path)
    if handle.final_path.exists() or handle.final_path.is_symlink():
        raise PublicationError("final dataset already exists")
    if handle.path.stat().st_dev != handle.final_path.parent.stat().st_dev:
        raise PublicationError("staging and final paths are on different filesystems")
    _set_tree_modes(handle.path, readonly=True)
    try:
        os.rename(handle.path, handle.final_path)
    except OSError as exc:
        try:
            _set_tree_modes(handle.path, readonly=False)
        except OSError:
            pass
        raise PublicationError("atomic dataset rename failed") from exc
    _marker_path(handle).unlink()
    descriptor = os.open(handle.final_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    relative = handle.final_path.relative_to(handle.root).as_posix()
    return PublicationResult(handle.root, relative, handle.final_path, files)


def abandon_staging(handle: StagingDataset) -> bool:
    """Remove only a staging directory carrying this module's matching marker."""
    _validate_handle(handle, require_marker=False)
    if not handle.path.exists():
        return False
    _validate_handle(handle)
    shutil.rmtree(handle.path)
    _marker_path(handle).unlink()
    return True
