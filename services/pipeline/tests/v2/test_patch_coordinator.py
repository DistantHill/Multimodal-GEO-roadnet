"""Coordinator state/admission checks; PostgreSQL atomicity is tested separately."""

from dataclasses import asdict
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from track.v2.db.patch_ledger import AttemptToken, CommitOutcomeUnknown
from track.v2.patch_admission import ReservationBook, ResourceUnavailable
from track.v2.patch_coordinator import StageStopped, execute_registered_stage
from track.v2.patch_pool import PatchExecutionError, PoolShutdownUnconfirmed, RemotePatchFailure
from track.v2.patch_spool import write_patch_artifact
from track.v2.patches import PatchManifest, canonical_json
from track.v2.resource_budget import MemoryEstimates, plan_budget


class Ledger:
    def __init__(self, count=2):
        self.rows = {}
        self.sealed = True
        self.published = False
        self.abandoned = 0
        self.unknown_commit = False
        for index in range(count):
            manifest = PatchManifest("run", "clean", "subtrack", canonical_json({"group": index}),
                                     "input", "a" * 64, "code", "algorithm", 1)
            self.rows[manifest.task_id] = [manifest, "pending", 0]

    def stage_state(self):
        counts = {status: sum(row[1] == status for row in self.rows.values())
                  for status in ("pending", "working", "complete", "failed")}
        return SimpleNamespace(sealed=self.sealed, published=self.published,
                               retry_after_seconds=0, **counts)

    def pending_manifests(self, *, limit):
        return [row[0] for row in self.rows.values() if row[1] == "pending"][:limit]

    def claim(self, task_id):
        row = self.rows[task_id]
        assert row[1] == "pending"
        row[1], row[2] = "working", row[2] + 1
        return AttemptToken("run", "clean", task_id, 1, row[2], str(uuid4()))

    def commit_result(self, attempt, write):
        if self.unknown_commit:
            raise CommitOutcomeUnknown("injected")
        write(None)
        self.rows[attempt.task_id][1] = "complete"
        return True

    def fail(self, attempt, *, kind, message, retry_delay_seconds=0):
        row = self.rows[attempt.task_id]
        row[1] = "pending" if kind == "transient" and row[2] < 3 else "failed"
        return row[1]

    def abandon_working(self, *, message):
        self.abandoned += 1
        for row in self.rows.values():
            if row[1] == "working":
                row[1] = "pending" if row[2] < 3 else "failed"

    def publish(self, callback=None):
        assert not self.stage_state().pending
        if callback:
            callback(None)
        self.published = True
        return True


def budget():
    return plan_budget(10_000, MemoryEstimates(100, 100, 10, 100, 100, 100),
                       available_cpu_count=2, preferred_patch_rows=2)


def setup_execution(tmp_path, *, count=2, spool_limit=1000):
    ledger = Ledger(count)
    book = ReservationBook(100, spool_limit)
    calls = []

    def descriptor(manifest, attempt, limit):
        return {"attempt_id": attempt.token, "allowance": limit}

    def inline_pool(tasks, *, consume, **kwargs):
        calls.append(kwargs)
        count = 0
        for task in tasks:
            params = json.loads(task.descriptor_json)
            artifact = write_patch_artifact(
                tmp_path, run_id="run", task_id=task.task_id, attempt_id=params["attempt_id"],
                chunks=[b"73"], attempt_byte_limit=params["allowance"],
            )
            consume(task, canonical_json({"artifact": asdict(artifact)}))
            count += 1
        return count

    def write(cur, manifest, artifact, chunks):
        for chunk in chunks:
            assert chunk == b"73"

    options = dict(budget=budget(), reservations=book, peak_bytes_per_row=10,
                   spool_bytes_per_patch=16, runtime_root=tmp_path,
                   compute_entrypoint="unused:kernel", build_descriptor=descriptor,
                   write_result=write, retry_delay_seconds=0, run_pool=inline_pool)
    return ledger, book, options, calls


def test_fixed_stage_commits_and_retains_artifact_charge(tmp_path):
    ledger, book, options, calls = setup_execution(tmp_path)
    result = execute_registered_stage(ledger, **options)
    assert result.committed_this_call == 2 and result.published_now
    assert len(calls) == 1
    assert book.snapshot().active_count == 0
    assert book.snapshot().retained_spool_bytes == 4
    assert not execute_registered_stage(ledger, **options).published_now
    assert len(calls) == 1


