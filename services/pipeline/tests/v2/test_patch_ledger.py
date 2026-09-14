"""State/precondition tests; actual transaction guarantees are tested on PostgreSQL."""

from collections import deque
from concurrent.futures import Future
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from track.v2.db.patch_ledger import (
    AttemptToken, CommitOutcomeUnknown, LedgerError, PatchLedger, RunLocked, StageState, StaleAttempt,
    install_ledger,
)
from track.v2.patches import PatchManifest, ResumeSignature


class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        query = query.as_string() if hasattr(query, "as_string") else query
        self.conn.statements.append((query, params))
        return self

    def fetchone(self):
        return self.conn.rows.popleft()

    def fetchmany(self, size):
        self.conn.fetch_sizes.append(size)
        return [self.conn.rows.popleft() for _ in range(min(size, len(self.conn.rows)))]

    @property
    def rowcount(self):
        return self.conn.rowcount


class Transaction:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.transactions += 1
        self.conn.info.transaction_status = TransactionStatus.INTRANS

    def __exit__(self, kind, value, tb):
        self.conn.info.transaction_status = TransactionStatus.IDLE
        if kind is not None:
            self.conn.rollbacks += 1
        elif self.conn.commit_error:
            raise self.conn.commit_error
        else:
            self.conn.commits += 1
        return False


class Connection:
    def __init__(self, rows=(), *, autocommit=True, status=TransactionStatus.IDLE):
        self.autocommit = autocommit
        self.info = SimpleNamespace(transaction_status=status)
        self.rows = deque(rows)
        self.statements = []
        self.transactions = self.commits = self.rollbacks = 0
        self.commit_error = None
        self.closed = False
        self.fetch_sizes = []
        self.rowcount = 0

    def cursor(self):
        return Cursor(self)

    def transaction(self):
        return Transaction(self)

    def execute(self, query, params=()):
        return self.cursor().execute(query, params)

    def close(self):
        self.closed = True


SIGNATURE = ResumeSignature("input-v1", "code-v1", "algorithm-v1", "a" * 64)
MANIFEST = PatchManifest("run", "refine", "subtrack", '{"group":1}',
                         SIGNATURE.input_fingerprint, SIGNATURE.semantic_config_digest,
                         SIGNATURE.code_version, SIGNATURE.algorithm_version, 5)
TOKEN = AttemptToken("run", "refine", MANIFEST.task_id, 1, 1, "attempt-1")


def active(rows=()):
    conn = Connection(rows)
    ledger = PatchLedger(conn, schema='isolated"schema')
    ledger.run_id, ledger.stage, ledger.generation = "run", "refine", 1
    ledger.signature, ledger._lock_key = SIGNATURE, 42
    return ledger, conn


@pytest.mark.parametrize("autocommit", [False, None, 1])
def test_rejects_non_autocommit_connections(autocommit):
    with pytest.raises(ValueError, match="autocommit=True"):
        PatchLedger(Connection(autocommit=autocommit))


@pytest.mark.parametrize("status", [TransactionStatus.INTRANS, TransactionStatus.INERROR,
                                  TransactionStatus.ACTIVE, TransactionStatus.UNKNOWN])
def test_rejects_non_idle_connections(status):
    with pytest.raises(ValueError, match="idle"):
        PatchLedger(Connection(status=status))


@pytest.mark.parametrize("schema", ["", " ", None, "bad\x00schema"])
def test_invalid_schema_rejected_before_sql(schema):
    conn = Connection()
    with pytest.raises(ValueError):
        PatchLedger(conn, schema=schema)
    assert not conn.statements


def test_installer_quotes_schema_and_owns_transaction():
    conn = Connection()
    install_ledger(conn, "CREATE TABLE {schema}.example (id int)", schema='a"b')
    assert conn.statements == [('CREATE TABLE "a""b".example (id int)', ())]
    assert conn.commits == 1


