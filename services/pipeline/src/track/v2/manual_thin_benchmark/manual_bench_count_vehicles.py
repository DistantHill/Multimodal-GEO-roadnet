"""Diagnose COUNT(DISTINCT vehicle_id) performance on trajectory_points (20M rows).

ZH: 诊断 20M 行 trajectory_points 表上 COUNT(DISTINCT vehicle_id) 的性能瓶颈。
    对比 4 种方案的耗时：
      A) 当前实现：子查询包裹 COUNT(DISTINCT vehicle_id)
      B) 直接查表（跳过子查询）
      C) 利用 pg_stats 近似值（适合进度条）
      D) 索引优化后的查询（如存在 idx on vehicle_id）

用法：
    python -m track.v2.manual_thin_benchmark.manual_bench_count_vehicles
    可选环境变量覆盖：HOST, PORT, DATABASE, USER, PASSWORD, TABLE

EN: Diagnose why COUNT(DISTINCT vehicle_id) takes 40s on a 20M-row table.
    Compares 4 approaches with EXPLAIN ANALYZE and wall-clock timing.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import psycopg


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


CONFIG = {
    "host": _env("PGHOST", _env("HOST", "127.0.0.1")),
    "port": int(_env("PGPORT", _env("PORT", "15432"))),
    "database": _env("PGDATABASE", _env("DATABASE", "trajectory")),
    "user": _env("PGUSER", _env("USER", "track_user")),
    "password": _env("PGPASSWORD", _env("PASSWORD", "")),
    "table": _env("PGTABLE", _env("TABLE", "trajectory_points")),
}


def _connect() -> Any:
    cfg = CONFIG
    return psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["user"],
        password=cfg["password"] or None,
    )


def _run(conn: Any, label: str, sql: str, params: tuple = ()) -> tuple[float, Any]:
    """Execute SQL, print EXPLAIN ANALYZE, return (wall_seconds, result)."""
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")

    # 1) EXPLAIN ANALYZE (may take as long as the real query)
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN ANALYZE {sql}", params)
        plan_rows = cur.fetchall()
    t_explain = time.perf_counter() - t0
    # Print last 8 lines of plan (most relevant)
    for row in plan_rows[-8:]:
        print(f"  {row[0]}")
    print(f"  --- EXPLAIN ANALYZE wall: {t_explain:.1f}s ---")

    # 2) Plain wall clock (second run, likely cached)
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        result = cur.fetchone()
    t_wall = time.perf_counter() - t0
    print(f"  >>> Wall clock: {t_wall:.2f}s  Result: {result[0] if result else 'NULL'}")
    return t_wall, result


def _check_indexes(conn: Any, table: str) -> None:
    """Print existing indexes on the table."""
    print(f"\n{'='*70}")
    print(f"  Existing indexes on {table}")
    print(f"{'='*70}")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = %s
            ORDER BY indexname
            """,
            (table,),
        )
        rows = cur.fetchall()
        if not rows:
            print("  (none)")
            return
        for name, definition in rows:
            # Highlight vehicle_id indexes
            marker = "  *** VEHICLE_ID INDEX ***" if "vehicle_id" in definition.lower() else ""
            print(f"  {name}: {definition}{marker}")


