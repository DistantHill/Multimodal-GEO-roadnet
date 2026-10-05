"""Build and verify immutable Parquet artifact manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any
from uuid import UUID

import duckdb

from track.v2.artifacts.publish_dataset import StagingDataset, _validate_handle
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json


class ParquetArtifactError(RuntimeError):
    """A staged or published Parquet artifact failed closed validation."""


def _connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(database=":memory:", config={
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "threads": "1",
    })


def _relative(value: str) -> str:
    if type(value) is not str or not value or value.endswith("/") or "//" in value:
        raise ParquetArtifactError("file path must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ParquetArtifactError("file path escapes the dataset")
    if path.suffix != ".parquet":
        raise ParquetArtifactError("every declared file must use the .parquet extension")
    return value


def _real_file(root: Path, relative_path: str) -> Path:
    path = root / relative_path
    try:
        path.relative_to(root)
        info = path.lstat()
    except (ValueError, FileNotFoundError) as exc:
        raise ParquetArtifactError(f"missing declared file: {relative_path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ParquetArtifactError(f"declared path is not a regular file: {relative_path}")
    current = root
    for part in PurePosixPath(relative_path).parts[:-1]:
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ParquetArtifactError("dataset contains a symlink")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parquet_metadata(
    connection: duckdb.DuckDBPyConnection, path: Path,
) -> tuple[int, list[dict[str, Any]]]:
    try:
        metadata = connection.execute(
            "SELECT num_rows FROM parquet_file_metadata(?)", [str(path)]
        ).fetchall()
        rows = connection.execute(
            "SELECT name, repetition_type, num_children, duckdb_type "
            "FROM parquet_schema(?) ORDER BY column_id", [str(path)]
        ).fetchall()
    except duckdb.Error as exc:
        raise ParquetArtifactError(f"invalid Parquet footer: {path.name}") from exc
    if len(metadata) != 1 or not rows:
        raise ParquetArtifactError("Parquet metadata must describe exactly one file")
    root, fields = rows[0], rows[1:]
    if root[2] != len(fields) or any(field[2] is not None for field in fields):
        raise ParquetArtifactError("nested Parquet schemas are not supported")
    schema = [{
        "name": field[0], "type": field[3], "nullable": field[1] != "REQUIRED",
    } for field in fields]
    if not schema or any(not field["name"] or not field["type"] for field in schema):
        raise ParquetArtifactError("Parquet schema must contain named flat fields")
    return int(metadata[0][0]), schema


def _staging_files(handle: StagingDataset) -> set[str]:
    _validate_handle(handle)
    found: set[str] = set()
    for current, directories, files in os.walk(handle.path, followlinks=False):
        directory = Path(current)
        for name in directories:
            child = directory / name
            if stat.S_ISLNK(child.lstat().st_mode):
                raise ParquetArtifactError("staging contains a symlink")
        for name in files:
            child = directory / name
            if stat.S_ISLNK(child.lstat().st_mode) or not child.is_file():
                raise ParquetArtifactError("staging contains a non-regular file")
            found.add(child.relative_to(handle.path).as_posix())
    return found


def build_descriptor(
    handle: StagingDataset, logical_tables: Mapping[str, Sequence[str]],
    source_fingerprint: Mapping[str, Any],
) -> ArtifactDescriptor:
    """Inspect every staged Parquet file and build registry manifest v1."""
    if not logical_tables:
        raise ParquetArtifactError("logical table manifest must not be empty")
    if any(type(name) is not str or not name.strip()
           or not isinstance(paths, Sequence) or isinstance(paths, (str, bytes))
           for name, paths in logical_tables.items()):
        raise ParquetArtifactError("logical table names and file lists must be explicit")
    declared = [_relative(path) for paths in logical_tables.values() for path in paths]
    if any(not paths for paths in logical_tables.values()):
        raise ParquetArtifactError("each logical table must declare files")
    if len(declared) != len(set(declared)):
        raise ParquetArtifactError("a Parquet file may be declared only once")
    if set(declared) != _staging_files(handle):
        raise ParquetArtifactError("declared files must exactly match staging contents")
    tables: list[dict[str, Any]] = []
    with _connection() as connection:
        for name in sorted(logical_tables):
            paths = sorted(logical_tables[name])
            files, row_count, schema = [], 0, None
            for relative_path in paths:
                path = _real_file(handle.path, relative_path)
                rows, current_schema = _parquet_metadata(connection, path)
                if schema is not None and current_schema != schema:
                    raise ParquetArtifactError("Parquet parts have different schemas")
                schema, row_count = current_schema, row_count + rows
                files.append({"relative_path": relative_path, "byte_count": path.stat().st_size,
                              "sha256": _sha256(path)})
            fingerprint = hashlib.sha256(canonical_json(schema).encode()).hexdigest()
            tables.append({"name": name, "schema": schema, "schema_fingerprint": fingerprint,
                           "row_count": row_count, "files": files})
    owner = {f"{handle.stage}_id": handle.run_id}
    return ArtifactDescriptor.create(
        id=handle.artifact_id, stage=handle.stage, relative_path=f"{handle.stage}/{handle.run_id}",
        source_fingerprint=source_fingerprint, manifest={"version": 1, "tables": tables}, **owner,
    )


def _published_root(root: Path | str) -> Path:
    path = Path(root)
    if (not path.is_absolute() or not path.exists() or not path.is_dir()
            or stat.S_ISLNK(path.lstat().st_mode)):
        raise ParquetArtifactError("publication root must be an existing real absolute directory")
    return path.resolve(strict=True)


def validate_published(
    root: Path | str, descriptor: ArtifactDescriptor, state: str, *, verify_sha256: bool = False,
) -> None:
    """Quickly validate a present immutable dataset before consumption."""
    if state != "present":
        raise ParquetArtifactError("artifact lifecycle state is not present")
    base = _published_root(root)
    owner = descriptor.refine_id if descriptor.stage == "refine" else descriptor.detect_id
    expected = f"{descriptor.stage}/{owner}"
    if descriptor.relative_path != expected:
        raise ParquetArtifactError("descriptor does not use its controlled stage/run path")
    stage_path = base / descriptor.stage
    if (not stage_path.exists() or stage_path.is_symlink() or not stage_path.is_dir()):
        raise ParquetArtifactError("published stage is missing or not a real directory")
    final = base / expected
    if not final.exists() or stat.S_ISLNK(final.lstat().st_mode) or not final.is_dir():
        raise ParquetArtifactError("published dataset is missing or not a real directory")
    manifest = json.loads(descriptor.manifest_json)
    declared = {file["relative_path"] for table in manifest["tables"] for file in table["files"]}
    actual: set[str] = set()
    for current, directories, files in os.walk(final, followlinks=False):
        directory = Path(current)
        if stat.S_IMODE(directory.lstat().st_mode) & 0o222:
            raise ParquetArtifactError("published dataset has writable mode bits")
        for name in directories + files:
            child = directory / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ParquetArtifactError("published dataset contains a symlink")
            if stat.S_IMODE(mode) & 0o222:
                raise ParquetArtifactError("published dataset has writable mode bits")
        for name in files:
            child = directory / name
            if not stat.S_ISREG(child.lstat().st_mode):
                raise ParquetArtifactError("published dataset contains a non-regular file")
            actual.add(child.relative_to(final).as_posix())
    if actual != declared:
        raise ParquetArtifactError("published files do not match the manifest")
    with _connection() as connection:
        for table in manifest["tables"]:
            total = 0
            for file in table["files"]:
                path = _real_file(final, file["relative_path"])
                if path.stat().st_size != file["byte_count"]:
                    raise ParquetArtifactError("published file size does not match the manifest")
                rows, schema = _parquet_metadata(connection, path)
                if schema != table["schema"]:
                    raise ParquetArtifactError("published schema does not match the manifest")
                if verify_sha256 and _sha256(path) != file["sha256"]:
                    raise ParquetArtifactError("published SHA-256 does not match the manifest")
                total += rows
            if total != table["row_count"]:
                raise ParquetArtifactError("published row count does not match the manifest")


def find_orphans(root: Path | str, registered_relative_paths: Sequence[str]) -> tuple[str, ...]:
    """Return valid final datasets absent from the PostgreSQL registry snapshot."""
    base = _published_root(root)
    registered: set[str] = set()
    for relative_path in registered_relative_paths:
        parts = PurePosixPath(relative_path).parts if type(relative_path) is str else ()
        if len(parts) != 2 or parts[0] not in ("refine", "detect"):
            raise ParquetArtifactError("registered dataset path is not controlled")
        try:
            UUID(parts[1])
        except ValueError as exc:
            raise ParquetArtifactError("registered dataset path is not controlled") from exc
        registered.add(relative_path)
    found: set[str] = set()
    for stage in ("refine", "detect"):
        stage_path = base / stage
        if not stage_path.exists():
            continue
        if stat.S_ISLNK(stage_path.lstat().st_mode) or not stage_path.is_dir():
            raise ParquetArtifactError(f"malformed final stage entry: {stage}")
        for entry in stage_path.iterdir():
            if stat.S_ISLNK(entry.lstat().st_mode) or not entry.is_dir():
                raise ParquetArtifactError(f"malformed final entry: {stage}/{entry.name}")
            try:
                UUID(entry.name)
            except ValueError as exc:
                raise ParquetArtifactError(f"malformed final entry: {stage}/{entry.name}") from exc
            found.add(f"{stage}/{entry.name}")
    return tuple(sorted(found - registered))
