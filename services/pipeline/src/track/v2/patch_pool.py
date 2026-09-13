"""Bounded, synchronous coordination of reusable spawned calculation processes.

Only small JSON descriptions cross process boundaries. Kernels own row reads and
large-result spooling; a central consumer owns atomic database publication. This
primitive does not persist state, retry failures, enforce memory limits, or make
business stages recoverable by itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
import importlib
import inspect
import json
import multiprocessing
import os
from typing import Any

from .patches import canonical_json


_THREAD_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
    "NUMBA_NUM_THREADS",
)


def _positive_integer(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _bounded_object(value: str, limit: int, name: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{name} must be a JSON object string")
    # Reject obvious oversize input before encoding or parsing a second copy.
    if len(value) > limit or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} exceeds max_descriptor_bytes")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must describe a JSON object")
    canonical_json(decoded)  # Reject non-finite JSON numbers.


@dataclass(frozen=True, slots=True)
class PatchTask:
    """A task identity and reader/attempt description, never materialized rows.

    The serialized identity plus descriptor is bounded again at dispatch. Keeping
    the descriptor small is a transport requirement, not a runtime memory bound.
    """

    task_id: str
    descriptor_json: str


class PatchExecutionError(RuntimeError):
    """A calculation failed; the coordinator can classify the original cause."""

    def __init__(self, task: PatchTask, error: Exception):
        self.task = task
        self.original_error = error
        super().__init__(f"Patch calculation failed: {task.task_id}")


class RemotePatchFailure(RuntimeError):
    """Sanitized child failure; no original exception, message, or traceback."""

    def __init__(self, descriptor_json: str):
        self.descriptor_json = descriptor_json
        descriptor = json.loads(descriptor_json)
        self.error_type = descriptor.get("error_type", "RemoteError")
        self.category = descriptor.get("category", "unknown")
        super().__init__(f"Remote calculation failed ({self.error_type})")


def _result(future: Future, task: PatchTask, limit: int) -> str:
    try:
        successful, description = future.result()
        _bounded_object(description, limit, "result_json")
        if successful is False:
            raise RemotePatchFailure(description)
        if successful is not True:
            raise ValueError("invalid child outcome tag")
        return description
    except Exception as error:
        raise PatchExecutionError(task, error) from error


def _validate_task(task: PatchTask, limit: int) -> None:
    if type(task) is not PatchTask:
        raise ValueError("tasks must contain PatchTask descriptions")
    if type(task.task_id) is not str or not task.task_id.strip():
        raise ValueError("task_id must be a nonempty string")
    if len(task.task_id) > limit:
        raise ValueError("task_id exceeds max_descriptor_bytes")
    identity_bytes = len(task.task_id.encode("utf-8"))
    if identity_bytes >= limit:
        raise ValueError("task_id leaves no room for a descriptor")
    _bounded_object(task.descriptor_json, limit - identity_bytes, "descriptor_json")


def _initialize_native_threads(threads: int) -> None:
    """Change only spawned child environment, before loading a kernel module."""
    for name in _THREAD_VARIABLES:
        os.environ[name] = str(threads)
    os.environ["OMP_DYNAMIC"] = "FALSE"
    os.environ["MKL_DYNAMIC"] = "FALSE"


def _execute_task(entrypoint: str, task: PatchTask, descriptor_limit: int) -> tuple[bool, str]:
    try:
        module_name, function_name = entrypoint.split(":")
        kernel = getattr(importlib.import_module(module_name), function_name)
        if not callable(kernel):
            raise TypeError("compute_entrypoint must resolve to a callable")
        result = kernel(task)
        # Validate inside the child, before the potentially expensive IPC transfer.
        _bounded_object(result, descriptor_limit, "result_json")
        return True, result
    except BaseException as error:
        # Never serialize arbitrary args/attributes, call str(error), or generate
        # a traceback. Even unpickleable/huge custom exceptions become small data.
        category = "resource" if isinstance(error, MemoryError) else "unknown"
        description = canonical_json({
            "error_type": type(error).__name__[:96], "category": category,
        })
        if len(description) > descriptor_limit:
            description = "{}"  # Valid tasks necessarily leave room for two bytes.
        return False, description


def run_bounded_patches(
    tasks: Iterable[PatchTask],
    *,
    compute_entrypoint: str,
    consume: Callable[[PatchTask, str], Any],
    max_workers: int,
    max_inflight: int,
    native_threads_per_process: int = 1,
    max_descriptor_bytes: int = 65_536,
    executor_factory: Callable[..., Any] = ProcessPoolExecutor,
    waiter: Callable[..., Any] = wait,
) -> int:
    """Run lazily and return the number synchronously accepted by ``consume``.

    ``max_inflight`` counts submitted tasks until central consumption returns,
    including finished results awaiting consumption. Results arrive in completion
    order (ties are unspecified). No tasks are replenished while the consumer is
    running. The consumer must finish its commit before returning; returning a
    future/awaitable is not supported. A failure stops further input pulls and
    propagates without an in-memory retry. Child errors are sanitized into a
    bounded type/category descriptor; messages, attributes and tracebacks never
    cross the process boundary. Very small descriptor limits omit type/category.
    Pool infrastructure failures retain their standard-library exception cause.
    Retry decisions belong to a durable
    attempt ledger; successful earlier commits are not rolled back here.

    The executor uses spawn, never inherited database connections. The kernel is
    a ``package.module:function`` string, imported after the child initializer
    sets native-thread environment variables. The application's spawn entrypoint
    must itself avoid importing native kernels at module scope: Python imports
    that entrypoint before running the initializer. Kernels must honor these
    settings; arbitrary libraries are not forcibly reconfigured or limited.

    On success or any error, queued futures are cancelled and the pool is shut
    down with ``wait=True``. Already-running tasks cannot be forcibly cancelled;
    shutdown can therefore wait for them. Source iterators remain caller-owned.
    Executor/wait injection exists for deterministic tests, not as a way to
    replace process isolation in production.
    """
    for name, value in (
        ("max_workers", max_workers), ("max_inflight", max_inflight),
        ("native_threads_per_process", native_threads_per_process),
        ("max_descriptor_bytes", max_descriptor_bytes),
    ):
        _positive_integer(value, name)
    if (type(compute_entrypoint) is not str or len(compute_entrypoint) > 512
            or compute_entrypoint.count(":") != 1):
        raise ValueError("compute_entrypoint must be package.module:function")
    module, function = compute_entrypoint.split(":")
    if not module or not all(part.isidentifier() for part in module.split(".")):
        raise ValueError("compute_entrypoint contains an invalid module name")
    if not function.isidentifier():
        raise ValueError("compute_entrypoint contains an invalid function name")
    if not callable(consume):
        raise ValueError("consume must be callable")

    source = iter(tasks)
    executor = executor_factory(
        max_workers=max_workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize_native_threads,
        initargs=(native_threads_per_process,),
    )
    pending: dict[Any, PatchTask] = {}
    exhausted = False
    consumed = 0
    try:
        while True:
            # Observe failures that finished during the previous central commit
            # before reading more input. A later concurrent failure is observed
            # on the next pass; this is not instantaneous process supervision.
            for future in pending:
                if future.done():
                    _result(future, pending[future], max_descriptor_bytes)
            while not exhausted and len(pending) < max_inflight:
                try:
                    task = next(source)
                except StopIteration:
                    exhausted = True
                    break
                _validate_task(task, max_descriptor_bytes)
                future = executor.submit(
                    _execute_task, compute_entrypoint, task, max_descriptor_bytes,
                )
                pending[future] = task
            if not pending:
                return consumed
            done, not_done = waiter(tuple(pending), return_when=FIRST_COMPLETED)
            del not_done  # The pending mapping already owns all unfinished work.
            if not done or not set(done).issubset(pending):
                raise RuntimeError("waiter returned an invalid completed set")
            # Do not replenish on a successful member of a batch whose other
            # member is already known to have failed.
            for future in done:
                _result(future, pending[future], max_descriptor_bytes)
            for future in done:
                task = pending[future]
                result = _result(future, task, max_descriptor_bytes)
                acknowledgement = consume(task, result)
                if (inspect.isawaitable(acknowledgement)
                        or inspect.isgenerator(acknowledgement)
                        or inspect.isasyncgen(acknowledgement)
                        or isinstance(acknowledgement, Future)):
                    if inspect.iscoroutine(acknowledgement) or inspect.isgenerator(acknowledgement):
                        acknowledgement.close()
                    raise TypeError("consume must complete synchronously, not return deferred work")
                del pending[future]
                consumed += 1
            # Do not retain an accepted batch through the next refill via the
            # wait result set or loop locals holding Future/result references.
            del done, future, task, result, acknowledgement
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