def _check_table_stats(conn: Any, table: str) -> None:
    """Print pg_stats info for vehicle_id + table size estimate."""
    print(f"\n{'='*70}")
    print(f"  Table stats: {table}")
    print(f"{'='*70}")
    with conn.cursor() as cur:
        # Row count estimate
        cur.execute(
            "SELECT c.reltuples::bigint, pg_size_pretty(pg_total_relation_size(c.oid)) "
            "FROM pg_class c WHERE c.relname = %s",
            (table,),
        )
        row2 = cur.fetchone()
        if row2:
            est_rows, total_size = row2
            print(f"  Estimated rows: {est_rows:,}")
            print(f"  Total relation size: {total_size}")

        # n_distinct from pg_stats
        cur.execute(
            "SELECT n_distinct, most_common_vals IS NOT NULL AS has_mcv "
            "FROM pg_stats WHERE tablename = %s AND attname = 'vehicle_id'",
            (table,),
        )
        row = cur.fetchone()
        if row:
            nd, has_mcv = row
            print(f"  pg_stats.n_distinct(vehicle_id): {nd}  has_mcv={has_mcv}")
            if nd and nd > 0:
                print(f"    → approximate vehicle count ≈ {int(nd):,}")
        else:
            print("  pg_stats: NO STATS for vehicle_id (run ANALYZE first!)")

        # Last ANALYZE time
        cur.execute(
            "SELECT last_analyze, last_autoanalyze FROM pg_stat_user_tables WHERE relname = %s",
            (table,),
        )
        row = cur.fetchone()
        if row:
            print(f"  last_analyze: {row[0]}  last_autoanalyze: {row[1]}")


def main() -> None:
    table = CONFIG["table"]
    source_query = f"SELECT * FROM {table} ORDER BY vehicle_id, timestamp"

    # ── connect once ──
    conn = _connect()
    conn.execute("SET statement_timeout TO '5min'")
    conn.execute("SET idle_in_transaction_session_timeout TO '5min'")

    try:
        _check_indexes(conn, table)
        _check_table_stats(conn, table)

        print(f"\n{'#'*70}")
        print(f"#  PERFORMANCE COMPARISON")
        print(f"#  Source query: {source_query}")
        print(f"{'#'*70}")

        # ── A) Current implementation: subquery wrap ──
        sql_a = (
            f"SELECT COUNT(DISTINCT vehicle_id) "
            f"FROM ({source_query}) AS _count_sub"
        )
        t_a, res_a = _run(conn, "A) CURRENT: subquery-wrapped COUNT(DISTINCT vehicle_id)", sql_a)

        # ── B) Direct table query (no subquery) ──
        sql_b = f"SELECT COUNT(DISTINCT vehicle_id) FROM {table}"
        t_b, res_b = _run(conn, "B) DIRECT: COUNT(DISTINCT vehicle_id) on table (no subquery)", sql_b)

        # ── C) pg_stats approximation ──
        sql_c = (
            f"SELECT COALESCE(n_distinct, 0)::bigint "
            f"FROM pg_stats "
            f"WHERE tablename = %s AND attname = 'vehicle_id'"
        )
        t_c, res_c = _run(conn, "C) APPROX: pg_stats.n_distinct (instant, good for progress bar)", sql_c, (table,))

        # ── D) Check if vehicle_id index exists and use it ──
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = %s AND indexdef ILIKE '%%vehicle_id%%' LIMIT 1",
                (table,),
            )
            idx_row = cur.fetchone()
        if idx_row:
            print(f"\n  --- Index found: {idx_row[0][:100]}... ---")
        else:
            print(f"\n  --- NO vehicle_id index found. Run: ---")
            print(f"  CREATE INDEX CONCURRENTLY idx_{table}_vehicle ON {table} (vehicle_id);")
            print(f"  --- Then ANALYZE {table}; and rerun this benchmark ---")

        # ── Summary ──
        print(f"\n{'#'*70}")
        print(f"#  SUMMARY (wall clock)")
        print(f"{'#'*70}")
        print(f"  A) Subquery wrap : {t_a:6.1f}s")
        print(f"  B) Direct table  : {t_b:6.1f}s")
        print(f"  C) pg_stats      : {t_c:6.3f}s  ← {'✓ SUITABLE for progress bar' if t_c < 0.01 else '✗ UNEXPECTED'}")
        print(f"  Speedup C vs A  : {t_a/t_c:,.0f}x" if t_c > 0 else "  Speedup: infinite")

        if t_a > 5.0:
            print(f"\n  ⚠  {t_a:.0f}s for vehicle count is SLOW for a progress bar.")
            print(f"  → Recommendation: use pg_stats approximation (option C)")
            print(f"  → OR: CREATE INDEX CONCURRENTLY ON {table} (vehicle_id)")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
