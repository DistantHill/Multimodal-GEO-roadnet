from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelineConfig:
    data_root: Path
    parquet_root: Path
    duckdb_root: Path
    polars_root: Path
    postgres_host: str
    qdrant_host: str

    @classmethod
    def from_environment(cls) -> "PipelineConfig":
        data_root = Path(os.getenv("DATA_ROOT", "/data"))
        parquet_root = (
            Path(os.environ["PARQUET_ROOT"])
            if "PARQUET_ROOT" in os.environ
            else data_root / "parquet"
        )
        return cls(
            data_root=data_root,
            parquet_root=parquet_root,
            duckdb_root=data_root / "duckdb",
            polars_root=data_root / "polars",
            postgres_host=os.getenv("POSTGRES_HOST", "postgres"),
            qdrant_host=os.getenv("QDRANT_HOST", "qdrant"),
        )


def check_config(config: PipelineConfig) -> int:
    print(f"DATA_ROOT={config.data_root}")
    print(f"PARQUET_ROOT={config.parquet_root}")
    print(f"DUCKDB_ROOT={config.duckdb_root}")
    print(f"POLARS_ROOT={config.polars_root}")
    print(f"POSTGRES_HOST={config.postgres_host}")
    print(f"QDRANT_HOST={config.qdrant_host}")
    roots = {
        "DATA_ROOT": config.data_root,
        "PARQUET_ROOT": config.parquet_root,
        "DUCKDB_ROOT": config.duckdb_root,
        "POLARS_ROOT": config.polars_root,
    }
    if any(not path.is_absolute() for path in roots.values()):
        print("ERROR: runtime roots must be absolute paths")
        return 1
    try:
        data_root = config.data_root.resolve(strict=False)
        for name in ("DUCKDB_ROOT", "POLARS_ROOT"):
            roots[name].resolve(strict=False).relative_to(data_root)
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
    if len(sys.argv) > 1 and sys.argv[1] in {"artifacts", "init-db", "run", "semantic-diff"}:
        command, forwarded = sys.argv[1], sys.argv[2:]
        if command == "init-db":
            from track.v2.cli.init_db import main as delegated_main
        elif command == "artifacts":
            from track.v2.cli.artifacts import main as delegated_main
        elif command == "run":
            from track.v2.cli.run import main as delegated_main
        else:
            from track.v2.cli.semantic_diff import main as delegated_main

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
