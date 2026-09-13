import importlib
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["cv_preview", "cv_tune"])
def test_module_entrypoint_help(command):
    result = subprocess.run(
        [sys.executable, "-m", f"scripts.cv_human_check.{command}", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--output" in result.stdout


@pytest.mark.parametrize("command, inputs", [
    ("cv_preview", ["--detect-id", "00000000-0000-0000-0000-000000000001"]),
    ("cv_tune", ["--preview", "/tmp/unused"]),
])
def test_output_in_checkout_is_rejected_before_reading_inputs(command, inputs, monkeypatch):
    module = importlib.import_module(f"scripts.cv_human_check.{command}")
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(sys, "argv", [command, *inputs, "--output", str(repository / "unsafe")])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
