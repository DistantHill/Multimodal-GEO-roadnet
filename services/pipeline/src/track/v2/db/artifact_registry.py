"""Strict, transaction-neutral registry API for immutable columnar artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID


def canonical_json(value: Any) -> str:
    """Return the single JSON representation used for registry fingerprints."""
    _validate_json(value, "JSON value")
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _validate_json(value: Any, name: str) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise ValueError(f"{name} contains a non-finite JSON float")
    if type(value) is list:
        for item in value:
            _validate_json(item, name)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{name} JSON object keys must be strings")
            _validate_json(item, name)
        return
    raise ValueError(f"{name} contains a non-JSON value")


def _object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a JSON object")
    _validate_json(value, name)
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} must contain exactly {sorted(expected)}")


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty, trimmed text without NUL")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _sha256(value: Any, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _uuid(value: Any, name: str) -> UUID:
    try:
        parsed = value if type(value) is UUID else UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    return parsed


def _relative_path(value: Any, name: str) -> str:
    text = _text(value, name)
    path = PurePosixPath(text)
    if path.is_absolute() or text.endswith("/") or "//" in text:
        raise ValueError(f"{name} must be a normalized relative POSIX path")
    if any(part in ("", ".", "..") for part in text.split("/")):
        raise ValueError(f"{name} cannot contain empty, dot, or parent segments")
    return text


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if type(value) is not list or not value:
        raise ValueError(f"{name} must be a nonempty JSON array")
    return value


def _field(value: Any) -> dict[str, Any]:
    obj = _object(value, "schema field")
    _keys(obj, {"name", "type", "nullable"}, "schema field")
    if type(obj["nullable"]) is not bool:
        raise ValueError("schema field nullable must be boolean")
    return {
        "name": _text(obj["name"], "schema field name"),
        "type": _text(obj["type"], "schema field type"),
        "nullable": obj["nullable"],
    }


def _file(value: Any) -> dict[str, Any]:
    obj = _object(value, "manifest file")
    _keys(obj, {"relative_path", "byte_count", "sha256"}, "manifest file")
    return {
        "relative_path": _relative_path(obj["relative_path"], "file relative_path"),
        "byte_count": _count(obj["byte_count"], "file byte_count"),
        "sha256": _sha256(obj["sha256"], "file sha256"),
    }


def _table(value: Any) -> dict[str, Any]:
    obj = _object(value, "logical table")
    expected = {"name", "schema", "schema_fingerprint", "row_count", "files"}
    _keys(obj, expected, "logical table")
    fields = [_field(item) for item in _sequence(obj["schema"], "table schema")]
    field_names = [field["name"] for field in fields]
    if len(field_names) != len(set(field_names)):
        raise ValueError("table schema contains duplicate field names")
    schema_fingerprint = hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()
    if _sha256(obj["schema_fingerprint"], "schema_fingerprint") != schema_fingerprint:
        raise ValueError("schema_fingerprint does not match the canonical schema")
    files = [_file(item) for item in _sequence(obj["files"], "table files")]
    paths = [item["relative_path"] for item in files]
    if len(paths) != len(set(paths)):
        raise ValueError("logical table contains duplicate files")
    return {
        "name": _text(obj["name"], "logical table name"),
        "schema": fields,
        "schema_fingerprint": schema_fingerprint,
        "row_count": _count(obj["row_count"], "table row_count"),
        "files": files,
    }


def validate_manifest(value: Any) -> tuple[dict[str, Any], int, int]:
    """Validate manifest v1 and return normalized JSON plus aggregate totals."""
    obj = _object(value, "manifest")
    _keys(obj, {"version", "tables"}, "manifest")
    if type(obj["version"]) is not int or obj["version"] != 1:
        raise ValueError("manifest version must be 1")
    tables = [_table(item) for item in _sequence(obj["tables"], "manifest tables")]
    names = [table["name"] for table in tables]
    paths = [file["relative_path"] for table in tables for file in table["files"]]
    if len(names) != len(set(names)):
        raise ValueError("manifest contains duplicate logical tables")
    if len(paths) != len(set(paths)):
        raise ValueError("manifest contains duplicate files")
    row_count = sum(table["row_count"] for table in tables)
    byte_count = sum(file["byte_count"] for table in tables for file in table["files"])
    return {"version": 1, "tables": tables}, row_count, byte_count


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    id: UUID
    stage: str
    refine_id: UUID | None
    detect_id: UUID | None
    relative_path: str
    source_fingerprint_json: str
    manifest_json: str
    manifest_sha256: str
    row_count: int
    byte_count: int

    def __post_init__(self) -> None:
        _uuid(self.id, "artifact id")
        if self.stage not in ("refine", "detect"):
            raise ValueError("stage must be refine or detect")
        if (self.stage == "refine") != (self.refine_id is not None) or (
            self.stage == "detect"
        ) != (self.detect_id is not None):
            raise ValueError("stage requires exactly its matching owner Run id")
        if self.refine_id is not None:
            _uuid(self.refine_id, "refine_id")
        if self.detect_id is not None:
            _uuid(self.detect_id, "detect_id")
        _relative_path(self.relative_path, "dataset relative_path")
        source = json.loads(self.source_fingerprint_json)
        if canonical_json(_object(source, "source_fingerprint")) != self.source_fingerprint_json:
            raise ValueError("source_fingerprint_json must be canonical JSON")
        manifest = json.loads(self.manifest_json)
        normalized, rows, bytes_ = validate_manifest(manifest)
        if canonical_json(normalized) != self.manifest_json:
            raise ValueError("manifest_json must be canonical JSON")
        if _sha256(self.manifest_sha256, "manifest_sha256") != hashlib.sha256(
            self.manifest_json.encode("utf-8")
        ).hexdigest():
            raise ValueError("manifest_sha256 does not match canonical manifest")
        if _count(self.row_count, "row_count") != rows:
            raise ValueError("row_count does not match manifest contents")
        if _count(self.byte_count, "byte_count") != bytes_:
            raise ValueError("byte_count does not match manifest contents")

    @classmethod
    def create(
        cls, *, id: UUID | str, stage: str, refine_id: UUID | str | None = None,
        detect_id: UUID | str | None = None, relative_path: str,
        source_fingerprint: Mapping[str, Any], manifest: Mapping[str, Any],
        row_count: int | None = None, byte_count: int | None = None,
        manifest_sha256: str | None = None,
    ) -> ArtifactDescriptor:
        if stage not in ("refine", "detect"):
            raise ValueError("stage must be refine or detect")
        refine = None if refine_id is None else _uuid(refine_id, "refine_id")
        detect = None if detect_id is None else _uuid(detect_id, "detect_id")
        if (stage == "refine") != (refine is not None) or (stage == "detect") != (detect is not None):
            raise ValueError("stage requires exactly its matching owner Run id")
        source = _object(source_fingerprint, "source_fingerprint")
        normalized, computed_rows, computed_bytes = validate_manifest(manifest)
        manifest_json = canonical_json(normalized)
        digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        if row_count is not None and _count(row_count, "row_count") != computed_rows:
            raise ValueError("row_count does not match manifest contents")
        if byte_count is not None and _count(byte_count, "byte_count") != computed_bytes:
            raise ValueError("byte_count does not match manifest contents")
        if manifest_sha256 is not None and _sha256(manifest_sha256, "manifest_sha256") != digest:
            raise ValueError("manifest_sha256 does not match canonical manifest")
        return cls(
            _uuid(id, "artifact id"), stage, refine, detect,
            _relative_path(relative_path, "dataset relative_path"), canonical_json(source),
            manifest_json, digest, computed_rows, computed_bytes,
        )


class ArtifactRegistryReadError(RuntimeError):
    """Registry rows cannot identify one trustworthy artifact snapshot."""


@dataclass(frozen=True, slots=True)
class RegisteredArtifact:
    descriptor: ArtifactDescriptor
    state: str


def _json_object_text(value: Any, name: str) -> str:
    if type(value) is str:
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ArtifactRegistryReadError(f"{name} is invalid JSON") from exc
    try:
        return canonical_json(_object(value, name))
    except ValueError as exc:
        raise ArtifactRegistryReadError(f"invalid {name}") from exc


def _find_artifact(cur: Any, owner_id: UUID | str, stage: str) -> RegisteredArtifact:
    owner_field = f"{stage}_id"
    owner = _uuid(owner_id, owner_field)
    cur.execute(
        "SELECT a.id, a.stage, a.refine_id, a.detect_id, a.relative_path, "
        "a.manifest_version, a.manifest, a.manifest_sha256, a.source_fingerprint, "
        "a.row_count, a.byte_count, latest.state "
        "FROM pipeline_artifacts a LEFT JOIN LATERAL ("
        "SELECT e.state FROM pipeline_artifact_events e "
        "WHERE e.artifact_id = a.id ORDER BY e.id DESC LIMIT 1"
        f") latest ON true WHERE a.{owner_field} = %s LIMIT 2",
        (owner,),
    )
    rows = cur.fetchmany(2)
    if not rows:
        raise ArtifactRegistryReadError(f"unknown {stage.title()} artifact: {owner}")
    if len(rows) != 1:
        raise ArtifactRegistryReadError(f"duplicate {stage.title()} artifacts: {owner}")
    row = rows[0]
    if len(row) != 12 or row[5] != 1 or row[11] not in ("present", "deleted"):
        raise ArtifactRegistryReadError("artifact registry row is malformed")
    try:
        descriptor = ArtifactDescriptor(
            id=_uuid(row[0], "artifact id"), stage=row[1],
            refine_id=None if row[2] is None else _uuid(row[2], "refine_id"),
            detect_id=None if row[3] is None else _uuid(row[3], "detect_id"),
            relative_path=row[4],
            manifest_json=_json_object_text(row[6], "manifest"),
            manifest_sha256=row[7],
            source_fingerprint_json=_json_object_text(row[8], "source_fingerprint"),
            row_count=row[9], byte_count=row[10],
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactRegistryReadError("artifact descriptor is malformed") from exc
    descriptor_owner = descriptor.refine_id if stage == "refine" else descriptor.detect_id
    if descriptor.stage != stage or descriptor_owner != owner:
        raise ArtifactRegistryReadError("artifact owner is malformed")
    return RegisteredArtifact(descriptor, row[11])


def find_refine_artifact(cur: Any, refine_id: UUID | str) -> RegisteredArtifact:
    """Read one Refine artifact and latest lifecycle event without transaction control."""
    return _find_artifact(cur, refine_id, "refine")


def find_detect_artifact(cur: Any, detect_id: UUID | str) -> RegisteredArtifact:
    """Read one Detect artifact and latest lifecycle event without transaction control."""
    return _find_artifact(cur, detect_id, "detect")


def insert_present(cur: Any, descriptor: ArtifactDescriptor) -> None:
    """Insert metadata then its present event; the caller owns the transaction."""
    if type(descriptor) is not ArtifactDescriptor:
        raise ValueError("descriptor must be an ArtifactDescriptor")
    cur.execute(
        "INSERT INTO pipeline_artifacts "
        "(id, stage, refine_id, detect_id, relative_path, manifest_version, manifest, "
        "manifest_sha256, source_fingerprint, row_count, byte_count) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s::jsonb, %s, %s::jsonb, %s, %s)",
        (descriptor.id, descriptor.stage, descriptor.refine_id, descriptor.detect_id,
         descriptor.relative_path, descriptor.manifest_json, descriptor.manifest_sha256,
         descriptor.source_fingerprint_json, descriptor.row_count, descriptor.byte_count),
    )
    cur.execute(
        "INSERT INTO pipeline_artifact_events (artifact_id, state, reason) "
        "VALUES (%s, 'present', NULL)",
        (descriptor.id,),
    )


def append_deleted(cur: Any, artifact_id: UUID | str, reason: str) -> None:
    """Append a deleted event; database constraints enforce lifecycle order."""
    cur.execute(
        "INSERT INTO pipeline_artifact_events (artifact_id, state, reason) "
        "VALUES (%s, 'deleted', %s)",
        (_uuid(artifact_id, "artifact id"), _text(reason, "reason")),
    )
