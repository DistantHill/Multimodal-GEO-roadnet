"""薄壳脚本：从 postgres.day01 取前 N 行覆盖 track_test.trajectory_points。

ZH: 使用源端命名游标 + 目标端 COPY FROM STDIN 流式传输，
    避免 Python 内存中物化全部数据。映射所有公共列。

EN: Uses source named-cursor + target COPY FROM STDIN to stream rows
    without materializing everything in Python memory.

用法 / Usage::

    python track/v2/manual_thin_benchmark/manual_bench_seed_1m.py
    python track/v2/manual_thin_benchmark/manual_bench_seed_1m.py --limit 500000
    python track/v2/manual_thin_benchmark/manual_bench_seed_1m.py --no-truncate  # 追加而非覆盖
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from uuid import uuid4

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import psycopg

from track.v2.manual_thin_benchmark._thin_bench_helpers import load_test_env, TEST_DB_NAME


# ── day01 → trajectory_points 列映射 ──
# ZH: 源列名和目标列名一致，按 SELECT/INSERT 顺序列出。
#     id/geom/created_at 由目标端自动生成。
_COPY_COLUMNS = [
    "source_id",
    "source_row_key",
    "stable",
    "is_active",
    "vehicle_id",
    "timestamp",
    "lon",
    "lat",
    "heading",
    "speed",
    "extra",
]

# 每 10 万行打印一次进度
_PROGRESS_EVERY = 100_000


def _load_env() -> dict[str, str]:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    env: dict[str, str] = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _format_value(v: object) -> str:
    """Format a Python value for COPY TEXT format.

    ZH: COPY TEXT 格式规则：
        - None → \\N
        - str → 保持原样（制表符/换行符已在 JSONB extra 中被 psycopg 处理）
        - bool → t / f
        - float → repr（保留精度）
        - dict → JSON 字符串
        - 其他 → str()
    """
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dict):
        # JSONB columns — dump as JSON string
        import json
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def _format_row(row: tuple, col_count: int) -> str:
    """Format one DB row as a COPY TEXT line (tab-separated, newline-terminated)."""
    parts: list[str] = []
    for i in range(col_count):
        parts.append(_format_value(row[i]))
    return "\t".join(parts) + "\n"


def run_migration(
    src_conn: psycopg.Connection,
    tgt_conn: psycopg.Connection,
    limit: int,
) -> int:
    """Stream rows from source → target via COPY. Returns total rows written."""
    cols = ", ".join(_COPY_COLUMNS)
    insert_sql = f"COPY trajectory_points ({cols}) FROM STDIN WITH (FORMAT TEXT)"

    t0 = time.perf_counter()
    total_rows = 0
    byte_count = 0
    next_report = _PROGRESS_EVERY

    # ── 源端：命名游标，单次 SELECT ──
    src_cur = src_conn.cursor(name=f"seed_1m_{uuid4().hex}")
    src_cur.execute(
        f"SELECT {cols} FROM day01 ORDER BY vehicle_id, timestamp LIMIT %s",
        (limit,),
    )
    col_count = len(src_cur.description or [])

    # ── 目标端：单次 COPY FROM STDIN ──
    with tgt_conn.cursor() as tgt_cur:
        with tgt_cur.copy(insert_sql) as copy:
            for row in src_cur:
                line = _format_row(row, col_count)
                copy.write(line.encode("utf-8"))
                byte_count += len(line)
                total_rows += 1

                if total_rows >= next_report:
                    elapsed = time.perf_counter() - t0
                    mb = byte_count / (1024 * 1024)
                    print(
                        f"  {total_rows:>9,} rows  "
                        f"| {mb:6.1f} MB  "
                        f"| {elapsed:5.1f}s  "
                        f"| {total_rows / elapsed:,.0f} rows/s"
                    )
                    next_report += _PROGRESS_EVERY

    src_cur.close()
    tgt_conn.commit()
    elapsed = time.perf_counter() - t0
    mb = byte_count / (1024 * 1024)

    print(
        f"  {'─' * 50}\n"
        f"  Total: {total_rows:,} rows  |  {mb:.1f} MB  |  {elapsed:.1f}s  "
        f"|  {total_rows / elapsed:,.0f} rows/s"
    )
    return total_rows


def verify(conn: psycopg.Connection) -> None:
    """打印目标表行数和示例行。"""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trajectory_points")
        count = cur.fetchone()[0]
        print(f"\n  目标表行数 / Target rows: {count:,}")

        cur.execute("SELECT MIN(id), MAX(id) FROM trajectory_points")
        min_id, max_id = cur.fetchone()
        print(f"  id 范围 / id range: {min_id:,} ~ {max_id:,}")

        cur.execute("""
            SELECT vehicle_id, timestamp, lon, lat, heading, speed
            FROM trajectory_points
            ORDER BY vehicle_id, timestamp
            LIMIT 3
        """)
        print("  前 3 行 / First 3 rows:")
        for row in cur.fetchall():
            print(f"    {row[0]:20s} ts={row[1]:.1f}  ({row[2]:.6f}, {row[3]:.6f})  h={row[4]:.0f}  v={row[5]:.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 postgres.day01 复制 N 行到 track_test.trajectory_points"
    )
    parser.add_argument(
        "--limit", type=int, default=1_000_000,
        help="要复制的行数（默认 1000000）"
    )
    parser.add_argument(
        "--no-truncate", action="store_true",
        help="不清空目标表——追加数据"
    )
    args = parser.parse_args()
    limit: int = args.limit

    env = _load_env()
    total_start = time.perf_counter()

    print("=" * 66)
    print("  数据迁移: postgres.day01 → track_test.trajectory_points")
    print(f"  目标行数: {limit:,}")
    print(f"  模式: {'追加/Append' if args.no_truncate else '覆盖/Replace'}")
    print("=" * 66)

    # ── 连接 ──
    t0 = time.perf_counter()
    src_conn = psycopg.connect(
        host=env["TRACK_PG_HOST"],
        port=env["TRACK_PG_PORT"],
        dbname="postgres",
        user=env["TRACK_PG_USER"],
        password=env["TRACK_PG_PASSWORD"],
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
    )
    test_env = load_test_env()
    tgt_conn = psycopg.connect(
        host=test_env["TRACK_PG_HOST"],
        port=test_env["TRACK_PG_PORT"],
        dbname=TEST_DB_NAME,
        user=test_env["TRACK_PG_USER"],
        password=test_env["TRACK_PG_PASSWORD"],
        connect_timeout=int(test_env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
    )
    print(f"[OK] 已连接 / Connected ({time.perf_counter() - t0:.2f}s)")

    try:
        # ── Step 1: 清空目标表 ──
        if not args.no_truncate:
            t0 = time.perf_counter()
            with tgt_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM trajectory_points")
                before = cur.fetchone()[0]
                cur.execute("TRUNCATE trajectory_points RESTART IDENTITY CASCADE")
            tgt_conn.commit()
            print(f"\n[Step 1] 清空目标表 / Truncate target")
            print(f"  删除 / Deleted: {before:,} rows ({time.perf_counter() - t0:.2f}s)")

        # ── Step 2: 源端禁用超时 + 流式 COPY ──
        print(f"\n[Step 2] 流式迁移 / Streaming migration")

        # 禁用源端超时（named cursor 长事务）
        with src_conn.cursor() as cur:
            cur.execute("SET statement_timeout TO '0'")
            cur.execute("SET idle_in_transaction_session_timeout TO '0'")

        total_rows = run_migration(src_conn, tgt_conn, limit)

        elapsed = time.perf_counter() - total_start
        print(f"\n{'=' * 66}")
        print(f"  [OK] 迁移完成 / Migration complete")
        print(f"  总行数 / Total rows: {total_rows:,}")
        print(f"  总耗时 / Total time: {elapsed:.1f}s")
        print(f"  吞吐量 / Throughput: {total_rows / elapsed:,.0f} rows/s")
        print(f"{'=' * 66}")

        # ── Step 3: ANALYZE ──
        t0 = time.perf_counter()
        with tgt_conn.cursor() as cur:
            cur.execute("ANALYZE trajectory_points")
        tgt_conn.commit()
        print(f"\n[Step 3] ANALYZE 完成 ({time.perf_counter() - t0:.1f}s)")

        # ── Step 4: 验证 ──
        verify(tgt_conn)

    finally:
        src_conn.close()
        tgt_conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
