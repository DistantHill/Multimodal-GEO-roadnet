"""Generate a three-family tuning comparison from a saved CV preview."""

import argparse
from pathlib import Path

from .tuning import write_tuning_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output == repository or repository in output.parents:
        parser.error("runtime output must be outside the repository")
    if output.exists():
        parser.error("output directory already exists; choose a new path")
    print(write_tuning_report(args.preview.resolve(), output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
