from __future__ import annotations

import argparse
import signal
import sys
import tempfile
import time
from track.v2.config import PipelineConfig
from track.v2.cli._load_config import load_startup_config


def check_config(config: PipelineConfig) -> int:
    print(f"paths.data_root={config.paths.data_root}")
    print(f"paths.parquet_directory={config.paths.parquet_directory}")
    print(f"logging.directory={config.logging.directory}")
    print(f"postgres.host={config.postgres.host}")
    roots = {
        "paths.data_root": config.paths.data_root,
        "paths.parquet_directory": config.paths.parquet_directory,
        "logging.directory": config.logging.directory,
    }
    if any(not path.is_absolute() for path in roots.values()):
        print("ERROR: runtime roots must be absolute paths")
        return 1
    try:
        data_root = config.paths.data_root.resolve(strict=False)
        for name, path in roots.items():
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise OSError(f"{name} is not a directory: {path}")
            with tempfile.NamedTemporaryFile(dir=path):
                pass
    except (OSError, ValueError) as exc:
        print(f"ERROR: invalid runtime directory: {exc}")
        return 1
    return 0


def serve(config: PipelineConfig) -> int:
    """Keep the independent batch runtime available in the Compose stack."""

    print("Pipeline runtime ready. Start a run with `python -m pipeline run ...`.")
    print(f"Runtime data root: {config.paths.data_root}")
    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        time.sleep(1)
    return 0


def main() -> int:
    delegated_modules = {
        "artifacts": "reconcile_artifacts",
        "delete-run": "delete_run",
        "import-trajectories": "import_trajectories",
        "init-db": "init_db",
        "replay": "replay_run",
        "run": "run_pipeline",
        "semantic-diff": "compare_run_lineages",
        "rasterize": "rasterize",
    }
    if len(sys.argv) > 1 and sys.argv[1] in delegated_modules:
        command, forwarded = sys.argv[1], sys.argv[2:]
        if command == "init-db":
            from track.v2.cli.init_db import main as delegated_main
        elif command == "artifacts":
            from track.v2.cli.reconcile_artifacts import main as delegated_main
        elif command == "run":
            from track.v2.cli.run_pipeline import main as delegated_main
        elif command == "semantic-diff":
            from track.v2.cli.compare_run_lineages import main as delegated_main
        elif command == "delete-run":
            from track.v2.cli.delete_run import main as delegated_main
        elif command == "import-trajectories":
            from track.v2.cli.import_trajectories import main as delegated_main
        elif command == "rasterize":
            from track.v2.cli.rasterize import main as delegated_main
        else:
            from track.v2.cli.replay_run import main as delegated_main

        sys.argv = [f"track.v2.cli.{delegated_modules[command]}", *forwarded]
        return delegated_main()

    parser = argparse.ArgumentParser(description="Roadnet pipeline runtime")
    parser.add_argument("command", choices=("check-config", "serve"))
    parser.add_argument("--ask-password", action="store_true")
    args = parser.parse_args()
    config = load_startup_config(ask_password=args.ask_password)
    if args.command == "check-config":
        return check_config(config)
    return serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
