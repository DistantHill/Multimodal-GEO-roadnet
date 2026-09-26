"""Periodic operation status while the caller is blocked in SQL or a loop.

The observer never uses the database connection. Nested operations share one
thread, and scope exit joins it before the caller publishes completion/failure.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import inspect
from threading import Event, Lock, Thread
import time
from typing import Any, Callable, Iterator

from .console_logging import log_info


@dataclass(frozen=True)
class OperationStatus:
    operation: str
    started: float
    completed: int = 0
    total: int | None = None
    unit: str = ''
    detail: str = ''

    def message(self) -> str:
        elapsed = time.monotonic() - self.started
        counts = ''
        if self.unit:
            total = '' if self.total is None else f'/{self.total:,}'
            counts = f'，{self.completed:,}{total} {self.unit}'
        detail = f'，{self.detail}' if self.detail else ''
        return f'{self.operation}，已用 {elapsed:.1f} 秒{counts}{detail}，仍在执行'


class _Monitor:
    def __init__(
        self, status: OperationStatus,
        emit: Callable[[str], None] | None, interval: float,
    ) -> None:
        self.status = status
        self.emit = emit
        self.interval = interval
        self.lock = Lock()
        self.stopped = Event()
        self.thread = Thread(target=self.run, name='pipeline-heartbeat', daemon=True)

    def run(self) -> None:
        while not self.stopped.wait(self.interval):
            # Keep callbacks serialized with scope changes, so a completed
            # query cannot publish a late heartbeat over the next operation.
            with self.lock:
                message = self.status.message()
                log_info(f'[heartbeat] {message}')
                if self.emit is not None:
                    self.emit(message)


_current: ContextVar[_Monitor | None] = ContextVar('pipeline_operation', default=None)


@contextmanager
def operation_heartbeat(
    operation: str,
    *,
    emit: Callable[[str], None] | None = None,
    interval: float = 5.0,
) -> Iterator[None]:
    """Observe a blocking operation; elapsed time is not database progress."""
    if interval <= 0:
        raise ValueError('heartbeat interval must be positive')
    status = OperationStatus(operation, time.monotonic())
    monitor = _current.get()
    if monitor is not None:
        with monitor.lock:
            previous = monitor.status
            monitor.status = status
        try:
            yield
        finally:
            with monitor.lock:
                monitor.status = previous
        return

    monitor = _Monitor(status, emit, interval)
    token = _current.set(monitor)
    try:
        if emit is not None:
            emit(status.message())
        monitor.thread.start()
        try:
            yield
        finally:
            monitor.stopped.set()
            monitor.thread.join()
    finally:
        _current.reset(token)


def heartbeat_progress(
    completed: int, *, total: int | None = None, unit: str = '行', detail: str = '',
) -> None:
    """Publish counts at batch boundaries, without logging once per row."""
    monitor = _current.get()
    if monitor is not None:
        with monitor.lock:
            monitor.status = replace(
                monitor.status, completed=completed, total=total, unit=unit, detail=detail,
            )


def observed_operation(operation: str):
    """Add a heartbeat around a query, including iteration of its result."""
    def decorate(function):
        if inspect.isgeneratorfunction(function):
            @wraps(function)
            def iterate(*args, **kwargs):
                with operation_heartbeat(operation):
                    yield from function(*args, **kwargs)
            return iterate

        @wraps(function)
        def call(*args, **kwargs):
            with operation_heartbeat(operation):
                return function(*args, **kwargs)
        return call
    return decorate


@contextmanager
def validation_notices(conn: Any) -> Iterator[None]:
    """Name the current deferred check without querying the busy connection."""
    monitor = _current.get()
    if monitor is None:
        yield
        return

    def notice(diagnostic: Any) -> None:
        message = diagnostic.message_primary or ''
        prefix = 'track_v2 validation: '
        if message.startswith(prefix):
            with monitor.lock:
                monitor.status = OperationStatus(message[len(prefix):], time.monotonic())

    conn.add_notice_handler(notice)
    try:
        yield
    finally:
        conn.remove_notice_handler(notice)
