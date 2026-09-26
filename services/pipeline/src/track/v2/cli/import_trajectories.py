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
import time

from ..config import connect as pg_connect
from ..telemetry.console_logging import init_logger, shutdown_logger
from ..models import CsvSource
from ..trajectories.read_source import get_database
from ..db.insert_trajectory_points import insert_trajectory_points
from ._load_config import load_startup_config


# ── Import logic ─────────────────────────────────────────────────────


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

    try:
        t_start = time.perf_counter()

        # ── Parser: read CSV ──
        source = CsvSource(path=args.csv)
        handle = get_database(
            source,
            field_config=pc.parser.fields,
            runtime=pc.runtime,
        )

        # ── Stream insert: 每批 BATCH_SIZE 行边读边写 / batch-insert as we read ──
        total_parsed = 0
        total_written = 0

        with pg_connect(db_config) as conn:
            batch: list = []
            for point in handle.iter_points():
                batch.append(point)
                if len(batch) >= pc.runtime.import_batch_size:
                    t0 = time.perf_counter()
                    tp_id_map = insert_trajectory_points(conn, batch, table=args.table)
                    written = len(tp_id_map)
                    total_written += written
                    total_parsed += len(batch)
                    elapsed = time.perf_counter() - t0
                    print(f"  ... 已解析 / parsed: {total_parsed:>12,}  "
                          f"已写入 / written: {total_written:>12,}  "
                          f"({elapsed:.1f}s per batch)")
                    batch.clear()

            # ── Final batch ──
            if batch:
                t0 = time.perf_counter()
                tp_id_map = insert_trajectory_points(conn, batch, table=args.table)
                written = len(tp_id_map)
                total_written += written
                total_parsed += len(batch)
                elapsed = time.perf_counter() - t0
                print(f"  ... 已解析 / parsed: {total_parsed:>12,}  "
                      f"已写入 / written: {total_written:>12,}  "
                      f"({elapsed:.1f}s final batch)")

            conn.commit()

        handle.close()

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
        shutdown_logger()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
