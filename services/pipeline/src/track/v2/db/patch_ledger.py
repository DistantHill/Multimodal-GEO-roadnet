"""Opt-in flat patch ledger; dedicated coordinator connection, no pool/CLI wiring.

The supplied connection must be exclusively owned by this coordinator, including
its session advisory lock. Executors never receive it. Business callbacks receive
a cursor inside an owned transaction: they must not commit, roll back, switch
connections, or produce external side effects. Only transactional database writes
can be made atomic here. Final stage/API publication is a later integration step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import inspect
import math
from typing import Any
from uuid import uuid4

from psycopg import sql
from psycopg.pq import TransactionStatus

from ..patches import PatchManifest, ResumeSignature, canonical_json


class LedgerError(RuntimeError):
    """Rejected state transition; no business result was committed."""


class RunLocked(LedgerError):
    """Another coordinator holds the run's session lock."""


class StaleAttempt(LedgerError):
    """The generation or attempt token is no longer current."""


class CommitOutcomeUnknown(RuntimeError):
    """Commit acknowledgement failed. Reconnect and inspect the ledger first.

    Never automatically replay the business callback. Close the failed connection
    before obtaining a new session lock, start with the same signature, and look
    up this task. A complete task's result is already durable.
    """


@dataclass(frozen=True, slots=True)
class AttemptToken:
    run_id: str
    stage: str
    task_id: str
    generation: int
    attempt_no: int
    token: str

    def __post_init__(self) -> None:
        for name in ("run_id", "stage", "task_id", "token"):
            _text(getattr(self, name), name)
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("generation must be a positive integer")
        if type(self.attempt_no) is not int or not 1 <= self.attempt_no <= 3:
            raise ValueError("attempt_no must be between 1 and 3")


@dataclass(frozen=True, slots=True)
class TaskState:
    task_id: str
    status: str
    attempt_no: int
    generation: int | None


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty string without NUL")


def _idle_connection(conn: Any) -> None:
    if conn.autocommit is not True:
        raise ValueError("Ledger requires a dedicated autocommit=True connection")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Ledger requires an idle connection, not a nested transaction")


