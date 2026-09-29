"""Observe container memory without changing Pipeline execution or resource limits."""
from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
import time

from .console_logging import log_error


MEMORY_WARNING_BYTES = 6_000_000_000
_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")


class MemoryWarningMonitor:
    def __init__(self, threshold_bytes: int = MEMORY_WARNING_BYTES, interval_seconds: float = 30.0):
        if threshold_bytes < 1 or interval_seconds <= 0:
            raise ValueError("memory warning threshold and interval must be positive")
        self.threshold_bytes = threshold_bytes
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._run, name="pipeline-memory-warning", daemon=True)
        self._last_warning: float | None = None

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join()

    def _sample(self, current_bytes: int, now: float) -> None:
        if current_bytes <= self.threshold_bytes:
            self._last_warning = None
            return
        if self._last_warning is None or now - self._last_warning >= 600.0:
            log_error(
                "Pipeline 容器内存超过观测阈值 / memory warning: "
                f"memory.current={current_bytes} B, threshold={self.threshold_bytes} B; "
                "作业继续执行 / execution continues"
            )
            self._last_warning = now

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._sample(int(_MEMORY_CURRENT.read_text().strip()), time.monotonic())
            except (OSError, ValueError):
                # The observer must not change the job's result if cgroup data is unavailable.
                continue
