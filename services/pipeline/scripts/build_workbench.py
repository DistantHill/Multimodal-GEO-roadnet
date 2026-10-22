"""Build the native wheel in an external staging directory, leaving source clean."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/roadnet-workbench-build"))
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.output.expanduser().resolve()
    repository = source.parents[1]
    if output.is_relative_to(repository):
        parser.error("--output must be outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=output) as temporary:
        staging = Path(temporary) / "source"
        staging.mkdir()
        for name in ("pyproject.toml", "setup.py", "MANIFEST.in"):
            shutil.copy2(source / name, staging / name)
        for name in ("src", "native"):
            shutil.copytree(source / name, staging / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.so", "*.egg-info"))
        wheels = Path(temporary) / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--wheel-dir", str(wheels), str(staging)], check=True)
        built = list(wheels.glob("roadnet_pipeline-*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"Expected one workbench wheel, found {len(built)}")
        wheel = output / built[0].name
        shutil.copy2(built[0], wheel)
    print(f"Built {wheel}", flush=True)
    if args.install:
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)], check=True)


if __name__ == "__main__":
    main()
