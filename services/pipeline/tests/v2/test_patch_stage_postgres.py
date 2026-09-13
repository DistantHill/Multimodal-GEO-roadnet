"""Opt-in stage orchestration tests; all database writes use fixture-owned schema."""

import json
import os

import pytest

from v2.test_patch_ledger_postgres import (
    _CommitAckFailure, _manifest, _results, _start, _write_result, ledger_database, spool_kernel,
)
from track.v2.db.patch_ledger import CommitOutcomeUnknown, PatchLedger
from track.v2.patch_admission import ReservationBook
from track.v2.patch_coordinator import execute_registered_stage
from track.v2.patches import canonical_json
from track.v2.resource_budget import MemoryEstimates, plan_budget


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


def test_stage_state_distinguishes_ready_delayed_and_failed(ledger_database):
    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    assert ledger.stage_state().retry_after_seconds is None
    manifests = [_manifest(group=i) for i in range(2)]
    for manifest in manifests:
        ledger.register(manifest)
    assert ledger.stage_state().pending == 2
    assert ledger.stage_state().retry_after_seconds == 0
    first = ledger.claim(manifests[0].task_id)
    second = ledger.claim(manifests[1].task_id)
    ledger.fail(first, kind="transient", message="wait", retry_delay_seconds=3600)
    ledger.fail(second, kind="permanent", message="bad input")
    state = ledger.stage_state()
    assert (state.pending, state.working, state.complete, state.failed) == (1, 0, 0, 1)
    assert 3500 < state.retry_after_seconds <= 3600
    ledger.close()


def test_abandon_after_pool_stop_preserves_completed_tasks_and_attempt_cap(ledger_database):
    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    finished, interrupted = _manifest(group=1), _manifest(group=2)
    ledger.register(finished)
    ledger.register(interrupted)
    token = ledger.claim(finished.task_id)
    ledger.commit_result(token, _write_result(schema, finished.task_id))
    for number in (1, 2, 3):
        token = ledger.claim(interrupted.task_id)
        assert token.attempt_no == number
        assert ledger.abandon_working() == 1
        assert ledger.lookup(finished.task_id).status == "complete"
    assert ledger.stage_state().failed == 1
    assert ledger.claim(interrupted.task_id) is None
    assert ledger.abandon_working() == 0
    ledger.close()


def test_empty_stage_business_publication_is_atomic_and_idempotent(ledger_database):
    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    ledger.seal()

    def fail(cur):
        _write_result(schema, "publication")(cur)
        raise ValueError("publication rejected")

    with pytest.raises(ValueError, match="rejected"):
        ledger.publish(fail)
    assert not ledger.stage_state().published
    assert _results(connect(), schema) == []
    assert ledger.publish(_write_result(schema, "publication"))
    assert not ledger.publish(lambda cur: pytest.fail("duplicate publication"))
    assert _results(connect(), schema) == [("publication", 1)]
    ledger.close()


@pytest.mark.parametrize("operation", ["commit", "rollback"])
def test_publication_callback_cannot_take_over_connection_transaction(ledger_database, operation):
    import psycopg

    schema, connect = ledger_database
    conn = connect()
    ledger = PatchLedger(conn, schema=schema)
    _start(ledger)
    ledger.seal()

    def invalid(cur):
        _write_result(schema, "publication")(cur)
        getattr(conn, operation)()

    with pytest.raises(psycopg.ProgrammingError):
        ledger.publish(invalid)
    assert not ledger.stage_state().published
    assert _results(connect(), schema) == []
    ledger.close()


def test_lost_publication_acknowledgement_resumes_as_published(ledger_database):
    schema, connect = ledger_database
    proxy = _CommitAckFailure(connect())
    ledger = PatchLedger(proxy, schema=schema)
    _start(ledger)
    ledger.seal()
    proxy.armed = True
    with pytest.raises(CommitOutcomeUnknown):
        ledger.publish(_write_result(schema, "publication"))
    recovered = PatchLedger(connect(), schema=schema)
    _start(recovered)
    assert recovered.stage_state().published
    assert not recovered.publish(lambda cur: pytest.fail("already published"))
    assert _results(connect(), schema) == [("publication", 1)]
    recovered.close()


def retry_kernel(task):
    if json.loads(task.descriptor_json)["attempt_no"] == 1:
        raise TimeoutError("fixture transient error; not sent to the coordinator")
    return spool_kernel(task)


def _execution_options(ledger, schema, tmp_path, *, retry=False, memory_capacity=10):
    budget = plan_budget(10000, MemoryEstimates(100, 100, 10, 100, 100, 100),
                         available_cpu_count=2, preferred_patch_rows=2)
    book = ReservationBook(memory_capacity, 1000)

    def descriptor(manifest, attempt, allowance):
        return {"root": str(tmp_path), "run_id": manifest.run_id, "attempt_id": attempt.token,
                "attempt_no": attempt.attempt_no, "allowance": allowance}

    def write(cur, manifest, artifact, chunks):
        value = 0
        for chunk in chunks:
            value = value * (10 ** len(chunk)) + int(chunk)
        _write_result(schema, manifest.task_id, value)(cur)

    return book, dict(
        budget=budget, reservations=book, peak_bytes_per_row=10, spool_bytes_per_patch=16,
        runtime_root=tmp_path, compute_entrypoint=("v2.test_patch_stage_postgres:retry_kernel" if retry
                                                 else "v2.test_patch_ledger_postgres:spool_kernel"),
        build_descriptor=descriptor, write_result=write, retry_delay_seconds=0,
        publish_callback=_write_result(schema, "publication"),
    )


def test_real_coordinator_applies_admission_and_atomically_publishes_business(ledger_database, tmp_path):
    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    for group in range(3):
        ledger.register(_manifest(group=group))
    ledger.seal()
    book, options = _execution_options(ledger, schema, tmp_path)
    result = execute_registered_stage(ledger, **options)
    assert result.committed_this_call == 3 and result.published_now
    assert ledger.stage_state().complete == 3
    assert sorted(value for _, value in _results(connect(), schema)) == [1, 73, 73, 73]
    assert book.snapshot().active_count == 0
    assert book.snapshot().retained_spool_bytes == 6
    assert not execute_registered_stage(ledger, **options).published_now
    ledger.close()


def test_real_coordinator_retries_only_after_persistent_failure_and_new_attempt(ledger_database, tmp_path):
    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    ledger.seal()
    book, options = _execution_options(ledger, schema, tmp_path, retry=True)
    result = execute_registered_stage(ledger, **options)
    assert result.failed_calculations == 1 and result.committed_this_call == 1
    assert ledger.lookup(manifest.task_id).attempt_no == 2
    assert ledger.lookup(manifest.task_id).status == "complete"
    assert book.snapshot().retained_spool_bytes == 18
    ledger.close()


def test_real_coordinator_rejects_partial_consumption_and_rolls_back(ledger_database, tmp_path):
    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    ledger.seal()
    book, options = _execution_options(ledger, schema, tmp_path)

    def partial(cur, manifest, artifact, chunks):
        next(chunks)
        _write_result(schema, manifest.task_id)(cur)

    options["write_result"] = partial
    with pytest.raises(ValueError, match="exhaust"):
        execute_registered_stage(ledger, **options)
    assert _results(connect(), schema) == []
    assert ledger.lookup(manifest.task_id).status == "failed"
    assert not ledger.stage_state().published
    ledger.close()