def test_start_records_generation_and_sanitized_supplied_metadata():
    conn = Connection([(False,), (True,), None])
    ledger = PatchLedger(conn)
    assert ledger.start("run", "refine", SIGNATURE,
                        execution_config={"n": 10}, environment={"cpu": "unknown"}) == 1
    inserts = [params for query, params in conn.statements if "pipeline_patch_executions" in query]
    assert inserts == [("run", "refine", 1, '{"n":10}', '{"cpu":"unknown"}')]
    assert conn.commits == 1
    with pytest.raises(LedgerError, match="Close"):
        ledger.start("other", "refine", SIGNATURE, execution_config={}, environment={})


@pytest.mark.parametrize("field,value", [("generation", True), ("generation", 0),
                                        ("attempt_no", 4), ("attempt_no", 0),
                                        ("token", ""), ("task_id", None)])
def test_token_validation(field, value):
    with pytest.raises(ValueError):
        replace(TOKEN, **{field: value})


@pytest.mark.parametrize("manifest", [replace(MANIFEST, parent_id="parent"),
                                       replace(MANIFEST, run_id="other"),
                                       replace(MANIFEST, code_version="changed")])
def test_invalid_manifest_never_opens_transaction(manifest):
    ledger, conn = active()
    with pytest.raises(ValueError):
        ledger.register(manifest)
    assert conn.transactions == 0


def test_register_is_idempotent_after_seal():
    ledger, conn = active([(True,), (1, True, False), (MANIFEST.to_dict(),)])
    assert ledger.register(MANIFEST) is False
    assert not any(query.startswith("INSERT") for query, _ in conn.statements)


def test_sealed_registration_rejects_new_task():
    ledger, conn = active([(True,), (1, True, False), None])
    with pytest.raises(LedgerError, match="sealed"):
        ledger.register(MANIFEST)
    assert conn.rollbacks == 1


@pytest.mark.parametrize("rows", [[(False,)], [(True,), (2, False, False)]])
def test_lost_lock_or_fenced_generation_blocks_write(rows):
    ledger, conn = active(rows)
    with pytest.raises(StaleAttempt):
        ledger.seal()
    assert not any(query.startswith("UPDATE") for query, _ in conn.statements)


def test_claim_persists_token_and_bounded_retry_predicate():
    ledger, conn = active([(True,), (1, False, False), (2,)])
    token = ledger.claim(MANIFEST.task_id)
    assert token.attempt_no == 2
    assert token.generation == 1
    update = next(query for query, _ in conn.statements if query.startswith("UPDATE"))
    assert "attempt_no < 3" in update
    assert "next_retry_at <= clock_timestamp()" in update
    assert conn.commits == 1


def test_result_callback_and_completion_share_one_transaction():
    ledger, conn = active([(True,), (1, False, False), ("working", 1, TOKEN.token, 1)])
    assert ledger.commit_result(TOKEN, lambda cur: cur.conn.statements.append(("BUSINESS", ())))
    assert conn.transactions == conn.commits == 1
    assert [query for query, _ in conn.statements][-3].startswith("BUSINESS")
    assert "status = 'complete'" in conn.statements[-2][0]


def test_completed_duplicate_never_runs_callback():
    ledger, _ = active([(True,), (1, True, True), ("complete", 1, None, 1), (TOKEN.token,)])
    def unexpected(cur):
        pytest.fail("Completed task callback repeated")
    assert ledger.commit_result(TOKEN, unexpected) is False


def test_completed_task_still_requires_exact_token():
    ledger, _ = active([(True,), (1, True, True), ("complete", 1, None, 1), ("different",)])
    with pytest.raises(StaleAttempt, match="token"):
        ledger.commit_result(TOKEN, lambda cur: None)


def test_callback_exception_rolls_back_without_unknown_outcome():
    ledger, conn = active([(True,), (1, False, False), ("working", 1, TOKEN.token, 1)])
    def failed(cur):
        raise ArithmeticError("deterministic failure")
    with pytest.raises(ArithmeticError):
        ledger.commit_result(TOKEN, failed)
    assert conn.rollbacks == 1
    assert not conn.closed
    assert not any("status = 'complete'" in query for query, _ in conn.statements)