def test_transient_failure_retries_with_new_attempt_and_persistent_cap(tmp_path):
    ledger, book, options, calls = setup_execution(tmp_path, count=1)
    normal_pool = options["run_pool"]
    attempts = []

    def fail_twice(tasks, **kwargs):
        if len(attempts) < 2:
            task = next(iter(tasks))
            attempts.append(json.loads(task.descriptor_json)["attempt_id"])
            raise PatchExecutionError(task, RemotePatchFailure('{"error_type":"TimeoutError"}'))
        return normal_pool(tasks, **kwargs)

    options["run_pool"] = fail_twice
    result = execute_registered_stage(ledger, **options)
    assert result.failed_calculations == 2
    assert result.committed_this_call == 1
    assert len(set(attempts)) == 2
    assert next(iter(ledger.rows.values()))[2] == 3
    assert book.snapshot().retained_spool_bytes == 34  # Unknown failed files stay charged.


@pytest.mark.parametrize("error_type,tries", [("TimeoutError", 3), ("MemoryError", 1), ("ValueError", 1)])
def test_failed_calculations_stop_without_publication(tmp_path, error_type, tries):
    ledger, book, options, calls = setup_execution(tmp_path, count=1)

    def fail(tasks, **kwargs):
        task = next(iter(tasks))
        raise PatchExecutionError(task, RemotePatchFailure(canonical_json({"error_type": error_type})))

    options["run_pool"] = fail
    with pytest.raises(PatchExecutionError):
        execute_registered_stage(ledger, **options)
    assert not ledger.published
    assert next(iter(ledger.rows.values()))[1:] == ["failed", tries]
    assert book.snapshot().active_count == 0
    assert book.snapshot().retained_spool_bytes == tries * 16


def test_insufficient_spool_stops_before_claiming_next_patch(tmp_path):
    ledger, book, options, calls = setup_execution(tmp_path, spool_limit=17)
    with pytest.raises(ResourceUnavailable):
        execute_registered_stage(ledger, **options)
    assert ledger.stage_state().complete == 1
    assert ledger.stage_state().pending == 1
    assert not ledger.published
    assert book.snapshot().retained_spool_bytes == 2
    assert sum(row[2] for row in ledger.rows.values()) == 1


def test_unexhausted_reader_is_not_marked_complete(tmp_path):
    ledger, book, options, calls = setup_execution(tmp_path, count=1)
    options["write_result"] = lambda cur, manifest, artifact, chunks: None
    with pytest.raises(ValueError, match="exhaust"):
        execute_registered_stage(ledger, **options)
    assert ledger.stage_state().failed == 1
    assert not ledger.published


def test_unknown_commit_stops_without_reclassifying_or_abandoning(tmp_path):
    ledger, book, options, calls = setup_execution(tmp_path, count=1)
    ledger.unknown_commit = True
    with pytest.raises(CommitOutcomeUnknown):
        execute_registered_stage(ledger, **options)
    assert ledger.abandoned == 0
    assert ledger.stage_state().working == 1
    assert book.snapshot().active_count == 0
    assert book.snapshot().retained_spool_bytes == 16


def test_unconfirmed_shutdown_keeps_reservations_and_working_state(tmp_path):
    ledger, book, options, calls = setup_execution(tmp_path, count=1)
    executor = object()

    def live_pool(tasks, **kwargs):
        next(iter(tasks))
        raise PoolShutdownUnconfirmed(executor)

    options["run_pool"] = live_pool
    with pytest.raises(PoolShutdownUnconfirmed) as raised:
        execute_registered_stage(ledger, **options)
    assert raised.value.executor is executor
    assert ledger.abandoned == 0
    assert ledger.stage_state().working == 1
    assert book.snapshot().active_count == 1
    assert book.snapshot().reserved_memory_bytes == 10
    assert book.snapshot().reserved_spool_bytes == 16


@pytest.mark.parametrize("field", ["sealed", "failed", "working"])
def test_invalid_initial_stage_does_not_dispatch(tmp_path, field):
    ledger, book, options, calls = setup_execution(tmp_path)
    if field == "sealed":
        ledger.sealed = False
    else:
        next(iter(ledger.rows.values()))[1] = field
    with pytest.raises(StageStopped):
        execute_registered_stage(ledger, **options)
    assert not calls


@pytest.mark.parametrize("name,value", [
    ("peak_bytes_per_row", True), ("spool_bytes_per_patch", 0),
    ("retry_delay_seconds", float("nan")), ("runtime_root", "/"),
])
def test_invalid_runtime_inputs_are_rejected(tmp_path, name, value):
    ledger, book, options, calls = setup_execution(tmp_path)
    options[name] = value
    with pytest.raises(ValueError):
        execute_registered_stage(ledger, **options)
    assert not calls
