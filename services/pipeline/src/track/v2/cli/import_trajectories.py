"""Import CSV trajectory data into the ``trajectory_points`` table.

ZH: 独立的 CSV → trajectory_points 导入 CLI。只做 Parser 0 解析 + 入库，
    不运行后续清洗/交叉口检测阶段。重复的 ``(source_id, source_row_key)``
    会因 UNIQUE 约束自动跳过（ON CONFLICT DO NOTHING）。

EN: Standalone CSV → trajectory_points import CLI. Runs only Parser 0 parsing +
    insert, without downstream cleaning/intersection stages. Duplicate
    ``(source_id, source_row_key)`` rows are silently skipped via the UNIQUE
    constraint.

Usage / 用法::

    python -m track.v2.cli.import_trajectories --csv track/data/guiji_20230301_top10w.csv --ask-password
    python -m track.v2.cli.import_trajectories --csv data.csv --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
"""

from __future__ import annotations

import argparse
import getpass
import time

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..models import CsvSource
from .. import get_database
from ..db_writer import insert_trajectory_points


# ── Import logic ─────────────────────────────────────────────────────


def main() -> int:
    # ZH: 从 .env 加载默认连接参数，CLI 参数可覆盖
    # EN: Load defaults from .env; CLI args take precedence
    _env = default_postgres_config()

    parser = argparse.ArgumentParser(
        description="Import CSV trajectory data into the trajectory_points table."
    )
    parser.add_argument("--csv", required=True, help="Path to input CSV file")
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
        "--table", default="trajectory_points",
        help="目标表名 / Target table name (default: trajectory_points)."
    )
    args = parser.parse_args()

    # ── Resolve password: CLI prompt > .env > empty ──
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

    print(f"数据源 / Source CSV: {args.csv}")
    print(f"连接目标 / Target:    {db_config.safe_dsn()}")

    try:
        # ── Parser 0: read CSV ──
        t0 = time.perf_counter()
        source = CsvSource(path=args.csv)
        handle = get_database(source)
        all_points = list(handle.iter_points())
        handle.close()
        t_read = time.perf_counter() - t0
        print(f"  解析 / Parsed: {len(all_points):,} 点/points ({t_read:.1f}s)")

        if not all_points:
            print("  无数据 / No data to import.")
            return 0

        # ── Insert ──
        with pg_connect(db_config) as conn:
            t0 = time.perf_counter()
            tp_id_map = insert_trajectory_points(conn, all_points, table=args.table)
            conn.commit()
            t_write = time.perf_counter() - t0
            print(f"  写入 / Wrote:  {len(tp_id_map):,} rows ({t_write:.1f}s)")

            skipped = len(all_points) - len(tp_id_map)
            if skipped > 0:
                print(f"  跳过 / Skipped: {skipped:,} (重复/duplicate source_id+source_row_key)")

            t_total = time.perf_counter() - (t0 - t_write)
            print(f"\n── 完成 / Done ({t_read + t_write:.1f}s) ──")

    except RuntimeError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"[FAIL] 导入失败 / Import failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
