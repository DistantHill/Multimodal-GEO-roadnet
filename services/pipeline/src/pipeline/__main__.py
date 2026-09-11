from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelineConfig:
    data_root: Path
    parquet_root: Path
    postgres_host: str
    qdrant_host: str

    @classmethod
    def from_environment(cls) -> "PipelineConfig":
        return cls(
            data_root=Path(os.getenv("DATA_ROOT", "/data")),
            parquet_root=Path(os.getenv("PARQUET_ROOT", "/data/parquet")),
            postgres_host=os.getenv("POSTGRES_HOST", "postgres"),
            qdrant_host=os.getenv("QDRANT_HOST", "qdrant"),
        )


def check_config(config: PipelineConfig) -> int:
    print(f"DATA_ROOT={config.data_root}")
    print(f"PARQUET_ROOT={config.parquet_root}")
    print(f"POSTGRES_HOST={config.postgres_host}")
    print(f"QDRANT_HOST={config.qdrant_host}")
    if not config.data_root.exists():
        print(f"ERROR: DATA_ROOT does not exist: {config.data_root}")
        return 1
    return 0


def serve(config: PipelineConfig) -> int:
    """Keep the independent batch runtime available in the Compose stack."""

    print("Pipeline runtime ready. Start a run with `python -m pipeline run ...`.")
    print(f"Runtime data root: {config.data_root}")
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
    if len(sys.argv) > 1 and sys.argv[1] in {"init-db", "run", "cv-preview", "cv-tune"}:
        command, forwarded = sys.argv[1], sys.argv[2:]
        if command == "init-db":
            from track.v2.cli.init_db import main as delegated_main
        elif command == "cv-preview":
            from track.v2.cli.cv_preview import main as delegated_main
        elif command == "cv-tune":
            from track.v2.cli.cv_tune import main as delegated_main
        else:
            from track.v2.cli.run import main as delegated_main

        sys.argv = [f"track.v2.cli.{command.replace('-', '_')}", *forwarded]
        return delegated_main()

    parser = argparse.ArgumentParser(description="Roadnet pipeline runtime")
    parser.add_argument("command", choices=("check-config", "serve"))
    args = parser.parse_args()
    config = PipelineConfig.from_environment()
    if args.command == "check-config":
        return check_config(config)
    return serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
