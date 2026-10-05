from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from track.v2.artifacts.reconcile_datasets import ReconcileError, build_reconcile_plan


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


def row(stage="refine", status="completed", state="present", age_days=8,
        *, run_id=None, artifact_id=None, relative=None):
    run_id, artifact_id = run_id or uuid4(), artifact_id or uuid4()
    return (artifact_id, stage, run_id, relative or f"{stage}/{run_id}",
            NOW - timedelta(days=age_days), status,
            NOW - timedelta(days=age_days) if status != "running" else None,
            state, NOW - timedelta(days=age_days) if state is not None else None)


def directory(root: Path, relative: str, age_days=8):
    path = root / relative
    path.mkdir(parents=True)
    timestamp = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def by_kind(plan, kind):
    return [item for item in plan.actions if item.kind == kind]


def test_one_select_and_completed_running_failed_rules(tmp_path):
    completed, running, failed = row(), row(status="running"), row(status="failed")
    for item in (completed, running, failed):
        directory(tmp_path, item[3])
    cur = Cursor([completed, running, failed])
    plan = build_reconcile_plan(cur, tmp_path.resolve(), NOW, 7)
    assert len(cur.calls) == 1 and cur.calls[0][1] == ()
    assert cur.calls[0][0].lstrip().startswith("SELECT")
    assert all(word not in cur.calls[0][0].upper() for word in ("INSERT ", "UPDATE ", "DELETE "))
    items = {item.artifact_id: item for item in plan.actions}
    assert (items[completed[0]].protected, items[completed[0]].eligible) == (True, False)
    assert (items[running[0]].protected, items[running[0]].eligible) == (True, False)
    assert (items[failed[0]].protected, items[failed[0]].eligible) == (False, True)


def test_present_missing_deleted_residue_and_absent(tmp_path):
    missing = row()
    residue = row(status="failed", state="deleted")
    absent = row(state="deleted")
    directory(tmp_path, residue[3])
    plan = build_reconcile_plan(Cursor([missing, residue, absent]), tmp_path.resolve(), NOW, 7)
    assert by_kind(plan, "registered_missing")[0].protected
    assert by_kind(plan, "deleted_residue")[0].eligible
    assert not by_kind(plan, "registered_deleted")[0].eligible


def test_orphan_staging_and_inclusive_grace_boundary(tmp_path):
    final_id, run_id, artifact_id = uuid4(), uuid4(), uuid4()
    directory(tmp_path, f"detect/{final_id}", age_days=7)
    directory(tmp_path, f".artifact-staging/refine/{run_id}/{artifact_id}", age_days=6)
    plan = build_reconcile_plan(Cursor(), tmp_path.resolve(), NOW, 7)
    assert by_kind(plan, "final_orphan")[0].eligible
    assert not by_kind(plan, "staging_leftover")[0].eligible


def test_failed_grace_uses_most_recent_registry_timestamp(tmp_path):
    record = list(row(status="failed", age_days=8))
    record[6] = NOW - timedelta(days=6)
    directory(tmp_path, record[3])
    item = by_kind(build_reconcile_plan(Cursor([tuple(record)]), tmp_path.resolve(), NOW, 7),
                   "registered_present")[0]
    assert item.age_seconds == 6 * 86400 and not item.eligible


def test_orphan_staging_marker_is_reported_once(tmp_path):
    run_id, artifact_id = uuid4(), uuid4()
    run_path = directory(tmp_path, f".artifact-staging/refine/{run_id}")
    marker = run_path / f".{artifact_id}.publication.json"
    marker.write_text("{}")
    timestamp = (NOW - timedelta(days=8)).timestamp()
    os.utime(marker, (timestamp, timestamp))
    actions = build_reconcile_plan(Cursor(), tmp_path.resolve(), NOW, 7).actions
    assert [(item.kind, item.relative_path, item.eligible) for item in actions] == [
        ("staging_leftover", marker.relative_to(tmp_path).as_posix(), True)
    ]

    directory(tmp_path, f".artifact-staging/refine/{run_id}/{artifact_id}")
    actions = build_reconcile_plan(Cursor(), tmp_path.resolve(), NOW, 7).actions
    assert len(actions) == 1 and actions[0].relative_path.endswith(str(artifact_id))


@pytest.mark.parametrize("state", [None, "surprise"])
def test_unknown_or_missing_event_fails_closed(tmp_path, state):
    record = row(state=state)
    directory(tmp_path, record[3])
    item = by_kind(build_reconcile_plan(Cursor([record]), tmp_path.resolve(), NOW, 7),
                   "malformed_registry")[0]
    assert item.protected and not item.eligible
    assert len(build_reconcile_plan(Cursor([record]), tmp_path.resolve(), NOW, 7).actions) == 1


def test_owner_mismatch_duplicate_and_stable_sort(tmp_path):
    owner = uuid4()
    first = row(run_id=owner)
    mismatch = row(run_id=uuid4(), relative=f"refine/{owner}")
    duplicate = row(run_id=owner, relative=first[3])
    one = build_reconcile_plan(Cursor([duplicate, mismatch, first]), tmp_path.resolve(), NOW, 7)
    two = build_reconcile_plan(Cursor([duplicate, mismatch, first]), tmp_path.resolve(), NOW, 7)
    assert one.actions == two.actions
    assert len(by_kind(one, "malformed_registry")) == 2


def test_symlink_and_malformed_entries_fail_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "refine").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReconcileError, match="unsafe final stage"):
        build_reconcile_plan(Cursor(), tmp_path.resolve(), NOW, 7)

    (tmp_path / "refine").unlink()
    directory(tmp_path, "detect/not-a-uuid")
    issue = by_kind(build_reconcile_plan(Cursor(), tmp_path.resolve(), NOW, 7),
                    "malformed_path")[0]
    assert issue.protected and not issue.eligible


@pytest.mark.parametrize("now,days", [(NOW.replace(tzinfo=None), 7), (NOW, -1), (NOW, True)])
def test_time_and_grace_validation(tmp_path, now, days):
    with pytest.raises(ValueError):
        build_reconcile_plan(Cursor(), tmp_path.resolve(), now, days)


@pytest.mark.parametrize("kind", ["final", "run", "artifact", "marker"])
@pytest.mark.parametrize("name", ["ABCDEF00-1234-1234-1234-123456789ABC",
                                   "abcdef00123412341234123456789abc", "not-a-uuid"])
def test_noncanonical_uuid_entries_are_protected_during_planning(tmp_path, kind, name):
    run_id, artifact_id = uuid4(), uuid4()
    if kind == "final":
        relative = f"refine/{name}"
    elif kind == "run":
        relative = f".artifact-staging/refine/{name}/{artifact_id}"
    elif kind == "artifact":
        relative = f".artifact-staging/refine/{run_id}/{name}"
    else:
        relative = f".artifact-staging/refine/{run_id}/.{name}.publication.json"
    if kind == "marker":
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    else:
        directory(tmp_path, relative)
    actions = build_reconcile_plan(Cursor(), tmp_path.resolve(), NOW, 0).actions
    assert len(actions) == 1
    assert actions[0].kind == "malformed_path"
    assert actions[0].protected and not actions[0].eligible