@pytest.mark.parametrize("result", [Future(), 42, {}])
def test_async_or_unexpected_callback_result_rejected(result):
    ledger, conn = active([(True,), (1, False, False), ("working", 1, TOKEN.token, 1)])
    with pytest.raises(ValueError, match="synchronously"):
        ledger.commit_result(TOKEN, lambda cur: result)
    assert conn.rollbacks == 1


def test_coroutine_callback_is_closed_without_execution():
    ledger, conn = active([(True,), (1, False, False), ("working", 1, TOKEN.token, 1)])
    async def asynchronous(cur):
        pytest.fail("Coroutine must not execute")
    with pytest.raises(ValueError, match="synchronously"):
        ledger.commit_result(TOKEN, asynchronous)
    assert conn.rollbacks == 1


def test_commit_acknowledgement_failure_requires_reconnect_and_lookup():
    ledger, conn = active([(True,), (1, False, False), ("working", 1, TOKEN.token, 1)])
    conn.commit_error = OSError("acknowledgement lost")
    callbacks = []
    with pytest.raises(CommitOutcomeUnknown, match="Inspect durable task"):
        ledger.commit_result(TOKEN, lambda cur: callbacks.append("called"))
    assert callbacks == ["called"]
    assert conn.closed


@pytest.mark.parametrize("delay", [-1, 3601, float("nan"), float("inf"), True, "1"])
def test_retry_delay_rejected_before_mutation(delay):
    ledger, conn = active()
    with pytest.raises(ValueError, match="retry_delay"):
        ledger.fail(TOKEN, kind="transient", message="safe", retry_delay_seconds=delay)
    assert not conn.statements


@pytest.mark.parametrize("kind,number,status", [("transient", 1, "pending"),
                                             ("transient", 3, "failed"),
                                             ("resource", 1, "failed"),
                                             ("permanent", 1, "failed")])
def test_retry_classification_and_attempt_limit(kind, number, status):
    token = replace(TOKEN, attempt_no=number)
    ledger, conn = active([(True,), (1, False, False), ("working", number, TOKEN.token, 1)])
    assert ledger.fail(token, kind=kind, message="safe", retry_delay_seconds=0) == status
    assert conn.commits == 1
    update = next(params for query, params in conn.statements if query.startswith("UPDATE"))
    assert update[0] == status
    assert update[1] == (0 if status == "pending" else None)


@pytest.mark.parametrize("rows,error", [([(True,), (1, False, False)], "sealed"),
                                      ([(True,), (1, True, False), (True,)], "incomplete")])
def test_publish_rejects_unsealed_or_incomplete_stage(rows, error):
    ledger, conn = active(rows)
    with pytest.raises(LedgerError, match=error):
        ledger.publish()
    assert conn.rollbacks == 1


def test_lookup_is_one_bounded_query_and_uses_quoted_schema():
    ledger, conn = active([(MANIFEST.task_id, "complete", 1, 1)])
    state = ledger.lookup(MANIFEST.task_id)
    assert state.status == "complete"
    assert len(conn.statements) == 1
    assert '"isolated""schema".pipeline_patch_tasks' in conn.statements[0][0]
    assert conn.statements[0][1] == ("run", "refine", MANIFEST.task_id)


def test_close_healthy_connection_releases_only_our_lock():
    ledger, conn = active()
    ledger.close()
    ledger.close()
    assert conn.statements == [("SELECT pg_advisory_unlock(%s)", (42,))]
    assert not conn.closed
    with pytest.raises(LedgerError, match="No active run"):
        ledger.lookup(MANIFEST.task_id)


def test_close_nonidle_connection_closes_session_to_avoid_leaked_lock():
    ledger, conn = active()
    conn.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(ValueError, match="idle"):
        ledger.close()
    assert conn.closed
    assert ledger._lock_key is None


@pytest.mark.parametrize("limit", [0, -1, 10_001, True, 1.5, "100", None])
def test_pending_manifest_page_rejects_unbounded_or_invalid_limit(limit):
    ledger, conn = active()
    with pytest.raises(ValueError, match="limit"):
        ledger.pending_manifests(limit=limit)
    assert not conn.statements


