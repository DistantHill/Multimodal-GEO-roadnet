"""The cgroup warning is informational and never stops a Pipeline run."""
from __future__ import annotations

from track.v2.telemetry import memory_warning


def test_measurement_rejects_container_hard_cap():
    import importlib.util
    from pathlib import Path
    import pytest

    script = Path(__file__).resolve().parents[2] / "scripts" / "measure_pipeline_run.py"
    spec = importlib.util.spec_from_file_location("measure_pipeline_run", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.require_uncapped_memory("max")
    with pytest.raises(SystemExit, match="memory.max=max"):
        module.require_uncapped_memory("4000000000")
    assert module.pipeline_selection_args("public.trajectory_points", None) == [
        "--source-table", "public.trajectory_points",
    ]
    assert module.pipeline_selection_args("public.trajectory_points", "completed-refine-id") == [
        "--refine-id", "completed-refine-id",
    ]

    class RefineLookup:
        def __init__(self, row):
            self.row = row

        def execute(self, statement, parameters):
            assert "FROM refine_runs" in statement
            assert parameters == ("completed-refine-id",)
            return self

        def fetchone(self):
            return self.row

    module.require_completed_refine_source(
        RefineLookup(("public.trajectory_points", "completed")),
        "completed-refine-id", "public.trajectory_points",
    )
    with pytest.raises(SystemExit, match="completed and belong"):
        module.require_completed_refine_source(
            RefineLookup(("public.trajectory_points", "running")),
            "completed-refine-id", "public.trajectory_points",
        )


def test_threshold_logs_error_once_per_episode_and_does_not_stop(monkeypatch):
    messages = []
    monkeypatch.setattr(memory_warning, "log_error", messages.append)
    monitor = memory_warning.MemoryWarningMonitor(threshold_bytes=6_000_000_000)

    monitor._sample(6_000_000_000, 0.0)
    assert not messages
    monitor._sample(6_000_000_001, 1.0)
    monitor._sample(6_100_000_000, 2.0)
    assert len(messages) == 1
    assert "作业继续执行" in messages[0]
    monitor._sample(6_100_000_000, 602.0)
    assert len(messages) == 2
    monitor._sample(5_000_000_000, 603.0)
    monitor._sample(6_000_000_001, 604.0)
    assert len(messages) == 3


def test_cgroup_observer_reads_current_and_logs_without_changing_result(tmp_path, monkeypatch):
    from threading import Event

    current = tmp_path / "memory.current"
    current.write_text("6000000001")
    seen = Event()
    monkeypatch.setattr(memory_warning, "_MEMORY_CURRENT", current)
    monkeypatch.setattr(memory_warning, "log_error", lambda _message: seen.set())
    monitor = memory_warning.MemoryWarningMonitor(interval_seconds=0.001)
    monitor.start()
    try:
        assert seen.wait(1.0)
    finally:
        monitor.close()


def test_warning_is_in_pipeline_log_and_process_exits_successfully(tmp_path):
    import subprocess
    import sys

    current = tmp_path / "memory.current"
    current.write_text("2")
    script = "\n".join((
        "import time",
        "from pathlib import Path",
        "from track.v2.telemetry.console_logging import init_logger, shutdown_logger",
        "from track.v2.telemetry import memory_warning",
        f"memory_warning._MEMORY_CURRENT = Path({str(current)!r})",
        f"init_logger(Path({str(tmp_path)!r}))",
        "monitor = memory_warning.MemoryWarningMonitor(threshold_bytes=1, interval_seconds=0.001)",
        "monitor.start()",
        "time.sleep(0.05)",
        "monitor.close()",
        "print('PIPELINE_CONTINUES')",
        "shutdown_logger()",
    ))
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    logs = list(tmp_path.glob("*_log.txt"))
    assert len(logs) == 1
    contents = logs[0].read_text()
    assert "[ERROR]" in contents
    assert "threshold=1 B" in contents
    assert "PIPELINE_CONTINUES" in contents
