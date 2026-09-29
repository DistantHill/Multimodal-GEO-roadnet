"""Import CSV trajectory data into the ``trajectory_points`` table.

ZH: 独立的 CSV → trajectory_points 导入 CLI。只做 Parser 解析 + 入库，
    不运行后续精炼/交叉口检测阶段。

EN: Standalone CSV → trajectory_points import CLI. Runs only Parser parsing +
    insert, without downstream cleaning/intersection stages.

Usage / 用法::

    python -m track.v2.cli.import_trajectories --csv track/data/guiji_20230301_top10w.csv --ask-password
    python -m track.v2.cli.import_trajectories --csv data.csv --ask-password
"""

from __future__ import annotations

import argparse
from itertools import chain, groupby, islice
import time
from typing import Iterable, Iterator

from ..config import connect as pg_connect
from ..telemetry.console_logging import init_logger, shutdown_logger
from ..models import CsvSource, SourceBatch
from ..trajectories.read_source import get_database
from ..db.insert_trajectory_points import copy_trajectory_point_batches
from ._load_config import load_startup_config


# ── Import logic ─────────────────────────────────────────────────────


def _iter_vehicle_groups(
    batches: Iterable[SourceBatch], *, vehicles_per_group: int = 10_000,
) -> Iterator[Iterator[SourceBatch]]:
    """Yield one lazy stream of batches for each group of vehicles."""
    if vehicles_per_group <= 0:
        raise ValueError("vehicles_per_group must be positive")
    by_vehicle = groupby(batches, key=lambda batch: batch.vehicle_id)
    while True:
        first = next(by_vehicle, None)
        if first is None:
            return
        vehicles = chain((first,), islice(by_vehicle, vehicles_per_group - 1))
        yield (batch for _, vehicle_batches in vehicles for batch in vehicle_batches)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import CSV trajectory data into the trajectory_points table."
    )
    parser.add_argument("--csv", required=True, help="Path to input CSV file")
    parser.add_argument(
        "--ask-password", action="store_true",
        help="Prompt for password interactively instead of using POSTGRES_PASSWORD."
    )
    parser.add_argument(
        "--table", default="trajectory_points",
        help="目标表名 / Target table name (default: trajectory_points)."
    )
    args = parser.parse_args()

    pc = load_startup_config(ask_password=args.ask_password)
    db_config = pc.postgres

    init_logger(pc.logging.directory)

    print(f"数据源 / Source CSV: {args.csv}")
    print(f"连接目标 / Target:    {db_config.safe_dsn()}")
    print(f"目标表  / Table:      {args.table}")

    handle = None
    try:
        t_start = time.perf_counter()

        # ── Parser: read CSV ──
        source = CsvSource(path=args.csv)
        handle = get_database(
            source,
            field_config=pc.parser.fields,
            runtime=pc.runtime,
            hash_csv_rows=False,
        )

        # ── Stream insert: one COPY per 10,000 vehicles ──
        total_parsed = 0
        total_written = 0

        with pg_connect(db_config) as conn:
            batches = handle.iter_vehicle_batches(
                max_batch_size=pc.runtime.import_batch_size,
            )
            for group in _iter_vehicle_groups(batches):
                t0 = time.perf_counter()
                written = copy_trajectory_point_batches(conn, group, table=args.table)
                total_written += written
                total_parsed += written
                print(f"  ... 已解析 / parsed: {total_parsed:>12,}  "
                      f"已写入 / written: {total_written:>12,}  "
                      f"({time.perf_counter() - t0:.1f}s per vehicle group)")

            conn.commit()

        t_total = time.perf_counter() - t_start
        skipped = total_parsed - total_written
        print(f"\n  解析 / Parsed: {total_parsed:,} 点/points")
        print(f"  写入 / Wrote:  {total_written:,} rows")
        if skipped > 0:
            print(f"  跳过 / Skipped: {skipped:,}")
        print(f"\n── 完成 / Done ({t_total:.1f}s) ──")

    except RuntimeError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"[FAIL] 导入失败 / Import failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if handle is not None:
            handle.close()
        shutdown_logger()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
