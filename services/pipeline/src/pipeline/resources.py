"""Native resource measurements, safe-boundary budgets and gperftools integration.

RSS is measured by C++ on a dedicated sampling thread, including while Python's
GIL is held. Sampled peaks can miss short spikes; the process lifetime high-water
mark is reported separately and is never presented as a stage-local peak.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import tempfile
from typing import Any, Iterator


class ResourceLimitExceeded(RuntimeError):
    """A sampled soft budget was exceeded; computation stops at a safe boundary."""


def _native_module() -> Any:
    try:
        from . import _native
    except ImportError as exc:
        raise RuntimeError(
            "The C++ extension is required. Build/install services/pipeline before running the workbench."
        ) from exc
    return _native


def _exception_status(exc: BaseException | None) -> str:
    if exc is None:
        return "COMPLETE"
    native_limit = getattr(_native_module(), "NativeResourceLimitExceeded", ())
    if isinstance(exc, ResourceLimitExceeded) or isinstance(exc, native_limit):
        return "RESOURCE_LIMIT"
    return "PRUNED" if type(exc).__name__ == "TrialPruned" else "FAILED"


def validate_heap_profiler() -> dict[str, Any]:
    """Fail before a run if a requested gperftools allocator is not preloaded."""
    status = dict(_native_module().heap_profiler_status())
    if not status["available"]:
        raise ValueError(
            "resources.heap_profile=true requires gperftools' full libtcmalloc "
            "(not libtcmalloc_minimal). Install libgoogle-perftools-dev and start Python "
            "with LD_PRELOAD=/absolute/path/to/libtcmalloc.so. This must be set before "
            "the process starts; loading a library later cannot profile earlier allocations."
        )
    if status["running"]:
        raise ValueError(
            "gperftools is already profiling. Remove HEAPPROFILE; the workbench starts and stops its own profile."
        )
    return status


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".resources-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class StageSpan:
    """One stage with repeatable read/compute/write timing and row counters."""

    def __init__(self, monitor: ResourceMonitor, name: str) -> None:
        self.monitor = monitor
        self.name = name
        self.counts: dict[str, int] = {}
        self.phases: dict[str, dict[str, Any]] = {}
        self._phase_active = False

    def add_counts(self, **counts: int) -> None:
        for name, value in counts.items():
            if type(value) is not int or value < 0:
                raise ValueError(f"Resource count {name} must be a nonnegative integer")
            self.counts[name] = self.counts.get(name, 0) + value

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time nonoverlapping phases; repeated batch phases are aggregated."""
        if name not in {"read", "compute", "write"}:
            raise ValueError("Resource phases must be read, compute or write")
        if self._phase_active:
            raise RuntimeError("Resource phases must not overlap or nest")
        self.monitor.check()
        span_id = self.monitor._sampler.begin(f"{self.name}.{name}", False)
        self._phase_active = True
        try:
            yield
        finally:
            measured = dict(self.monitor._sampler.end(span_id, False))
            self._phase_active = False
            previous = self.phases.setdefault(name, {
                "calls": 0,
                "wall_s": 0.0,
                "process_cpu_user_s": 0.0,
                "process_cpu_system_s": 0.0,
                "rss_sampled_peak_bytes": None,
                "process_tree_rss_sampled_peak_bytes": None,
            })
            previous["calls"] += 1
            for key in ("wall_s", "process_cpu_user_s", "process_cpu_system_s"):
                previous[key] += measured[key]
            for key in ("rss_sampled_peak_bytes", "process_tree_rss_sampled_peak_bytes"):
                if measured[key] is not None:
                    previous[key] = max(previous[key] or 0, measured[key])
        # Preserve an original computation exception instead of masking it.
        self.monitor.check()


