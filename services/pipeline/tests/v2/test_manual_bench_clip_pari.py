"""Regression tests for the Clip-pair manual debugging script."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


class ManualBenchClipPairImportTest(unittest.TestCase):
    def test_script_bootstraps_project_root_without_running_the_database_query(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        source_root = project_root / "src"
        script = source_root / "track/v2/manual_thin_benchmark/manual_bench_clip_pari.py"
        probe = f"""
import runpy
import sys
from pathlib import Path

sys.meta_path = [
    finder
    for finder in sys.meta_path
    if not finder.__class__.__module__.startswith("__editable___track_")
]
sys.path = [
    entry
    for entry in sys.path
    if entry and Path(entry).resolve() != Path({str(source_root)!r})
]
runpy.run_path({str(script)!r}, run_name="manual_bench_import_probe")
"""

        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
