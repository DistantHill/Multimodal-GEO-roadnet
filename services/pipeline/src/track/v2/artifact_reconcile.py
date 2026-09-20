"""Read-only reconciliation planning for immutable artifact datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
import stat
from typing import Any
from uuid import UUID


class ReconcileError(RuntimeError):
    """The artifact root cannot be scanned safely."""


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    artifact_id: UUID
    stage: str
    owner_id: UUID
    relative_path: str
    created_at: datetime
    owner_status: str
    finished_at: datetime | None
    lifecycle_state: str | None
    event_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconcileAction:
    kind: str
    relative_path: str
    artifact_id: UUID | None
    reason: str
    age_seconds: int | None
    eligible: bool
    protected: bool


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    generated_at: datetime
    grace_days: int
    actions: tuple[ReconcileAction, ...]


_QUERY = """
SELECT a.id, a.stage, COALESCE(a.refine_id, a.detect_id), a.relative_path,
       a.created_at, COALESCE(rr.status, dr.status),
       COALESCE(rr.finished_at, dr.finished_at), event.state, event.event_at
FROM pipeline_artifacts AS a
LEFT JOIN refine_runs AS rr ON rr.id = a.refine_id
LEFT JOIN detect_runs AS dr ON dr.id = a.detect_id
LEFT JOIN LATERAL (
    SELECT state, event_at FROM pipeline_artifact_events
    WHERE artifact_id = a.id ORDER BY id DESC LIMIT 1
) AS event ON TRUE
ORDER BY a.id
"""


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _age(now: datetime, then: datetime) -> int:
    return int((now - then).total_seconds())


def _root(path: Path | str) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise ValueError("parquet_root must be absolute")
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError as exc:
        raise ReconcileError("parquet_root does not exist") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ReconcileError("parquet_root must be a real directory")
    return root.resolve(strict=True)


def _controlled_path(stage: Any, owner: Any, relative: Any) -> tuple[UUID, str] | None:
    if stage not in ("refine", "detect"):
        return None
    try:
        owner_id = owner if type(owner) is UUID else UUID(str(owner))
    except (TypeError, ValueError):
        return None
    expected = f"{stage}/{owner_id}"
    if relative != expected or PurePosixPath(expected).parts != (stage, str(owner_id)):
        return None
    return owner_id, expected


def _filesystem_entries(root: Path) -> tuple[dict[str, datetime], list[ReconcileAction]]:
    found: dict[str, datetime] = {}
    issues: list[ReconcileAction] = []
    for stage in ("refine", "detect"):
        stage_path = root / stage
        if not stage_path.exists():
            continue
        mode = stage_path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ReconcileError(f"unsafe final stage path: {stage}")
        for entry in stage_path.iterdir():
            relative = f"{stage}/{entry.name}"
            mode = entry.lstat().st_mode
            try:
                UUID(entry.name)
                valid = stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)
            except ValueError:
                valid = False
            if valid:
                found[relative] = datetime.fromtimestamp(entry.stat().st_mtime).astimezone()
            else:
                issues.append(ReconcileAction("malformed_path", relative, None,
                    "final entry is not a real UUID directory", None, False, True))
    staging = root / ".artifact-staging"
    if staging.exists():
        if stat.S_ISLNK(staging.lstat().st_mode) or not staging.is_dir():
            raise ReconcileError("unsafe staging root")
        for stage_path in staging.iterdir():
            if (stage_path.name not in ("refine", "detect") or
                    stat.S_ISLNK(stage_path.lstat().st_mode) or not stage_path.is_dir()):
                issues.append(ReconcileAction("malformed_path",
                    stage_path.relative_to(root).as_posix(), None,
                    "staging stage is not a real controlled directory", None, False, True))
                continue
            for run_path in stage_path.iterdir():
                if (stat.S_ISLNK(run_path.lstat().st_mode) or not run_path.is_dir()):
                    issues.append(ReconcileAction("malformed_path",
                        run_path.relative_to(root).as_posix(), None,
                        "staging Run is not a real directory", None, False, True))
                    continue
                try:
                    UUID(run_path.name)
                except ValueError:
                    issues.append(ReconcileAction("malformed_path",
                        run_path.relative_to(root).as_posix(), None,
                        "staging Run name is not a UUID", None, False, True))
                    continue
                artifact_names: set[str] = set()
                for entry in run_path.iterdir():
                    if entry.name.startswith(".") and entry.name.endswith(".publication.json"):
                        continue
                    relative = entry.relative_to(root).as_posix()
                    try:
                        UUID(entry.name)
                        valid = stat.S_ISDIR(entry.lstat().st_mode) and not entry.is_symlink()
                    except ValueError:
                        valid = False
                    if valid:
                        artifact_names.add(entry.name)
                        found[relative] = datetime.fromtimestamp(entry.stat().st_mtime).astimezone()
                    else:
                        issues.append(ReconcileAction("malformed_path", relative, None,
                            "staging entry is not a real UUID directory", None, False, True))
                for marker in run_path.glob(".*.publication.json"):
                    artifact_name = marker.name[1:-len(".publication.json")]
                    if artifact_name in artifact_names:
                        continue
                    relative = marker.relative_to(root).as_posix()
                    if marker.is_symlink() or not marker.is_file():
                        issues.append(ReconcileAction("malformed_path", relative, None,
                            "staging marker is not a real file", None, False, True))
                    else:
                        found[relative] = datetime.fromtimestamp(marker.stat().st_mtime).astimezone()
    return found, issues


def build_reconcile_plan(
    cur: Any, parquet_root: Path | str, now: datetime, grace_days: int,
) -> ReconcilePlan:
    """Build a deterministic plan without mutating PostgreSQL or the filesystem."""
    if type(grace_days) is not int or grace_days < 0:
        raise ValueError("grace_days must be a nonnegative integer")
    if not _aware(now):
        raise ValueError("now must be timezone-aware")
    root = _root(parquet_root)
    cur.execute(_QUERY, ())
    rows = cur.fetchall()
    filesystem, actions = _filesystem_entries(root)
    registered: set[str] = set()
    claimed: set[str] = set()
    seen_artifacts: set[UUID] = set()
    threshold = timedelta(days=grace_days).total_seconds()

    for row in rows:
        if type(row[3]) is str:
            parts = PurePosixPath(row[3]).parts
            try:
                if len(parts) == 2 and parts[0] in ("refine", "detect"):
                    UUID(parts[1])
                    claimed.add(row[3])
            except ValueError:
                pass
        try:
            artifact_id = row[0] if type(row[0]) is UUID else UUID(str(row[0]))
        except (TypeError, ValueError):
            actions.append(ReconcileAction("malformed_registry", str(row[3]), None,
                "artifact id is not a UUID", None, False, True))
            continue
        controlled = _controlled_path(row[1], row[2], row[3])
        timestamps_ok = _aware(row[4]) and (row[6] is None or _aware(row[6])) and (
            row[8] is None or _aware(row[8]))
        if artifact_id in seen_artifacts or controlled is None or row[3] in registered:
            reason = "duplicate artifact/path or owner/path mismatch"
        elif row[5] not in ("completed", "failed", "running"):
            reason = "unknown or missing owner Run status"
        elif row[7] not in ("present", "deleted"):
            reason = "unknown or missing lifecycle state"
        elif not timestamps_ok:
            reason = "registry timestamps must be timezone-aware"
        else:
            reason = ""
        if reason:
            actions.append(ReconcileAction("malformed_registry", str(row[3]), artifact_id,
                reason, None, False, True))
            continue
        seen_artifacts.add(artifact_id)
        registered.add(row[3])
        record = RegistryRecord(artifact_id, row[1], controlled[0], row[3], row[4],
            row[5], row[6], row[7], row[8])
        exists = record.relative_path in filesystem
        age = _age(now, max(value for value in (
            record.created_at, record.event_at, record.finished_at) if value is not None))
        protected = record.owner_status in ("completed", "running")
        if record.lifecycle_state == "present" and exists:
            kind = "registered_present"
            eligible = record.owner_status == "failed" and age >= threshold
            why = "dataset exists and matches a present registry entry"
        elif record.lifecycle_state == "present":
            kind, eligible = "registered_missing", False
            why = "present registry entry has no final dataset"
            protected = True
        elif exists:
            kind, eligible = "deleted_residue", True
            why = "deleted registry entry still has a final dataset"
        else:
            kind, eligible = "registered_deleted", False
            why = "deleted registry entry has no final dataset"
        actions.append(ReconcileAction(kind, record.relative_path, record.artifact_id,
            why, age, eligible and not protected, protected))

    for relative, modified in filesystem.items():
        if relative in claimed:
            continue
        age = _age(now, modified)
        kind = "staging_leftover" if relative.startswith(".artifact-staging/") else "final_orphan"
        actions.append(ReconcileAction(kind, relative, None,
            "filesystem directory has no registry entry", age, age >= threshold, False))
    actions.sort(key=lambda item: (item.relative_path, item.kind,
                                   "" if item.artifact_id is None else str(item.artifact_id)))
    return ReconcilePlan(now, grace_days, tuple(actions))