def test_pending_manifest_page_restores_fixed_manifest_with_bounded_fetch():
    ledger, conn = active([(True,), (1, True, False), (MANIFEST.to_dict(),)])
    assert ledger.pending_manifests(limit=2) == [MANIFEST]
    assert conn.fetch_sizes == [2]
    query, params = conn.statements[-1]
    assert "status = 'pending'" in query
    assert "next_retry_at <= clock_timestamp()" in query
    assert "ORDER BY task_id LIMIT %s" in query
    assert params == ("run", "refine", 2)
    assert not any(query.startswith(("UPDATE", "INSERT")) for query, _ in conn.statements)


def test_pending_manifest_page_requires_current_run_lock():
    ledger, conn = active([(False,)])
    with pytest.raises(StaleAttempt):
        ledger.pending_manifests()
    assert not conn.fetch_sizes


def test_pending_manifest_page_requires_start():
    conn = Connection()
    with pytest.raises(LedgerError, match="No active run"):
        PatchLedger(conn).pending_manifests()
    assert not conn.fetch_sizes


@pytest.mark.parametrize("failure_at", ["execute", "fetchone", "exit"])
def test_start_closes_session_when_lock_acquisition_acknowledgement_fails(failure_at):
    conn = Connection([(False,), (True,)])
    class FailedCursor(Cursor):
        acquired = False

        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "pg_try_advisory_lock" in str(query):
                self.acquired = True
                if failure_at == "execute":
                    raise OSError("lock acknowledgement lost")
            return result

        def fetchone(self):
            if self.acquired and failure_at == "fetchone":
                raise OSError("lock acknowledgement lost")
            return super().fetchone()

        def __exit__(self, *args):
            if self.acquired and failure_at == "exit":
                raise OSError("lock cursor close failed")
            return False

    conn.cursor = lambda: FailedCursor(conn)
    ledger = PatchLedger(conn)
    with pytest.raises(OSError):
        ledger.start("run", "refine", SIGNATURE, execution_config={}, environment={})
    assert conn.closed
    assert ledger._lock_key is None
    assert ledger.run_id is None
    assert not conn.transactions


@pytest.mark.parametrize("rows", [[(True,)], [(False,), (False,)]])
def test_start_known_lock_refusal_preserves_existing_owner_and_connection(rows):
    conn = Connection(rows)
    ledger = PatchLedger(conn)
    with pytest.raises(RunLocked):
        ledger.start("run", "refine", SIGNATURE, execution_config={}, environment={})
    assert not conn.closed
    assert ledger._lock_key is None
    assert not any("pg_advisory_unlock" in query for query, _ in conn.statements)


@pytest.mark.parametrize("counts", [(0, 0, 0, 0, None), (2, 1, 5, 3, 0),
                                   (1, 0, 9, 0, 2.25)])
def test_stage_state_is_one_guarded_bounded_aggregate(counts):
    ledger, conn = active([(True,), (1, True, False), counts])
    state = ledger.stage_state()
    assert state == StageState(True, False, *counts)
    assert conn.transactions == conn.commits == 1
    assert len(conn.statements) == 3
    query, params = conn.statements[-1]
    assert "count(*) FILTER" in query
    assert "statement_timestamp()" in query
    assert "manifest" not in query
    assert params == ("run", "refine")
    assert not conn.fetch_sizes
    with pytest.raises(FrozenInstanceError):
        state.pending = 9


@pytest.mark.parametrize("overrides", [
    {"sealed": 1}, {"published": "false"}, {"pending": -1}, {"working": True},
    {"complete": 1.0}, {"failed": "2"}, {"retry_after_seconds": 0},
    {"pending": 1}, {"pending": 1, "retry_after_seconds": float("nan")},
    {"pending": 1, "retry_after_seconds": float("inf")},
    {"pending": 1, "retry_after_seconds": -0.1},
    {"pending": 1, "retry_after_seconds": True}, {"published": True},
    {"published": True, "sealed": True, "working": 1},
    {"published": True, "sealed": True, "failed": 1},
])
def test_stage_state_validates_database_summary(overrides):
    with pytest.raises(ValueError):
        replace(StageState(False, False, 0, 0, 0, 0, None), **overrides)


