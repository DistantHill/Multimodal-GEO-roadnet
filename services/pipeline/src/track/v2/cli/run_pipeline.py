"""Schema-v3 CLI for the database-backed track.v2 Pipeline.

The implementation lives in :mod:`track.v2.orchestration.run_pipeline`; this module provides
the PostgreSQL ``source_table``/Refine replay entrypoint and reporting seam.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from ..config import PipelineConfig
from ..telemetry.console_logging import init_logger, shutdown_logger
from ..orchestration.run_pipeline import PipelineInput, PipelineResult, run_pipeline as _run_pipeline
from ._load_config import load_startup_config
from .report_pipeline_events import PipelineReporter
from .refine_selection import (
    format_refine_history,
    get_refine_selection,
    latest_completed_refine,
)


def run_pipeline(
    config: PipelineConfig,
    *,
    source_table: str | None = None,
    refine_id: str | None = None,
    selection: object | None = None,
) -> dict[str, float | int | str | list[int]]:
    """Run with a configuration object already loaded at the process boundary."""
    pipeline_input = PipelineInput(
        source_table=source_table,
        refine_id=refine_id,
        selection=selection,
    )
    reporter = PipelineReporter()
    try:
        result: PipelineResult = _run_pipeline(pipeline_input, config, emit=reporter)
        return result.stats
    finally:
        reporter.close()


def _uuid_arg(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def _bool_arg(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _ask_reuse(selection: object) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError(
            "发现已完成 Refine，但当前没有交互终端；请显式传 --refine-resume-last true 自动复用，或 false 新建"
        )
    while True:
        answer = input("是否复用此 Refine？[Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 y/yes 或 n/no，也可直接回车复用。")


def main() -> int:
    # Force stdout/stderr to UTF-8 so CJK text survives pipe environments.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Run track.v2 through Network."
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--source-table", metavar="TABLE",
        help="从 PostgreSQL 表读取输入轨迹点 / Read input trajectory points from a PostgreSQL table."
    )
    parser.add_argument(
        "--refine-resume-last",
        type=_bool_arg,
        default=None,
        metavar="true|false",
        help="有历史 Refine 时自动复用 (true) 或新建 (false)；单独传 true 复用全库最近完成的 Refine。",
    )
    source_group.add_argument(
        "--refine-id", metavar="UUID", type=_uuid_arg,
        help="复用一个已完成 Refine Run，只创建新的 Detect Run / Reuse one completed Refine Run."
    )
    parser.add_argument(
        "--ask-password", action="store_true",
        help="Prompt for password interactively instead of using POSTGRES_PASSWORD."
    )
    parser.add_argument(
        "--stats-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write counts and A65 phase timings as JSON after a successful run.",
    )
    parser.add_argument("--time-start", help="Inclusive ISO timestamp with timezone")
    parser.add_argument("--time-end", help="Exclusive ISO timestamp with timezone")
    args = parser.parse_args()
    selection = None
    if bool(args.time_start) != bool(args.time_end):
        parser.error("--time-start and --time-end must be provided together")
    if args.time_start:
        from ..experiments.config import TimeSelection, parse_time
        try:
            selection = TimeSelection(parse_time(args.time_start, "time-start"), parse_time(args.time_end, "time-end"))
        except ValueError as exc:
            parser.error(str(exc))

    if args.refine_id is not None and args.refine_resume_last is not None:
        parser.error("--refine-resume-last 不能与 --refine-id 配合使用")
    if args.source_table is None and args.refine_id is None and args.refine_resume_last is not True:
        parser.error("请指定 --source-table、--refine-id，或 --refine-resume-last true")

    config = load_startup_config(ask_password=args.ask_password)
    db_config = config.postgres

    selected_refine_id = str(args.refine_id) if args.refine_id else None
    selected_history = None
    if args.refine_resume_last is True or (
        args.source_table is not None and args.refine_resume_last is not False
    ):
        try:
            selected_history = latest_completed_refine(db_config, args.source_table)
        except Exception as exc:
            source_description = args.source_table or "全库"
            print(f"[FAIL] 查询来源 {source_description} 的已完成 Refine 失败: {exc}", file=sys.stderr)
            return 2
        if selected_history is None:
            if args.source_table is None:
                print("[FAIL] 全库没有已完成的 Refine Run。", file=sys.stderr)
                return 2
            print(f"未找到来源表 {args.source_table} 的已完成 Refine；将新建 Refine。")
        else:
            print("找到可复用的历史 Refine：")
            print(format_refine_history(selected_history))
            if args.refine_resume_last is True:
                selected_refine_id = selected_history.id
            else:
                try:
                    if _ask_reuse(selected_history):
                        selected_refine_id = selected_history.id
                        print(f"已选择复用 Refine Run {selected_refine_id}。")
                    else:
                        print("已选择新建 Refine。")
                except (RuntimeError, EOFError, KeyboardInterrupt) as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        print("\n已取消。", file=sys.stderr)
                        return 130
                    if isinstance(exc, EOFError):
                        print(
                            "交互输入遇到 EOF，已退出；请显式传 --refine-resume-last true 自动复用，或 false 新建。",
                            file=sys.stderr,
                        )
                    else:
                        print(str(exc), file=sys.stderr)
                    return 2
    elif selected_refine_id is not None:
        try:
            selected_history = get_refine_selection(db_config, selected_refine_id)
            print("显式指定的历史 Refine：")
            print(format_refine_history(selected_history))
        except Exception as exc:
            print(f"[FAIL] Refine Run {selected_refine_id}: {exc}", file=sys.stderr)
            return 2

    source_label = (
        f"refine:{selected_refine_id}" if selected_refine_id
        else f"postgresql://{db_config.database}/{args.source_table}"
    )
    log_path = init_logger(config.logging.directory)
    from track.v2.telemetry.memory_warning import MemoryWarningMonitor
    memory_warning = MemoryWarningMonitor()
    memory_warning.start()
    print(f"日志文件 / Log file: {log_path}")
    print(f"数据源 / Source: {source_label}")
    print(f"连接目标 / Target: {db_config.safe_dsn()}")

    try:
        stats = run_pipeline(
            config,
            source_table=args.source_table if selected_refine_id is None else None,
            refine_id=selected_refine_id,
            **({"selection": selection} if selection is not None else {}),
        )
        _print_write_summary(stats)
        if args.stats_json is not None:
            args.stats_json.parent.mkdir(parents=True, exist_ok=True)
            args.stats_json.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"  stats_json: {args.stats_json}")
        return 0
    except RuntimeError as exc:
        prefix = f"Refine Run {selected_refine_id}: " if selected_refine_id else ""
        print(prefix + str(exc))
        return 2
    except Exception as exc:
        prefix = f"Refine Run {selected_refine_id}: " if selected_refine_id else ""
        print(f"[FAIL] {prefix}流水线失败 / Pipeline failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        memory_warning.close()
        shutdown_logger()


def _print_write_summary(stats: dict[str, float | int | str | list[int]]) -> None:
    """Print model-driven Clip fields plus persisted table row counts."""
    print("\n── 写入统计 / Write Summary ──")
    for table in [
        "source_points", "kept_trajectory_points",
        "injected_points", "tracks_cross", "turn_events", "candidate_intersections",
        "candidate_entry_exit_points",
        "intersections", "entry_exit_points", "resolved_subtrack_points",
    ]:
        if table in stats:
            print(f"  {table:40s} {stats[table]:>10,} rows")
    for name, value in stats.items():
        if name.startswith("clip_") and isinstance(value, int):
            print(f"  {name:40s} {value:>10,}")
    for name in (
        "detect_compute_s",
        "detect_persist_s",
        "resolved_sequence_s",
        "clip_assembly_s",
        "_elapsed_s",
    ):
        value = stats.get(name)
        if isinstance(value, float):
            print(f"  {name:40s} {value:>10.3f} s")


if __name__ == "__main__":
    raise SystemExit(main())