def _object(value: Mapping[str, Any], name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return canonical_json(value)


def install_ledger(conn: Any, ddl_text: str, *, schema: str = "public") -> None:
    """Execute the checked-in SQL template only on explicit caller request.

    The schema must already exist. Do not pass untrusted SQL; schema identifiers
    are quoted, but ddl_text is executable SQL, not a value parameter.
    """
    _idle_connection(conn)
    _text(schema, "schema")
    _text(ddl_text, "ddl_text")
    with conn.transaction():
        conn.execute(sql.SQL(ddl_text).format(schema=sql.Identifier(schema)))


class PatchLedger:
    """One active run/stage per exclusively owned connection, flat tasks only.

    Automatic attempts are capped at three across restarts. Resource/permanent
    failures are terminal here: callers need an explicit future recovery policy,
    never an unchanged-condition automatic retry. Configuration/environment JSON
    must already be sanitized by the caller; this module never collects secrets.
    """

    def __init__(self, conn: Any, *, schema: str = "public") -> None:
        _idle_connection(conn)
        _text(schema, "schema")
        self.conn = conn
        self.schema = schema
        self.run_id: str | None = None
        self.stage: str | None = None
        self.generation: int | None = None
        self.signature: ResumeSignature | None = None
        self._lock_key: int | None = None

    def _query(self, cur: Any, query: str, params: tuple = ()) -> Any:
        return cur.execute(sql.SQL(query).format(s=sql.Identifier(self.schema)), params)

    @property
    def _scope(self) -> tuple[str, str]:
        if self.run_id is None or self.stage is None:
            raise LedgerError("No active run; call start first")
        return self.run_id, self.stage

    def _has_lock(self, cur: Any) -> bool:
        if self._lock_key is None:
            return False
        unsigned = self._lock_key & ((1 << 64) - 1)
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE "
            "locktype = 'advisory' AND pid = pg_backend_pid() AND granted "
            "AND classid = %s::oid AND objid = %s::oid AND objsubid = 1)",
            (unsigned >> 32, unsigned & 0xFFFFFFFF),
        )
        return bool(cur.fetchone()[0])

    def _guard(self, cur: Any) -> tuple[bool, bool]:
        scope = self._scope
        if not self._has_lock(cur):
            raise StaleAttempt("Coordinator no longer owns the run lock")
        row = self._query(cur,
            "SELECT generation, sealed, published FROM {s}.pipeline_patch_runs "
            "WHERE run_id = %s AND stage = %s FOR UPDATE", scope).fetchone()
        if row is None or row[0] != self.generation:
            raise StaleAttempt("Coordinator generation is no longer current")
        return bool(row[1]), bool(row[2])

    def start(self, run_id: str, stage: str, signature: ResumeSignature, *,
              execution_config: Mapping[str, Any],
              environment: Mapping[str, Any]) -> int:
        _idle_connection(self.conn)
        if self.run_id is not None:
            raise LedgerError("Close the current session before starting another")
        _text(run_id, "run_id")
        _text(stage, "stage")
        if not isinstance(signature, ResumeSignature):
            raise ValueError("signature must be ResumeSignature")
        execution = _object(execution_config, "execution_config")
        hardware = _object(environment, "environment")
        signature_json = canonical_json(asdict(signature))
        # Stage intentionally excluded: only one coordinator per whole run.
        key = int.from_bytes(hashlib.sha256(canonical_json(
            [self.schema, run_id]).encode("ascii")).digest()[:8], "big", signed=True)
        acquisition_may_hold_lock = False
        try:
            with self.conn.cursor() as cur:
                self._lock_key = key
                if self._has_lock(cur):
                    raise RunLocked("Connection already owns this run lock")
                # Mark before sending: execute/fetch/close acknowledgement can fail
                # after PostgreSQL has already acquired the session lock.
                acquisition_may_hold_lock = True
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                if not cur.fetchone()[0]:
                    acquisition_may_hold_lock = False
                    raise RunLocked("Another coordinator owns this run")
        except BaseException:
            if acquisition_may_hold_lock:
                self.conn.close()
            self._lock_key = None
            raise
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                row = self._query(cur,
                    "SELECT signature, generation FROM {s}.pipeline_patch_runs "
                    "WHERE run_id = %s AND stage = %s FOR UPDATE", (run_id, stage)
                ).fetchone()
                if row is None:
                    generation = 1
                    self._query(cur,
                        "INSERT INTO {s}.pipeline_patch_runs "
                        "(run_id, stage, signature, generation) VALUES (%s,%s,%s::jsonb,1)",
                        (run_id, stage, signature_json))
                else:
                    if canonical_json(row[0]) != signature_json:
                        raise LedgerError("Resume signature differs; use a new run")
                    generation = row[1] + 1
                    self._query(cur,
                        "UPDATE {s}.pipeline_patch_runs SET generation = %s "
                        "WHERE run_id = %s AND stage = %s", (generation, run_id, stage))
                self._query(cur,
                    "INSERT INTO {s}.pipeline_patch_executions "
                    "(run_id,stage,generation,execution_config,environment) "
                    "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)",
                    (run_id, stage, generation, execution, hardware))
                self._query(cur,
                    "UPDATE {s}.pipeline_patch_attempts SET status = 'abandoned', "
                    "failure_kind = 'interrupted', error_message = 'Coordinator restarted', "
                    "finished_at = clock_timestamp() "
                    "WHERE run_id = %s AND stage = %s AND status = 'working'", (run_id, stage))
                self._query(cur,
                    "UPDATE {s}.pipeline_patch_tasks SET status = CASE WHEN attempt_no < 3 "
                    "THEN 'pending' ELSE 'failed' END, attempt_token = NULL, next_retry_at = NULL "
                    "WHERE run_id = %s AND stage = %s AND status = 'working'", (run_id, stage))
            self.run_id, self.stage = run_id, stage
            self.signature, self.generation = signature, generation
            return generation
        except BaseException:
            # Never hide the original failure if the connection is already lost.
            try:
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except Exception:
                self.conn.close()
            self._lock_key = None
            raise

    def close(self) -> None:
        """Release this ledger's lock; does not close a healthy caller connection."""
        if self._lock_key is not None:
            try:
                _idle_connection(self.conn)
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (self._lock_key,))
            except BaseException:
                # An unacknowledged unlock must not leave an orphan session lock.
                self.conn.close()
                raise
            finally:
                self._lock_key = None
                self.run_id = self.stage = None
                self.generation = None
                self.signature = None

    def register(self, manifest: PatchManifest) -> bool:
        _idle_connection(self.conn)
        if not isinstance(manifest, PatchManifest):
            raise ValueError("manifest must be PatchManifest")
        if manifest.parent_id is not None:
            raise ValueError("Parent/child manifests require a future atomic split protocol")
        if (manifest.run_id, manifest.stage) != self._scope:
            raise ValueError("Manifest belongs to another run/stage")
        signature = ResumeSignature(manifest.input_fingerprint, manifest.code_version,
                                    manifest.algorithm_version, manifest.semantic_config_digest)
        if signature != self.signature:
            raise ValueError("Manifest signature differs from the active run")
        encoded = canonical_json(manifest.to_dict())
        with self.conn.transaction(), self.conn.cursor() as cur:
            sealed, _ = self._guard(cur)
            row = self._query(cur,
                "SELECT manifest FROM {s}.pipeline_patch_tasks WHERE "
                "run_id = %s AND stage = %s AND task_id = %s",
                (*self._scope, manifest.task_id)).fetchone()
            if row is not None:
                if canonical_json(row[0]) != encoded:
                    raise LedgerError("Registered task identity has a different manifest")
                return False
            if sealed:
                raise LedgerError("Planning is sealed; new tasks cannot be registered")
            self._query(cur,
                "INSERT INTO {s}.pipeline_patch_tasks (run_id,stage,task_id,manifest) "
                "VALUES (%s,%s,%s,%s::jsonb)", (*self._scope, manifest.task_id, encoded))
            return True

    def seal(self) -> None:
        _idle_connection(self.conn)
        with self.conn.transaction(), self.conn.cursor() as cur:
            self._guard(cur)
            self._query(cur, "UPDATE {s}.pipeline_patch_runs SET sealed = true "
                        "WHERE run_id = %s AND stage = %s", self._scope)

    def lookup(self, task_id: str) -> TaskState | None:
        """Read one durable task; start first to establish scope and fencing."""
        _idle_connection(self.conn)
        _text(task_id, "task_id")
        with self.conn.cursor() as cur:
            row = self._query(cur,
                "SELECT task_id,status,attempt_no,generation FROM {s}.pipeline_patch_tasks "
                "WHERE run_id = %s AND stage = %s AND task_id = %s",
                (*self._scope, task_id)).fetchone()
            return TaskState(*row) if row is not None else None

    def claim(self, task_id: str) -> AttemptToken | None:
        _idle_connection(self.conn)
        _text(task_id, "task_id")
        with self.conn.transaction(), self.conn.cursor() as cur:
            _, published = self._guard(cur)
            if published:
                return None
            token = str(uuid4())
            row = self._query(cur,
                "UPDATE {s}.pipeline_patch_tasks SET status = 'working', "
                "attempt_no = attempt_no + 1, attempt_token = %s, generation = %s, next_retry_at = NULL "
                "WHERE run_id = %s AND stage = %s AND task_id = %s "
                "AND status = 'pending' AND attempt_no < 3 "
                "AND (next_retry_at IS NULL OR next_retry_at <= clock_timestamp()) RETURNING attempt_no",
                (token, self.generation, *self._scope, task_id)).fetchone()
            if row is None:
                return None
            number = row[0]
            self._query(cur,
                "INSERT INTO {s}.pipeline_patch_attempts "
                "(run_id,stage,task_id,attempt_no,attempt_token,generation,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'working')",
                (*self._scope, task_id, number, token, self.generation))
            return AttemptToken(*self._scope, task_id, self.generation, number, token)

    def pending_manifests(self, *, limit: int = 100) -> list[PatchManifest]:
        """Read a bounded, ready-to-dispatch page of fixed persisted manifests.

        This does not claim tasks. Claim or otherwise finish the returned tasks
        before requesting the next page; repeatedly calling without transitions
        may return the same entries. Retry delays remain enforced by claim too.
        """
        _idle_connection(self.conn)
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        with self.conn.transaction(), self.conn.cursor() as cur:
            self._guard(cur)
            self._query(cur,
                "SELECT manifest FROM {s}.pipeline_patch_tasks "
                "WHERE run_id = %s AND stage = %s AND status = 'pending' "
                "AND (next_retry_at IS NULL OR next_retry_at <= clock_timestamp()) "
                "ORDER BY task_id LIMIT %s", (*self._scope, limit))
            return [PatchManifest.from_dict(row[0]) for row in cur.fetchmany(limit)]

    def _attempt(self, cur: Any, token: AttemptToken) -> bool:
        if not isinstance(token, AttemptToken):
            raise ValueError("token must be AttemptToken")
        if (token.run_id, token.stage) != self._scope or token.generation != self.generation:
            raise StaleAttempt("Attempt belongs to another run/stage/generation")
        row = self._query(cur,
            "SELECT status,attempt_no,attempt_token,generation "
            "FROM {s}.pipeline_patch_tasks WHERE run_id = %s AND stage = %s "
            "AND task_id = %s FOR UPDATE", (*self._scope, token.task_id)).fetchone()
        if row is None:
            raise StaleAttempt("Task no longer exists")
        if row[0] == "complete":
            # A different earlier attempt must never masquerade as this completion.
            if (row[1], row[3]) != (token.attempt_no, token.generation):
                raise StaleAttempt("A different attempt completed this task")
            prior = self._query(cur,
                "SELECT attempt_token FROM {s}.pipeline_patch_attempts WHERE "
                "run_id = %s AND stage = %s AND task_id = %s AND attempt_no = %s",
                (*self._scope, token.task_id, token.attempt_no)).fetchone()
            if prior is None or prior[0] != token.token:
                raise StaleAttempt("Completion token differs")
            return False
        if row != ("working", token.attempt_no, token.token, token.generation):
            raise StaleAttempt("Attempt is not the current working attempt")
        return True

    def commit_result(self, token: AttemptToken, write_callback: Callable[[Any], None]) -> bool:
        """Run transactional writes once, then mark completion in the same commit.

        A callback exception rolls everything back and propagates unchanged. An
        exception while acknowledging COMMIT is deliberately indeterminate; this
        object must be discarded and the durable ledger inspected on reconnect.
        """
        _idle_connection(self.conn)
        if not callable(write_callback):
            raise ValueError("write_callback must be callable")
        ready_to_commit = False
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                self._guard(cur)
                if not self._attempt(cur, token):
                    return False
                callback_result = write_callback(cur)
                if callback_result is not None:
                    if inspect.iscoroutine(callback_result):
                        callback_result.close()
                    raise ValueError("write_callback must finish synchronously and return None")
                self._query(cur,
                    "UPDATE {s}.pipeline_patch_tasks SET status = 'complete', "
                    "attempt_token = NULL, completed_at = clock_timestamp() "
                    "WHERE run_id = %s AND stage = %s AND task_id = %s",
                    (*self._scope, token.task_id))
                self._query(cur,
                    "UPDATE {s}.pipeline_patch_attempts SET status = 'complete', "
                    "finished_at = clock_timestamp() WHERE run_id = %s AND stage = %s "
                    "AND task_id = %s AND attempt_no = %s",
                    (*self._scope, token.task_id, token.attempt_no))
                ready_to_commit = True
            return True
        except BaseException as exc:
            if ready_to_commit:
                # Closing releases the lock even when server-side commit succeeded.
                self.conn.close()
                raise CommitOutcomeUnknown(f"Inspect durable task {token.task_id} before retry") from exc
            raise

    def fail(self, token: AttemptToken, *, kind: str, message: str,
             retry_delay_seconds: float = 1.0) -> str:
        _idle_connection(self.conn)
        if kind not in ("transient", "resource", "permanent"):
            raise ValueError("Unknown failure kind")
        _text(message, "message")
        if len(message) > 4096:
            raise ValueError("Error message exceeds 4096 characters; sanitize and summarize it")
        if (type(retry_delay_seconds) not in (int, float)
                or not math.isfinite(retry_delay_seconds)
                or not 0 <= retry_delay_seconds <= 3600):
            raise ValueError("retry_delay_seconds must be finite and between 0 and 3600")
        with self.conn.transaction(), self.conn.cursor() as cur:
            self._guard(cur)
            if not self._attempt(cur, token):
                return "complete"
            status = "pending" if kind == "transient" and token.attempt_no < 3 else "failed"
            delay = retry_delay_seconds if status == "pending" else None
            self._query(cur,
                "UPDATE {s}.pipeline_patch_tasks SET status = %s, attempt_token = NULL, "
                "next_retry_at = clock_timestamp() + (%s * interval '1 second') "
                "WHERE run_id = %s AND stage = %s AND task_id = %s",
                (status, delay, *self._scope, token.task_id))
            self._query(cur,
                "UPDATE {s}.pipeline_patch_attempts SET status = 'failed', "
                "failure_kind = %s, error_message = %s, finished_at = clock_timestamp(), "
                "next_retry_at = clock_timestamp() + (%s * interval '1 second') "
                "WHERE run_id = %s AND stage = %s AND task_id = %s AND attempt_no = %s",
                (kind, message, delay, *self._scope, token.task_id, token.attempt_no))
            return status

    def publish(self) -> bool:
        """Publish the ledger stage only, not existing schema-v3/API stage rows."""
        _idle_connection(self.conn)
        with self.conn.transaction(), self.conn.cursor() as cur:
            sealed, published = self._guard(cur)
            if published:
                return False
            if not sealed:
                raise LedgerError("Cannot publish before planning is sealed")
            incomplete = self._query(cur,
                "SELECT EXISTS (SELECT 1 FROM {s}.pipeline_patch_tasks "
                "WHERE run_id = %s AND stage = %s AND status <> 'complete')",
                self._scope).fetchone()[0]
            if incomplete:
                raise LedgerError("Cannot publish an incomplete stage")
            self._query(cur, "UPDATE {s}.pipeline_patch_runs SET published = true "
                        "WHERE run_id = %s AND stage = %s", self._scope)
            return True
