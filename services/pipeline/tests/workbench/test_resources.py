from __future__ import annotations

import ctypes
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from pipeline import _native
import pipeline.resources as resources
from pipeline.resources import ResourceLimitExceeded, ResourceMonitor, validate_heap_profiler


def test_stage_phase_counts_and_failure_are_persisted(tmp_path: Path) -> None:
    monitor = ResourceMonitor(tmp_path, sample_interval_ms=10)
    with pytest.raises(ValueError, match="source failed"):
        with monitor:
            with monitor.stage("refine") as stage:
                stage.add_counts(input_rows=3, output_rows=2)
                with stage.phase("compute"):
                    time.sleep(0.02)
                with stage.phase("write"):
                    raise ValueError("source failed")
    result = json.loads((tmp_path / "resources.json").read_text())
    assert result["status"] == "FAILED"
    assert result["stages"][0]["status"] == "FAILED"
    assert result["stages"][0]["counts"] == {"input_rows": 3, "output_rows": 2}
    assert result["stages"][0]["phases"]["compute"]["wall_s"] >= 0.015
    assert result["overall"]["wall_s"] >= result["stages"][0]["wall_s"]
    assert result["overall"]["rss_sampled_peak_bytes"] > 0
    assert result["overall"]["process_lifetime_peak_rss_bytes"] > 0
    assert result["overall"]["samples"] >= 2


def test_soft_budget_fails_at_safe_boundary_and_records(tmp_path: Path) -> None:
    monitor = ResourceMonitor(tmp_path, soft_memory_mb=0.001)
    with pytest.raises(ResourceLimitExceeded, match="soft memory budget"):
        with monitor:
            pytest.fail("Budget should fail before the work starts")
    data = json.loads((tmp_path / "resources.json").read_text())
    assert data["overall"]["rss_sampled_peak_bytes"] > 1000
    assert "not a hard cap" in data["semantics"]["budget"]


def test_wall_budget_and_phase_aggregation(tmp_path: Path) -> None:
    monitor = ResourceMonitor(tmp_path, sample_interval_ms=5, max_wall_s=0.04)
    with pytest.raises(ResourceLimitExceeded, match="Wall-time budget"):
        with monitor:
            with monitor.stage("detect") as stage:
                for _ in range(2):
                    with stage.phase("compute"):
                        time.sleep(0.03)
    data = json.loads((tmp_path / "resources.json").read_text())
    assert data["status"] == "RESOURCE_LIMIT"
    phase = data["stages"][0]["phases"]["compute"]
    assert phase["calls"] == 2
    assert phase["wall_s"] >= 0.05


def test_native_safety_limit_is_recorded_as_resource_limit(tmp_path: Path) -> None:
    with pytest.raises(_native.NativeResourceLimitExceeded, match="max_grid_memberships"):
        with ResourceMonitor(tmp_path) as monitor:
            with monitor.stage("detect") as stage:
                with stage.phase("compute"):
                    _native.crossings(
                        np.array([[-100., 0., 100., 0.], [0., -100., 0., 100.]]),
                        np.array([1, 2], dtype=np.int64),
                        {"cross_grid_size_m": 1., "max_grid_memberships": 2},
                    )
    result = json.loads((tmp_path / "resources.json").read_text())
    assert result["status"] == "RESOURCE_LIMIT"
    assert result["stages"][0]["status"] == "RESOURCE_LIMIT"


@pytest.mark.parametrize("bad", [0, -1, 60_001, 2.5, True])
def test_interval_validation(tmp_path: Path, bad: object) -> None:
    with pytest.raises(ValueError, match="sample_interval_ms"):
        ResourceMonitor(tmp_path, sample_interval_ms=bad)


def test_phases_cannot_nest_and_overcount(tmp_path: Path) -> None:
    with ResourceMonitor(tmp_path) as monitor:
        with monitor.stage("fit") as stage:
            with stage.phase("compute"):
                with pytest.raises(RuntimeError, match="overlap"):
                    with stage.phase("read"):
                        pass
            with pytest.raises(ValueError, match="nonnegative"):
                stage.add_counts(input_rows=-1)


