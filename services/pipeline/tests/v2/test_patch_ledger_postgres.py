"""Opt-in multi-connection ledger checks in a uniquely owned test schema.

No existing business objects are used. Unlike temporary-table tests, these
checks need visibility across connections; teardown drops only the schema
created by this fixture after closing all of its connections.
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from uuid import uuid4

import pytest

from track.v2.patches import PatchManifest, ResumeSignature, canonical_json


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


def _ddl_file() -> Path:
    explicit = os.environ.get("ROADNET_TEST_LEDGER_DDL")
    if explicit:
        return Path(explicit)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "db/postgres/migrations/004_patch_ledger.sql"
        if candidate.is_file():
            return candidate
    pytest.fail("Mount the ledger DDL and set ROADNET_TEST_LEDGER_DDL")


@pytest.fixture
def ledger_database():
    import psycopg
    from psycopg import sql
    from track.v2.db.patch_ledger import install_ledger

    schema = "patch_test_" + uuid4().hex
    connections = []
    with psycopg.connect(os.environ["ROADNET_TEST_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            install_ledger(admin, _ddl_file().read_text(), schema=schema)
            admin.execute(sql.SQL(
                "CREATE TABLE {}.test_results (task_id text PRIMARY KEY, value integer NOT NULL)"
            ).format(sql.Identifier(schema)))

            def connect():
                conn = psycopg.connect(os.environ["ROADNET_TEST_DSN"], autocommit=True)
                # All ledger and test SQL must use explicit schema names.
                conn.execute("SET search_path TO pg_catalog")
                conn.execute("SET statement_timeout TO '5s'")
                connections.append(conn)
                return conn

            yield schema, connect
        finally:
            for conn in connections:
                conn.close()
            # Identifier originates only from the fixture's random schema name.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _signature():
    return ResumeSignature("input-v1", "code-v1", "algorithm-v1", "a" * 64)


def _manifest(run_id="run-1", group=1):
    return PatchManifest(
        run_id=run_id, stage="refine", partition_kind="subtrack",
        input_descriptor_json=canonical_json({"group": group}),
        input_fingerprint="input-v1", semantic_config_digest="a" * 64,
        code_version="code-v1", algorithm_version="algorithm-v1", row_count=1,
    )


def _write_result(schema, task_id, value=1):
    from psycopg import sql

    def write(cur):
        cur.execute(sql.SQL("INSERT INTO {}.test_results VALUES (%s, %s)").format(
            sql.Identifier(schema)), (task_id, value))

    return write


def _results(conn, schema):
    from psycopg import sql
    return conn.execute(sql.SQL(
        "SELECT task_id, value FROM {}.test_results ORDER BY task_id"
    ).format(sql.Identifier(schema))).fetchall()


def _start(ledger, run_id="run-1", *, signature=None, execution=None):
    return ledger.start(
        run_id, "refine", signature or _signature(),
        execution_config=execution or {"executor_processes": 1},
        environment={"test_environment": True},
    )


def test_atomic_result_visibility_duplicate_delivery_and_sealed_publication(ledger_database):
    from track.v2.db.patch_ledger import LedgerError, PatchLedger, StaleAttempt

    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    observer = connect()
    _start(ledger)
    manifest = _manifest()
    assert ledger.register(manifest)
    assert not ledger.register(manifest)
    with pytest.raises(LedgerError, match="sealed"):
        ledger.publish()
    ledger.seal()
    assert not ledger.register(manifest)
    with pytest.raises(LedgerError, match="sealed"):
        ledger.register(_manifest(group=2))
    with pytest.raises(LedgerError, match="incomplete"):
        ledger.publish()
    attempt = ledger.claim(manifest.task_id)
    assert attempt.attempt_no == 1
    assert ledger.claim(manifest.task_id) is None

    def write(cur):
        _write_result(schema, manifest.task_id, 42)(cur)
        # Even after an INSERT, the independent observer sees no partial result.
        assert _results(observer, schema) == []

    assert ledger.commit_result(attempt, write)
    assert ledger.lookup(manifest.task_id).status == "complete"
    assert _results(observer, schema) == [(manifest.task_id, 42)]
    assert not ledger.commit_result(attempt, lambda cur: pytest.fail("duplicate callback"))
    with pytest.raises(StaleAttempt):
        ledger.commit_result(replace(attempt, token=str(uuid4())), write)
    assert ledger.publish()
    assert not ledger.publish()
    assert ledger.claim(manifest.task_id) is None
    ledger.close()


def test_callback_failure_rolls_back_every_business_write(ledger_database):
    from track.v2.db.patch_ledger import PatchLedger

    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    observer = connect()
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    attempt = ledger.claim(manifest.task_id)

    def fail_after_insert(cur):
        _write_result(schema, manifest.task_id)(cur)
        raise ValueError("calculation output rejected")

    with pytest.raises(ValueError, match="output rejected"):
        ledger.commit_result(attempt, fail_after_insert)
    assert _results(observer, schema) == []
    assert ledger.lookup(manifest.task_id).status == "working"
    assert ledger.fail(attempt, kind="transient", message="fixture", retry_delay_seconds=0) == "pending"
    retry = ledger.claim(manifest.task_id)
    assert retry.attempt_no == 2
    assert retry.token != attempt.token
    assert ledger.commit_result(retry, _write_result(schema, manifest.task_id))
    ledger.close()


def test_run_lock_covers_other_connections_and_stages(ledger_database):
    from track.v2.db.patch_ledger import PatchLedger, RunLocked

    schema, connect = ledger_database
    first = PatchLedger(connect(), schema=schema)
    second = PatchLedger(connect(), schema=schema)
    _start(first)
    with pytest.raises(RunLocked):
        _start(second)
    with pytest.raises(RunLocked):
        second.start("run-1", "detect", _signature(), execution_config={}, environment={})
    # A genuinely different run is independent, not blocked by a global lock.
    assert _start(second, "run-2") == 1
    second.close()
    first.close()
    assert _start(second) == 2
    second.close()


def test_disconnect_resume_retains_complete_tasks_and_rejects_old_generation(ledger_database):
    from psycopg import sql
    from track.v2.db.patch_ledger import PatchLedger, StaleAttempt

    schema, connect = ledger_database
    first_conn = connect()
    first = PatchLedger(first_conn, schema=schema)
    _start(first)
    completed, interrupted = _manifest(group=1), _manifest(group=2)
    for manifest in (completed, interrupted):
        first.register(manifest)
    first.seal()
    completed_attempt = first.claim(completed.task_id)
    first.commit_result(completed_attempt, _write_result(schema, completed.task_id))
    old_attempt = first.claim(interrupted.task_id)
    first_conn.close()  # Simulated coordinator crash releases its session lock.
    second = PatchLedger(connect(), schema=schema)
    assert _start(second, execution={"executor_processes": 2}) == 2
    assert second.lookup(completed.task_id).status == "complete"
    assert second.claim(completed.task_id) is None
    assert second.lookup(interrupted.task_id).status == "pending"
    retry = second.claim(interrupted.task_id)
    assert retry.attempt_no == 2
    assert retry.generation == 2
    with pytest.raises(StaleAttempt):
        second.commit_result(old_attempt, lambda cur: pytest.fail("stale callback"))
    assert second.commit_result(retry, _write_result(schema, interrupted.task_id))
    assert second.publish()
    observer = connect()
    history = observer.execute(sql.SQL(
        "SELECT generation, execution_config FROM {}.pipeline_patch_executions "
        "WHERE run_id='run-1' AND stage='refine' ORDER BY generation"
    ).format(sql.Identifier(schema))).fetchall()
    assert history == [(1, {"executor_processes": 1}), (2, {"executor_processes": 2})]
    attempts = observer.execute(sql.SQL(
        "SELECT attempt_no, status FROM {}.pipeline_patch_attempts "
        "WHERE task_id=%s ORDER BY attempt_no"
    ).format(sql.Identifier(schema)), (interrupted.task_id,)).fetchall()
    assert attempts == [(1, "abandoned"), (2, "complete")]
    second.close()


@pytest.mark.parametrize("field,value", [
    ("input_fingerprint", "other-input"), ("code_version", "other-code"),
    ("algorithm_version", "other-algorithm"), ("semantic_config_digest", "b" * 64),
])
def test_changed_resume_signature_is_rejected_without_retaining_lock(ledger_database, field, value):
    from track.v2.db.patch_ledger import LedgerError, PatchLedger

    schema, connect = ledger_database
    first = PatchLedger(connect(), schema=schema)
    _start(first)
    first.close()
    changed = PatchLedger(connect(), schema=schema)
    with pytest.raises(LedgerError, match="signature"):
        _start(changed, signature=replace(_signature(), **{field: value}))
    original = PatchLedger(connect(), schema=schema)
    assert _start(original) == 2
    original.close()


def test_three_attempt_cap_survives_restarts_and_prevents_publication(ledger_database):
    from track.v2.db.patch_ledger import LedgerError, PatchLedger

    schema, connect = ledger_database
    manifest = _manifest()
    for number in (1, 2, 3):
        ledger = PatchLedger(connect(), schema=schema)
        _start(ledger)
        ledger.register(manifest)
        ledger.seal()
        attempt = ledger.claim(manifest.task_id)
        assert attempt.attempt_no == number
        status = ledger.fail(attempt, kind="transient", message="test failure", retry_delay_seconds=0)
        assert status == ("pending" if number < 3 else "failed")
        ledger.close()
    resumed = PatchLedger(connect(), schema=schema)
    _start(resumed)
    assert resumed.lookup(manifest.task_id).status == "failed"
    assert resumed.claim(manifest.task_id) is None
    with pytest.raises(LedgerError, match="incomplete"):
        resumed.publish()
    resumed.close()


@pytest.mark.parametrize("kind", ["resource", "permanent"])
def test_resource_and_permanent_failures_are_not_blindly_retried(ledger_database, kind):
    from track.v2.db.patch_ledger import PatchLedger

    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    attempt = ledger.claim(manifest.task_id)
    assert ledger.fail(attempt, kind=kind, message="stop unchanged conditions") == "failed"
    assert ledger.claim(manifest.task_id) is None
    ledger.close()


def test_retry_delay_is_durable_without_sleeping(ledger_database):
    from track.v2.db.patch_ledger import PatchLedger

    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    attempt = ledger.claim(manifest.task_id)
    ledger.fail(attempt, kind="transient", message="wait", retry_delay_seconds=3600)
    assert ledger.claim(manifest.task_id) is None
    ledger.close()
    resumed = PatchLedger(connect(), schema=schema)
    _start(resumed)
    assert resumed.claim(manifest.task_id) is None
    resumed.close()


def test_connection_that_lost_its_lock_cannot_submit(ledger_database):
    from track.v2.db.patch_ledger import PatchLedger, StaleAttempt

    schema, connect = ledger_database
    conn = connect()
    ledger = PatchLedger(conn, schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    attempt = ledger.claim(manifest.task_id)
    conn.execute("SELECT pg_advisory_unlock_all()")
    with pytest.raises(StaleAttempt):
        ledger.commit_result(attempt, lambda cur: pytest.fail("unowned callback"))
    assert _results(connect(), schema) == []
    ledger.close()


class _CommitAckFailure:
    """Forward real SQL; once armed, lose only the acknowledgement after COMMIT."""

    def __init__(self, connection):
        self.connection = connection
        self.armed = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def transaction(self):
        from contextlib import contextmanager
        import psycopg

        @contextmanager
        def transaction():
            with self.connection.transaction():
                yield
            if self.armed:
                self.armed = False
                raise psycopg.OperationalError("injected lost commit acknowledgement")

        return transaction()


def test_lost_commit_acknowledgement_is_resolved_by_durable_lookup(ledger_database):
    from track.v2.db.patch_ledger import CommitOutcomeUnknown, PatchLedger

    schema, connect = ledger_database
    proxy = _CommitAckFailure(connect())
    ledger = PatchLedger(proxy, schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    attempt = ledger.claim(manifest.task_id)
    proxy.armed = True
    with pytest.raises(CommitOutcomeUnknown):
        ledger.commit_result(attempt, _write_result(schema, manifest.task_id, 73))
    assert proxy.closed
    recovered = PatchLedger(connect(), schema=schema)
    _start(recovered)
    assert recovered.lookup(manifest.task_id).status == "complete"
    assert recovered.claim(manifest.task_id) is None
    assert _results(connect(), schema) == [(manifest.task_id, 73)]
    recovered.close()


def test_corrupt_spool_does_not_commit_provisional_rows(ledger_database, tmp_path):
    from track.v2.db.patch_ledger import PatchLedger
    from track.v2.patch_spool import ArtifactIntegrityError, iter_artifact_chunks, write_patch_artifact

    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    manifest = _manifest()
    ledger.register(manifest)
    attempt = ledger.claim(manifest.task_id)
    artifact = write_patch_artifact(
        tmp_path, run_id=manifest.run_id, task_id=manifest.task_id,
        attempt_id=attempt.token, chunks=[b"1"], attempt_byte_limit=1,
    )
    bad_descriptor = replace(artifact, sha256="0" * 64)

    def write(cur):
        for chunk in iter_artifact_chunks(tmp_path, bad_descriptor, chunk_size=1):
            _write_result(schema, manifest.task_id, int(chunk))(cur)
        pytest.fail("corrupt descriptor must fail before commit")

    with pytest.raises(ArtifactIntegrityError):
        ledger.commit_result(attempt, write)
    assert _results(connect(), schema) == []
    assert ledger.lookup(manifest.task_id).status == "working"
    ledger.close()


def test_resume_discovers_fixed_pending_manifests_without_replanning(ledger_database):
    from track.v2.db.patch_ledger import PatchLedger

    schema, connect = ledger_database
    ledger = PatchLedger(connect(), schema=schema)
    _start(ledger)
    manifests = sorted((_manifest(group=i) for i in range(4)), key=lambda m: m.task_id)
    for manifest in manifests:
        ledger.register(manifest)
    ledger.seal()
    assert ledger.pending_manifests(limit=2) == manifests[:2]
    complete = ledger.claim(manifests[0].task_id)
    ledger.commit_result(complete, _write_result(schema, complete.task_id))
    delayed = ledger.claim(manifests[1].task_id)
    ledger.fail(delayed, kind="transient", message="backoff", retry_delay_seconds=3600)
    ledger.claim(manifests[2].task_id)  # Interrupted calculation.
    ledger.close()
    resumed = PatchLedger(connect(), schema=schema)
    _start(resumed, execution={"patch_target_rows": 999})
    assert resumed.pending_manifests(limit=1) == manifests[2:3]
    assert resumed.pending_manifests(limit=10) == manifests[2:]
    # Discovery is read-only: claims alone consume attempts.
    assert resumed.lookup(manifests[2].task_id).attempt_no == 1
    resumed.close()


class _LockAckFailure:
    """Execute real lock acquisition, then lose its first acknowledgement."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def cursor(self):
        import psycopg
        cursor = self.connection.cursor()

        class Cursor:
            lock_query = False

            def __enter__(self):
                cursor.__enter__()
                return self

            def __exit__(self, *args):
                return cursor.__exit__(*args)

            def execute(self, query, params=()):
                self.lock_query = query == "SELECT pg_try_advisory_lock(%s)"
                cursor.execute(query, params)
                return self

            def fetchone(self):
                row = cursor.fetchone()
                if self.lock_query:
                    assert row == (True,)
                    raise psycopg.OperationalError("injected lost lock acknowledgement")
                return row

        return Cursor()


def test_lost_lock_acknowledgement_closes_session_and_allows_recovery(ledger_database):
    import psycopg
    from track.v2.db.patch_ledger import PatchLedger

    schema, connect = ledger_database
    proxy = _LockAckFailure(connect())
    ledger = PatchLedger(proxy, schema=schema)
    with pytest.raises(psycopg.OperationalError, match="lock acknowledgement"):
        _start(ledger)
    assert proxy.closed
    recovered = PatchLedger(connect(), schema=schema)
    assert _start(recovered) == 1
    recovered.close()
