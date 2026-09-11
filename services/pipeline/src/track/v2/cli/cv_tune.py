"""Generate a three-family tuning comparison from a saved CV preview."""

import argparse
from pathlib import Path

from ..cv_tuning import write_tuning_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package_root = Path(__file__).resolve().parents[4]
    repository = package_root.parents[1] if package_root.parent.name == "services" else package_root
    output = args.output.resolve()
    if output == repository or repository in output.parents:
        parser.error("runtime output must be outside the repository")
    if output.exists():
        parser.error("output directory already exists; choose a new path")
    print(write_tuning_report(args.preview.resolve(), output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
