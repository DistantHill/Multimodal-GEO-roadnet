from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from track.v2.artifacts.reconcile_datasets import (
    ReconcileAction,
    ReconcileError,
    ReconcilePlan,
    apply_reconcile_plan,
)


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, records=None, registered_paths=(), *, event_error=False, observe=None):
        self.records = records or {}
        self.registered_paths = set(registered_paths)
        self.event_error = event_error
        self.observe = observe
        self.calls = []
        self.result = None

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "WHERE a.id = %s" in sql:
            self.result = self.records.get(params[0])
        elif "SELECT 1 FROM pipeline_artifacts" in sql:
            self.result = (1,) if params[0] in self.registered_paths else None
        elif sql.startswith("INSERT INTO pipeline_artifact_events"):
            if self.observe:
                self.observe()
            if self.event_error:
                raise RuntimeError("event failed")

    def fetchone(self):
        return self.result


def action(kind, relative, *, artifact_id=None, eligible=True, protected=False):
    return ReconcileAction(kind, relative, artifact_id, "test", 700_000, eligible, protected)


def plan(*actions):
    return ReconcilePlan(NOW, 7, actions)


def dataset(root: Path, relative: str) -> Path:
    path = root / relative
    path.mkdir(parents=True)
    (path / "part.parquet").write_bytes(b"data")
    return path


def registered(root: Path, *, state="present"):
    run_id, artifact_id = uuid4(), uuid4()
    relative = f"refine/{run_id}"
    path = dataset(root, relative)
    (path / "part.parquet").chmod(0o444)
    path.chmod(0o555)
    kind = "registered_present" if state == "present" else "deleted_residue"
    item = action(kind, relative, artifact_id=artifact_id)
    cur = Cursor({artifact_id: (relative, "failed", state)})
    return item, cur, path


def test_no_eligible_action_is_a_true_noop(tmp_path):
    run_id = uuid4()
    path = dataset(tmp_path, f"refine/{run_id}")
    cur = Cursor()
    result = apply_reconcile_plan(
        cur, tmp_path.resolve(), plan(action("registered_present", f"refine/{run_id}",
                                          artifact_id=uuid4(), eligible=False, protected=True)),
        reason="gc",
    )
    assert path.exists() and not cur.calls
    assert result.appended_events == 0


def test_present_is_deleted_before_event_and_residue_has_no_event(tmp_path):
    item, cur, path = registered(tmp_path)
    cur.observe = lambda: pytest.fail("event inserted before deletion") if path.exists() else None
    result = apply_reconcile_plan(cur, tmp_path.resolve(), plan(item), reason="expired failed Run")
    assert not path.exists()
    assert result.removed_final == (item.relative_path,) and result.appended_events == 1

    residue, residue_cur, residue_path = registered(tmp_path, state="deleted")
    result = apply_reconcile_plan(residue_cur, tmp_path.resolve(), plan(residue), reason="residue")
    assert not residue_path.exists() and result.appended_events == 0
    assert not any(sql.startswith("INSERT") for sql, _ in residue_cur.calls)


def test_orphan_staging_directory_and_sidecar_are_exact_leaves(tmp_path):
    orphan_id, run_id, staged_id, marker_id = uuid4(), uuid4(), uuid4(), uuid4()
    orphan = dataset(tmp_path, f"detect/{orphan_id}")
    staged = dataset(tmp_path, f".artifact-staging/refine/{run_id}/{staged_id}")
    marker = staged.parent / f".{marker_id}.publication.json"
    marker.write_text("{}", encoding="utf-8")
    result = apply_reconcile_plan(
        Cursor(), tmp_path.resolve(), plan(
            action("final_orphan", orphan.relative_to(tmp_path).as_posix()),
            action("staging_leftover", staged.relative_to(tmp_path).as_posix()),
            action("staging_leftover", marker.relative_to(tmp_path).as_posix()),
        ), reason="gc",
    )
    assert not orphan.exists() and not staged.exists() and not marker.exists()
    assert result.removed_final and result.removed_staging and result.removed_sidecars
    assert staged.parent.exists()


@pytest.mark.parametrize("change", ["path", "status", "state", "missing"])
def test_stale_registered_plan_rejects_without_changes(tmp_path, change):
    item, cur, path = registered(tmp_path)
    values = [item.relative_path, "failed", "present"]
    if change == "path":
        values[0] = f"refine/{uuid4()}"
    elif change == "status":
        values[1] = "completed"
    elif change == "state":
        values[2] = "deleted"
    else:
        values = None
    cur.records[item.artifact_id] = None if values is None else tuple(values)
    with pytest.raises(ReconcileError, match="stale"):
        apply_reconcile_plan(cur, tmp_path.resolve(), plan(item), reason="gc")
    assert path.exists()


def test_newly_registered_orphan_is_not_removed(tmp_path):
    run_id = uuid4()
    path = dataset(tmp_path, f"refine/{run_id}")
    item = action("final_orphan", path.relative_to(tmp_path).as_posix())
    with pytest.raises(ReconcileError, match="stale"):
        apply_reconcile_plan(
            Cursor(registered_paths={item.relative_path}), tmp_path.resolve(), plan(item), reason="gc"
        )
    assert path.exists()


def test_duplicate_leaf_is_rejected_without_removal(tmp_path):
    run_id = uuid4()
    path = dataset(tmp_path, f"detect/{run_id}")
    item = action("final_orphan", path.relative_to(tmp_path).as_posix())
    with pytest.raises(ReconcileError, match="duplicate"):
        apply_reconcile_plan(Cursor(), tmp_path.resolve(), plan(item, item), reason="gc")
    assert path.exists()


@pytest.mark.parametrize("unsafe", ["symlink", "missing", "malformed"])
def test_full_batch_preflight_prevents_earlier_safe_removal(tmp_path, unsafe):
    first_id, second_id = uuid4(), uuid4()
    first = dataset(tmp_path, f"detect/{first_id}")
    second = dataset(tmp_path, f"detect/{second_id}")
    second_action = action("final_orphan", f"detect/{second_id}")
    if unsafe == "symlink":
        (second / "part.parquet").unlink()
        (second / "part.parquet").symlink_to(first / "part.parquet")
    elif unsafe == "missing":
        (second / "part.parquet").unlink()
        second.rmdir()
    else:
        second_action = action("final_orphan", "detect/not-a-uuid")
    with pytest.raises(ReconcileError):
        apply_reconcile_plan(
            Cursor(), tmp_path.resolve(),
            plan(action("final_orphan", f"detect/{first_id}"), second_action), reason="gc",
        )
    assert first.exists()


def test_protected_action_is_never_applied(tmp_path):
    run_id = uuid4()
    path = dataset(tmp_path, f"refine/{run_id}")
    result = apply_reconcile_plan(
        Cursor(), tmp_path.resolve(),
        plan(action("registered_present", f"refine/{run_id}", artifact_id=uuid4(),
                    protected=True)), reason="gc",
    )
    assert path.exists() and result.removed_final == ()


def test_event_failure_propagates_after_file_removal_without_commit(tmp_path):
    item, cur, path = registered(tmp_path)
    cur.event_error = True
    with pytest.raises(RuntimeError, match="event failed"):
        apply_reconcile_plan(cur, tmp_path.resolve(), plan(item), reason="gc")
    assert not path.exists()
    assert not any("COMMIT" in sql.upper() for sql, _ in cur.calls)