@pytest.mark.parametrize("method", ["stage_state", "abandon_working", "publish"])
@pytest.mark.parametrize("rows", [[(False,)], [(True,), (2, False, False)]])
def test_new_stage_apis_require_current_lock_and_generation(method, rows):
    ledger, conn = active(rows)
    with pytest.raises(StaleAttempt):
        getattr(ledger, method)()
    assert conn.rollbacks == 1
    assert not any(query.startswith("UPDATE") for query, _ in conn.statements)


@pytest.mark.parametrize("method", ["stage_state", "abandon_working", "publish"])
def test_new_stage_apis_require_start(method):
    with pytest.raises(LedgerError, match="No active run"):
        getattr(PatchLedger(Connection()), method)()


def test_abandon_updates_current_working_tasks_and_history_atomically():
    ledger, conn = active([(True,), (1, True, False)])
    conn.rowcount = 3
    assert ledger.abandon_working(message="Pool confirmed stopped") == 3
    assert conn.transactions == conn.commits == 1
    updates = [(query, params) for query, params in conn.statements if query.startswith("UPDATE")]
    assert len(updates) == 2
    assert "status = 'abandoned'" in updates[0][0]
    assert updates[0][1] == ("Pool confirmed stopped", "run", "refine", 1)
    assert "attempt_no < 3" in updates[1][0]
    assert "THEN 'pending' ELSE 'failed' END" in updates[1][0]
    assert "attempt_token = NULL, next_retry_at = NULL" in updates[1][0]
    assert updates[1][1] == ("run", "refine", 1)
    for query, _ in updates:
        assert "generation = %s AND status = 'working'" in query


@pytest.mark.parametrize("message", [None, "", "  ", "unsafe\x00message", "x" * 4097])
def test_abandon_rejects_invalid_message_without_sql(message):
    ledger, conn = active()
    with pytest.raises(ValueError):
        ledger.abandon_working(message=message)
    assert not conn.statements


def test_abandon_failure_rolls_back_history_and_task_transition():
    ledger, conn = active([(True,), (1, True, False)])
    class FailingCursor(Cursor):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "UPDATE" in str(query) and "pipeline_patch_tasks" in str(query):
                raise OSError("transaction interrupted")
            return result
    conn.cursor = lambda: FailingCursor(conn)
    with pytest.raises(OSError):
        ledger.abandon_working()
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_publish_callback_and_stage_flag_share_transaction():
    ledger, conn = active([(True,), (1, True, False), (False,)])
    assert ledger.publish(lambda cur: cur.conn.statements.append(("BUSINESS", ())))
    assert conn.transactions == conn.commits == 1
    assert conn.statements[-2] == ("BUSINESS", ())
    assert "published = true" in conn.statements[-1][0]


@pytest.mark.parametrize("rows", [
    [(True,), (1, False, False)], [(True,), (1, True, False), (True,)],
])
def test_publish_ineligible_stage_never_calls_callback(rows):
    ledger, _ = active(rows)
    with pytest.raises(LedgerError):
        ledger.publish(lambda cur: pytest.fail("Ineligible callback"))


def test_publish_duplicate_never_calls_callback():
    ledger, conn = active([(True,), (1, True, True)])
    assert ledger.publish(lambda cur: pytest.fail("Duplicate callback")) is False
    assert not any(query.startswith("UPDATE") for query, _ in conn.statements)


def test_publish_callback_failure_rolls_back_and_is_not_unknown():
    ledger, conn = active([(True,), (1, True, False), (False,)])
    def callback(cur):
        raise ArithmeticError("Business publication failed")
    with pytest.raises(ArithmeticError):
        ledger.publish(callback)
    assert conn.rollbacks == 1
    assert not conn.closed
    assert not any("published = true" in query for query, _ in conn.statements)