class ResourceMonitor:
    """Measure a run and its stages without summing overlapping timers.

    ``soft_memory_mb`` uses decimal MB and the sum of resident sizes of live
    processes when children are included. Shared pages can therefore be counted
    multiple times. A sampled budget is cooperative, not an OS-enforced cap.
    Cgroup values include any other jobs in that cgroup and are only diagnostics.
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        sample_interval_ms: int = 100,
        soft_memory_mb: float = 0,
        max_wall_s: float = 0,
        include_children: bool = True,
        heap_profile: bool = False,
    ) -> None:
        if type(sample_interval_ms) is not int or not 1 <= sample_interval_ms <= 60_000:
            raise ValueError("sample_interval_ms must be an integer in [1, 60000]")
        for name, value in (("soft_memory_mb", soft_memory_mb), ("max_wall_s", max_wall_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if type(include_children) is not bool or type(heap_profile) is not bool:
            raise ValueError("include_children and heap_profile must be booleans")
        self.output_dir = Path(output_dir)
        self.sample_interval_ms = sample_interval_ms
        self.soft_memory_mb = float(soft_memory_mb)
        self.max_wall_s = float(max_wall_s)
        self.include_children = include_children
        self.heap_profile = heap_profile
        self.status = "NEW"
        self.stages: list[dict[str, Any]] = []
        self._sampler: Any = None
        self._run_id: int | None = None
        self._run_result: dict[str, Any] | None = None
        self._heap_started = False
        self._active_stage = False
        self._closed = False

    def __enter__(self) -> ResourceMonitor:
        if self.status != "NEW":
            raise RuntimeError("ResourceMonitor instances cannot be reused")
        native = _native_module()
        if self.heap_profile:
            validate_heap_profiler()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.heap_profile:
                native.heap_profile_start(str((self.output_dir / "heap").resolve()))
                self._heap_started = True
            self._sampler = native.NativeMonitor(self.sample_interval_ms, self.include_children)
            self._run_id = self._sampler.begin("run")
            self.status = "RUNNING"
            self.check()
        except BaseException as exc:
            self.close(status=_exception_status(exc))
            raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        status = _exception_status(exc)
        if exc is None:
            try:
                self.check()
            except ResourceLimitExceeded:
                self.close(status="RESOURCE_LIMIT")
                raise
        self.close(status=status)

    @contextmanager
    def stage(self, name: str) -> Iterator[StageSpan]:
        if self.status != "RUNNING" or self._closed:
            raise RuntimeError("Start ResourceMonitor with a context manager before entering stages")
        if self._active_stage:
            raise RuntimeError("Pipeline stages must not overlap or nest")
        self.check()
        span = StageSpan(self, name)
        span_id = self._sampler.begin(name)
        self._active_stage = True
        status = "COMPLETE"
        try:
            yield span
            self.check()
        except BaseException as exc:
            status = _exception_status(exc)
            raise
        finally:
            try:
                measured = dict(self._sampler.end(span_id))
            finally:
                self._active_stage = False
            boundary_error: ResourceLimitExceeded | None = None
            if status == "COMPLETE":
                # end() includes a full tree sample, unlike cheap check() reads.
                # A child first observed at this boundary must still enforce the
                # budget before the stage is recorded as successful.
                try:
                    self.check()
                except ResourceLimitExceeded as exc:
                    status = "RESOURCE_LIMIT"
                    boundary_error = exc
            measured.update(status=status, phases=span.phases, counts=span.counts)
            # Phases are explicitly nonoverlapping; wall is still measured directly.
            phase_s = sum(phase["wall_s"] for phase in span.phases.values())
            measured["orchestration_wall_s"] = max(0.0, measured["wall_s"] - phase_s)
            self.stages.append(measured)
            if self._heap_started:
                _native_module().heap_profile_dump(f"stage {name}: {status}")
            _atomic_json(self.output_dir / "resources.json", self.summary())
            if boundary_error is not None:
                raise boundary_error

    def check(self) -> None:
        """Raise at a safe boundary if any recorded sample exceeded the budget."""
        if self._sampler is None or self._run_id is None or self._closed:
            raise RuntimeError("Resource monitor is not running")
        measured = dict(self._sampler.peek(self._run_id))
        self._check_measured(measured)

    def _check_measured(self, measured: dict[str, Any]) -> None:
        key = "process_tree_rss_sampled_peak_bytes" if self.include_children else "rss_sampled_peak_bytes"
        peak = max(measured[key] or 0, measured["rss_sampled_peak_bytes"] or 0)
        if self.soft_memory_mb and peak > self.soft_memory_mb * 1_000_000:
            raise ResourceLimitExceeded(
                f"Sampled soft memory budget exceeded: {peak} bytes > "
                f"{self.soft_memory_mb * 1_000_000:.0f} bytes ({key}); stopped at a safe boundary"
            )
        if self.max_wall_s and measured["wall_s"] > self.max_wall_s:
            raise ResourceLimitExceeded(
                f"Wall-time budget exceeded: {measured['wall_s']:.3f}s > "
                f"{self.max_wall_s:.3f}s; stopped at a safe boundary"
            )

    def summary(self) -> dict[str, Any]:
        overall = self._run_result
        if overall is None and self._sampler is not None and self._run_id is not None and not self._closed:
            overall = dict(self._sampler.peek(self._run_id))
        return {
            "schema_version": 1,
            "status": self.status,
            "measurement_backend": "C++ /proc + getrusage (Linux); mach + getrusage (macOS)",
            "sample_interval_ms": self.sample_interval_ms,
            "soft_memory_mb": self.soft_memory_mb,
            "max_wall_s": self.max_wall_s,
            "include_children": self.include_children,
            "heap_profile": self.heap_profile,
            "heap_profile_library": "gperftools" if self.heap_profile else None,
            "overall": overall,
            "stages": self.stages,
            "semantics": {
                "wall": "Monotonic elapsed time, measured directly; not the sum of nested timers.",
                "cpu": "This process, including all threads; excludes child-process CPU.",
                "rss_sampled_peak": "Peak of samples within this span, including start/end; short spikes may be missed.",
                "sampling": "Process RSS is also sampled at cheap phase/check boundaries; full tree/cgroup samples use the interval thread and outer stage boundaries.",
                "process_lifetime_peak_rss": "OS process high-water mark since process start; not stage-local.",
                "process_tree_rss": "Sum of live descendants' RSS; shared pages may be counted more than once; short-lived children may be missed.",
                "cgroup": "Current group plus descendants, possibly other jobs and file cache; not an exclusive run budget.",
                "budget": "Sampled cooperative soft limit checked at boundaries, not a hard cap or preemptive timeout.",
                "heap_profile": "gperftools allocations through tcmalloc; excludes non-allocator mappings and is not total RSS.",
                "units": "Memory fields in bytes; soft_memory_mb in decimal MB; time in seconds.",
            },
        }

    def close(self, *, status: str | None = None) -> None:
        if self._closed:
            return
        if status is not None:
            self.status = status
        elif self.status == "RUNNING":
            self.status = "COMPLETE"
        try:
            try:
                if self._sampler is not None:
                    try:
                        if self._run_id is not None:
                            self._run_result = dict(self._sampler.end(self._run_id))
                            if self.status == "COMPLETE":
                                self._check_measured(self._run_result)
                    except BaseException as exc:
                        self.status = _exception_status(exc)
                        raise
                    finally:
                        self._sampler.close()
            finally:
                # The allocator profiler is process-global. Never leave it
                # active after a failed RSS sampler finalization.
                if self._heap_started:
                    try:
                        _native_module().heap_profile_dump(f"run: {self.status}")
                    finally:
                        try:
                            _native_module().heap_profile_stop()
                        finally:
                            self._heap_started = False
        finally:
            self._closed = True
            _atomic_json(self.output_dir / "resources.json", self.summary())
