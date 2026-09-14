"""Durable flat-stage coordination over an already sealed task manifest.

This is a programmatic integration point, not the production Refine entrypoint.
The caller owns input fingerprint verification, measured budget calibration,
exclusive runtime directory accounting and the ledger's start/close lifecycle.
No DB connection or materialized rows are sent to calculation processes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Any

from .db.patch_ledger import AttemptToken, CommitOutcomeUnknown, PatchLedger
from .patch_admission import ReservationBook, ReservationToken, ResourceUnavailable
from .patch_pool import (
    DispatchPause, PatchExecutionError, PatchTask, PoolShutdownUnconfirmed,
    RemotePatchFailure, run_bounded_patches,
)
from .patch_spool import SpoolArtifact, iter_artifact_chunks
from .patches import PatchManifest, canonical_json
from .resource_budget import BudgetPlan


class StageStopped(RuntimeError):
    """A terminal task, unsupported state, or resource shortage prevents publish."""


@dataclass(frozen=True)
class StageExecution:
    committed_this_call: int
    failed_calculations: int
    published_now: bool


@dataclass(frozen=True)
class _Active:
    manifest: PatchManifest
    attempt: AttemptToken
    reservation: ReservationToken


def _failure_kind(error: PatchExecutionError) -> str:
    remote = error.original_error
    if isinstance(remote, RemotePatchFailure):
        if remote.category == "resource" or remote.error_type in ("SpoolLimitError", "MemoryError"):
            return "resource"
        if remote.error_type in (
            "TimeoutError", "ConnectionError", "ConnectionResetError",
            "ConnectionAbortedError", "BrokenPipeError",
        ):
            return "transient"
    # Unknown/custom errors and broken pools must not be blindly retried.
    return "permanent"


def execute_registered_stage(
    ledger: PatchLedger,
    *,
    budget: BudgetPlan,
    reservations: ReservationBook,
    peak_bytes_per_row: int,
    spool_bytes_per_patch: int,
    runtime_root: str | Path,
    compute_entrypoint: str,
    build_descriptor: Callable[[PatchManifest, AttemptToken, int], dict],
    write_result: Callable[[Any, PatchManifest, SpoolArtifact, Iterator[bytes]], None],
    publish_callback: Callable[[Any], None] | None = None,
    retry_delay_seconds: float = 1.0,
    run_pool: Callable[..., int] = run_bounded_patches,
    sleep: Callable[[float], None] = time.sleep,
) -> StageExecution:
    """Resume registered tasks, apply durable retry limits, and publish atomically.

    ``budget`` must be produced from measured costs by plan_budget. The ledger
    must already be started with that effective execution/environment metadata
    and a verified full-input signature. Planning must be sealed. Reservations
    cover patch working memory only: coordinator/pool baselines and buffers are
    already deducted in budget. The source directory must be exclusive to this
    run; include all existing files in initial_spool_bytes on each restart.

    build_descriptor returns small, credential-free reader/config data and must
    convey the supplied attempt identity and spool allowance to its kernel. The
    kernel returns JSON containing an ``artifact`` SpoolArtifact description.
    write_result must synchronously consume ALL supplied chunks and return None.
    Integrity validation and result writes occur inside the ledger transaction.

    Files are retained and charged after commit; no automatic deletion. Unknown
    failed output is conservatively charged at its whole allocation after the
    pool has stopped. Exhausting disk quota stops safely, requiring explicit
    reconciled cleanup or a larger quota. This does not enforce physical memory.

    A transient calculation failure stops/drains the pool, records fail, then
    abandons other uncommitted attempts before bounded re-dispatch. Normal quota
    backpressure retains the same pool. CommitOutcomeUnknown always propagates:
    reconnect/verify/start/lookup before calling again, never blindly replay.
    """
    for name, value in (("peak_bytes_per_row", peak_bytes_per_row),
                        ("spool_bytes_per_patch", spool_bytes_per_patch)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (type(retry_delay_seconds) not in (float, int)
            or not math.isfinite(retry_delay_seconds) or not 0 <= retry_delay_seconds <= 3600):
        raise ValueError("retry_delay_seconds must be finite and between 0 and 3600")
    if not isinstance(budget, BudgetPlan) or budget.executor_processes <= 0:
        raise ValueError("budget must be a valid BudgetPlan")
    root = Path(runtime_root)
    if not root.is_absolute() or ".." in root.parts or root == Path("/") or not root.is_dir():
        raise ValueError("runtime_root must be an existing absolute non-root directory")
    snapshot = reservations.snapshot()
    if snapshot.active_count:
        raise ValueError("Start only after all earlier reservation owners have stopped")
    if snapshot.memory_capacity_bytes > budget.all_patch_working_bytes:
        raise ValueError("Reservation capacity exceeds the planned working-memory budget")
    for callback in (build_descriptor, write_result, run_pool, sleep):
        if not callable(callback):
            raise ValueError("Coordinator callbacks must be callable")
    active: dict[str, _Active] = {}
    committing: str | None = None
    committed = failures = 0

    def tasks():
        while True:
            manifests = ledger.pending_manifests(limit=1)
            if not manifests:
                return
            manifest = manifests[0]
            try:
                reserved = reservations.reserve(
                    manifest.task_id, memory_bytes=manifest.row_count * peak_bytes_per_row,
                    spool_bytes=spool_bytes_per_patch,
                )
            except ResourceUnavailable:
                if active:
                    yield DispatchPause()
                    continue
                raise
            try:
                attempt = ledger.claim(manifest.task_id)
            except BaseException:
                reservations.settle(reserved, retained_spool_bytes=0)
                raise
            if attempt is None:
                reservations.settle(reserved, retained_spool_bytes=0)
                raise StageStopped("Ready task could not be claimed")
            active[manifest.task_id] = _Active(manifest, attempt, reserved)
            descriptor = build_descriptor(manifest, attempt, spool_bytes_per_patch)
            yield PatchTask(manifest.task_id, canonical_json(descriptor))

    def consume(task: PatchTask, result_json: str):
        nonlocal committing, committed
        committing = task.task_id
        current = active[task.task_id]
        artifact = SpoolArtifact(**json.loads(result_json)["artifact"])
        if (artifact.run_id, artifact.task_id, artifact.attempt_id) != (
            current.attempt.run_id, task.task_id, current.attempt.token,
        ):
            raise ValueError("Artifact identity does not match the active attempt")
        if artifact.byte_count > spool_bytes_per_patch:
            raise ValueError("Artifact exceeds its reserved disk allowance")

        def write(cur):
            exhausted = False

            def chunks():
                nonlocal exhausted
                with closing(iter_artifact_chunks(root, artifact)) as reader:
                    yield from reader
                exhausted = True

            with closing(chunks()) as reader:
                result = write_result(cur, current.manifest, artifact, reader)
                if result is not None or not exhausted:
                    if hasattr(result, "close"):
                        result.close()
                    raise ValueError("Writer must synchronously exhaust the artifact and return None")

        ledger.commit_result(current.attempt, write)
        reservations.settle(current.reservation, retained_spool_bytes=artifact.byte_count)
        del active[task.task_id]
        committing = None
        committed += 1

    while True:
        state = ledger.stage_state()
        if not state.sealed:
            raise StageStopped("Planning must be sealed before execution")
        if state.published:
            return StageExecution(committed, failures, False)
        if state.failed:
            raise StageStopped("Terminal tasks require explicit repair; stage was not published")
        if state.working:
            raise StageStopped("Unowned working tasks require ledger restart after old executors stop")
        if not state.pending:
            return StageExecution(committed, failures, ledger.publish(publish_callback))
        if state.retry_after_seconds and state.retry_after_seconds > 0:
            sleep(min(state.retry_after_seconds, 60.0))
            continue
        try:
            run_pool(
                tasks(), compute_entrypoint=compute_entrypoint, consume=consume,
                max_workers=budget.executor_processes, max_inflight=budget.executor_processes,
                native_threads_per_process=budget.native_threads_per_process,
            )
        except BaseException as error:
            if isinstance(error, PoolShutdownUnconfirmed):
                # The exception retains the executor handle for explicit shutdown.
                # Keep book reservations and durable working states unchanged.
                raise
            # Other run_bounded_patches errors follow confirmed shutdown(wait=True).
            # Do not release any executing process's reservation before this point.
            for current in active.values():
                reservations.settle(current.reservation, retained_spool_bytes=spool_bytes_per_patch)
            retry = False
            try:
                if not isinstance(error, CommitOutcomeUnknown):
                    if isinstance(error, PatchExecutionError):
                        failures += 1
                        current = active[error.task.task_id]
                        kind = _failure_kind(error)
                        status = ledger.fail(
                            current.attempt, kind=kind, message="Calculation failed; sanitized classification only",
                            retry_delay_seconds=retry_delay_seconds,
                        )
                        retry = status == "pending" and kind == "transient"
                    elif committing is not None and isinstance(error, Exception):
                        ledger.fail(active[committing].attempt, kind="permanent", message="Result validation/write failed")
                    ledger.abandon_working(message="Pool stopped before these results were committed")
            except BaseException as cleanup_error:
                raise error from cleanup_error
            finally:
                active.clear()
                committing = None
            if retry:
                continue
            raise
        if active:
            raise StageStopped("Pool returned with unacknowledged attempts; restart is required")