@pytest.mark.parametrize("result", [Future(), 1, {}])
def test_publish_rejects_non_synchronous_none_callback_results(result):
    ledger, conn = active([(True,), (1, True, False), (False,)])
    with pytest.raises(ValueError, match="synchronously"):
        ledger.publish(lambda cur: result)
    assert conn.rollbacks == 1


def test_publish_rejects_coroutine_without_execution():
    ledger, conn = active([(True,), (1, True, False), (False,)])
    async def callback(cur):
        pytest.fail("Coroutine cannot execute")
    with pytest.raises(ValueError, match="synchronously"):
        ledger.publish(callback)
    assert conn.rollbacks == 1


def test_publish_rejects_transaction_boundary_changed_by_callback():
    ledger, conn = active([(True,), (1, True, False), (False,)])
    def callback(cur):
        conn.info.transaction_status = TransactionStatus.IDLE
    with pytest.raises(LedgerError, match="changed the owned transaction"):
        ledger.publish(callback)
    assert not any("published = true" in query for query, _ in conn.statements)


def test_publish_commit_acknowledgement_loss_requires_stage_reconciliation():
    ledger, conn = active([(True,), (1, True, False), (False,)])
    conn.commit_error = OSError("COMMIT acknowledgement lost")
    called = []
    with pytest.raises(CommitOutcomeUnknown, match="durable stage publication"):
        ledger.publish(lambda cur: called.append(True))
    assert called == [True]
    assert conn.closed


def test_publish_invalid_callback_rejected_before_sql():
    ledger, conn = active()
    with pytest.raises(ValueError, match="callable"):
        ledger.publish(42)
    assert not conn.statements


@pytest.mark.parametrize("unlock_error", [KeyboardInterrupt(), SystemExit(), OSError("unlock lost")])
@pytest.mark.parametrize("close_error", [None, KeyboardInterrupt(), SystemExit(), OSError("close lost")])
def test_start_failure_preserves_original_when_cleanup_is_interrupted(unlock_error, close_error):
    conn = Connection([(False,), (True,)])
    original = ArithmeticError("initialization failed after acquiring lock")

    class InitializationFailure(Cursor):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "SELECT signature, generation" in str(query):
                raise original
            return result

    conn.cursor = lambda: InitializationFailure(conn)
    close_calls = []

    def execute(query, params=()):
        assert query == "SELECT pg_advisory_unlock(%s)"
        raise unlock_error

    def close():
        close_calls.append(True)
        if close_error is not None:
            raise close_error
        conn.closed = True

    conn.execute, conn.close = execute, close
    ledger = PatchLedger(conn)
    with pytest.raises(ArithmeticError) as raised:
        ledger.start("run", "refine", SIGNATURE, execution_config={}, environment={})
    assert raised.value is original
    assert close_calls == [True]
    assert conn.closed is (close_error is None)
    assert conn.rollbacks == 1
    assert ledger._lock_key is None
    assert ledger.run_id is ledger.stage is ledger.generation is ledger.signature is None
    if close_error is not None:
        assert "Connection close was not confirmed" in raised.value.__notes__[0]


def test_failed_acquisition_close_interruption_preserves_original_and_clears_identity():
    conn = Connection([(False,)])
    original = OSError("acquisition acknowledgement lost")

    class FailedAcquisition(Cursor):
        def execute(self, query, params=()):
            result = super().execute(query, params)
            if "pg_try_advisory_lock" in str(query):
                raise original
            return result

    def close():
        raise KeyboardInterrupt("close interrupted")

    conn.cursor, conn.close = lambda: FailedAcquisition(conn), close
    ledger = PatchLedger(conn)
    with pytest.raises(OSError) as raised:
        ledger.start("run", "refine", SIGNATURE, execution_config={}, environment={})
    assert raised.value is original
    assert "Connection close was not confirmed" in raised.value.__notes__[0]
    assert ledger._lock_key is None
    assert ledger.run_id is ledger.stage is ledger.generation is ledger.signature is None
