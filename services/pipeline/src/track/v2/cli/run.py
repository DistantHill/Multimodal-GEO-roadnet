"""CLI for the database-backed track.v2 pipeline.

The pipeline implementation lives in :mod:`track.v2.pipeline`; this module
keeps the historical command-line options and ``run_pipeline`` import seam.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from pathlib import Path

from ..config import PostgresConfig, default_postgres_config
from ..logger import init_logger, shutdown_logger
from ..pipeline import PipelineInput, PipelineResult, run_pipeline as _run_pipeline
from .pipeline_reporter import PipelineReporter


def run_pipeline(
    db_config: PostgresConfig,
    *,
    csv_path: str | None = None,
    pg_source_table: str | None = None,
    clean_id: str | None = None,
    table: str = "trajectory_points",
    config_path: str | Path | None = None,
    processing_srid: int | None = None,
) -> dict[str, int | str | list[int]]:
    """Compatibility adapter for the historical CLI runner signature."""
    pipeline_input = PipelineInput(
        db_config=db_config,
        csv_path=csv_path,
        pg_source_table=pg_source_table,
        clean_id=clean_id,
        table=table,
        config_path=config_path,
        processing_srid=processing_srid,
    )
    reporter = PipelineReporter()
    try:
        result: PipelineResult = _run_pipeline(pipeline_input, emit=reporter)
        return result.stats
    finally:
        reporter.close()


def _uuid_arg(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    # Force stdout/stderr to UTF-8 so CJK text survives pipe environments.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    _env = default_postgres_config()

    parser = argparse.ArgumentParser(
        description="Run track.v2 through Network."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--csv", metavar="PATH",
        help="从 CSV 文件读取输入轨迹点 / Read input trajectory points from a CSV file."
    )
    source_group.add_argument(
        "--pg-table", metavar="TABLE",
        help="从 PostgreSQL 表读取输入轨迹点 / Read input trajectory points from a PostgreSQL table."
    )
    source_group.add_argument(
        "--clean-id", metavar="UUID", type=_uuid_arg,
        help="复用一个已完成 Clean Run，只创建新的 Detect Run / Reuse one completed Clean Run."
    )
    parser.add_argument("--host", default=_env.host)
    parser.add_argument("--port", type=int, default=_env.port)
    parser.add_argument("--database", default=_env.database)
    parser.add_argument("--user", default=_env.user)
    parser.add_argument(
        "--ask-password", action="store_true",
        help="Prompt for password interactively (secure, no echo). Overrides .env password."
    )
    parser.add_argument("--connect-timeout", type=int, default=_env.connect_timeout)
    parser.add_argument("--sslmode", default=_env.sslmode)
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="pipeline 配置 TOML 路径 / pipeline config TOML path "
             "(default: track/v2/config/pipeline.toml)."
    )
    parser.add_argument(
        "--processing-srid", type=int, default=None, metavar="EPSG",
        help="本次新 Clean Run 的固定米制投影 EPSG / projected metre CRS for a new Clean Run.",
    )
    parser.add_argument(
        "--table", default=None,
        help="目标表名 / Target table name for trajectory_points "
             "(default: same as --pg-table for PG mode, 'trajectory_points' for CSV mode)."
    )
    args = parser.parse_args()

    if args.clean_id and args.table is not None:
        parser.error("--clean-id 不可覆盖 --table / cannot override --table")
    if args.clean_id and args.processing_srid is not None:
        parser.error(
            "--clean-id 继承已有投影，不可覆盖 --processing-srid / "
            "a reused Clean Run inherits its projection"
        )

    if args.table is None:
        args.table = args.pg_table or "trajectory_points"

    if args.ask_password:
        password = getpass.getpass("PostgreSQL password: ")
    else:
        password = _env.password

    db_config = PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )

    source_label = (
        f"clean:{args.clean_id}" if args.clean_id
        else args.csv or f"postgresql://{db_config.database}/{args.pg_table}"
    )
    log_path = init_logger()
    print(f"日志文件 / Log file: {log_path}")
    print(f"数据源 / Source: {source_label}")
    print(f"连接目标 / Target: {db_config.safe_dsn()}")

    try:
        stats = run_pipeline(
            db_config,
            csv_path=args.csv,
            pg_source_table=args.pg_table,
            clean_id=str(args.clean_id) if args.clean_id else None,
            table=args.table,
            config_path=args.config,
            processing_srid=args.processing_srid,
        )
        _print_write_summary(stats)
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"[FAIL] 流水线失败 / Pipeline failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        shutdown_logger()


def _print_write_summary(stats: dict[str, int | str | list[int]]) -> None:
    """Print model-driven Clip fields plus persisted table row counts."""
    print("\n── 写入统计 / Write Summary ──")
    for table in [
        "trajectory_points", "kept_trajectory_points",
        "injected_points", "tracks_cross", "turn_events", "candidate_intersections",
        "candidate_entry_exit_points",
        "intersections", "entry_exit_points",
    ]:
        if table in stats:
            print(f"  {table:40s} {stats[table]:>10,} rows")
    for name, value in stats.items():
        if name.startswith("clip_") and isinstance(value, int):
            print(f"  {name:40s} {value:>10,}")


if __name__ == "__main__":
    raise SystemExit(main())