@pytest.fixture(scope="module")
def native_allocation(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if sys.platform != "linux" or shutil.which("g++") is None:
        pytest.skip("Native /proc sampling fixture requires Linux and g++")
    directory = tmp_path_factory.mktemp("native-allocation")
    source = directory / "allocation.cpp"
    source.write_text('''
#include <cstddef>
#include <sys/mman.h>
#include <unistd.h>
extern "C" int hold_allocation(std::size_t size, unsigned milliseconds) {
    void* address = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (address == MAP_FAILED) return -1;
    volatile char* bytes = static_cast<volatile char*>(address);
    for (std::size_t i = 0; i < size; i += 4096) bytes[i] = 1;
    usleep(milliseconds * 1000);
    return munmap(address, size);
}
''')
    library = directory / "allocation.so"
    subprocess.run(["g++", "-O2", "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    return library


@pytest.mark.parametrize("hold_gil", [False, True])
def test_native_transient_allocation_measured_without_python_sampling(
    tmp_path: Path, native_allocation: Path, hold_gil: bool
) -> None:
    # PyDLL holds the GIL during the call. The allocation is gone before Python
    # resumes, so a Python observer or boundary-only RSS check would miss it.
    library = (ctypes.PyDLL if hold_gil else ctypes.CDLL)(str(native_allocation))
    library.hold_allocation.argtypes = [ctypes.c_size_t, ctypes.c_uint]
    library.hold_allocation.restype = ctypes.c_int
    allocation_bytes = 48 * 1024 * 1024
    with ResourceMonitor(tmp_path, sample_interval_ms=5, include_children=False) as monitor:
        with monitor.stage("transient_native"):
            assert library.hold_allocation(allocation_bytes, 180) == 0
    stage = monitor.summary()["stages"][0]
    assert stage["samples"] >= 3
    assert stage["rss_sampled_peak_bytes"] - stage["start"]["process_rss_bytes"] >= allocation_bytes * 0.8
    # A second small stage/run must not inherit the earlier sampled peak.
    with ResourceMonitor(tmp_path / "second", sample_interval_ms=5) as second:
        with second.stage("idle"):
            time.sleep(0.02)
    idle = second.summary()["stages"][0]
    assert idle["rss_sampled_peak_bytes"] < stage["rss_sampled_peak_bytes"] - allocation_bytes * 0.6


@pytest.mark.skipif(sys.platform != "linux", reason="Child RSS supported on Linux")
def test_process_tree_includes_child_rss(tmp_path: Path) -> None:
    with ResourceMonitor(tmp_path, sample_interval_ms=10) as monitor:
        with monitor.stage("child"):
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; a=bytearray(32*1024*1024); print('ready',flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                assert child.stdout.readline().strip() == "ready"
                sample = _native.resource_snapshot(True)
                assert sample["process_count"] >= 2
                assert sample["process_tree_rss_sum_bytes"] >= sample["process_rss_bytes"] + 28 * 1024 * 1024
                monitor.check()
            finally:
                child.communicate(timeout=10)
    assert monitor.summary()["stages"][0]["process_count_peak"] >= 2


@pytest.mark.skipif(sys.platform != "linux", reason="Child RSS supported on Linux")
@pytest.mark.parametrize("use_stage", [False, True])
def test_final_child_sample_enforces_budget(tmp_path: Path, use_stage: bool) -> None:
    # Disable interval sampling for this short run. The child is first sampled
    # by the final full tree read, after the ordinary cheap pre-close check.
    baseline = _native.resource_snapshot(True)["process_tree_rss_sum_bytes"]
    budget_mb = (baseline + 20 * 1024 * 1024) / 1_000_000
    child = None

    def start_child() -> None:
        nonlocal child
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; a=bytearray(64*1024*1024); print('ready',flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert child.stdout.readline().strip() == "ready"

    try:
        with pytest.raises(ResourceLimitExceeded, match="soft memory budget"):
            with ResourceMonitor(tmp_path, sample_interval_ms=60_000, soft_memory_mb=budget_mb) as monitor:
                if use_stage:
                    with monitor.stage("child"):
                        start_child()
                else:
                    start_child()
    finally:
        if child is not None:
            child.communicate(timeout=10)
    result = json.loads((tmp_path / "resources.json").read_text())
    assert result["status"] == "RESOURCE_LIMIT"
    assert result["overall"]["process_tree_rss_sampled_peak_bytes"] > budget_mb * 1_000_000
    if use_stage:
        assert result["stages"][0]["status"] == "RESOURCE_LIMIT"


def test_heap_profiler_stops_when_sampler_finalization_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = []

    class BrokenSampler:
        def __init__(self, *args):
            pass

        def begin(self, name):
            return 1

        def peek(self, identifier):
            return {"rss_sampled_peak_bytes": 0, "process_tree_rss_sampled_peak_bytes": 0, "wall_s": 0.0}

        def end(self, identifier):
            raise RuntimeError("sampler finalization failed")

        def close(self):
            events.append("sampler close")

    fake_native = SimpleNamespace(
        NativeMonitor=BrokenSampler,
        heap_profiler_status=lambda: {"available": True, "running": False},
        heap_profile_start=lambda prefix: events.append("heap start"),
        heap_profile_dump=lambda reason: events.append("heap dump"),
        heap_profile_stop=lambda: events.append("heap stop"),
    )
    monkeypatch.setattr(resources, "_native_module", lambda: fake_native)
    with pytest.raises(RuntimeError, match="sampler finalization failed"):
        with ResourceMonitor(tmp_path, heap_profile=True):
            pass
    assert events == ["heap start", "sampler close", "heap dump", "heap stop"]
    assert json.loads((tmp_path / "resources.json").read_text())["status"] == "FAILED"


def test_requested_heap_profiler_cannot_silently_fall_back(tmp_path: Path) -> None:
    status = _native.heap_profiler_status()
    if status["available"]:
        # This branch also validates the optional integration in a preloaded CI run.
        with ResourceMonitor(tmp_path, heap_profile=True) as monitor:
            with monitor.stage("allocation"):
                value = bytearray(1_000_000)
                assert len(value) == 1_000_000
        assert list(tmp_path.glob("heap.*.heap"))
        assert not _native.heap_profiler_status()["running"]
    else:
        with pytest.raises(ValueError, match="LD_PRELOAD"):
            validate_heap_profiler()
        with pytest.raises(ValueError, match="LD_PRELOAD"):
            with ResourceMonitor(tmp_path, heap_profile=True):
                pass
