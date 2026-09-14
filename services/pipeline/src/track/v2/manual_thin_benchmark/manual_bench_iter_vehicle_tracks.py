"""薄壳脚本：量化 iter_vehicle_tracks() 各环节性能 + SQL 层诊断。

ZH: 对 StreamVehicleSource.iter_vehicle_tracks() 做分步计时，同时诊断
    PG 模式下 SQL 查询本身的数据库内性能（执行计划、索引使用、FETCH 成本）。
    支持 CSV 和 PG 两种数据源对比。

测试维度 / Test dimensions:
  A) SQL 层诊断（仅 PG）—— EXPLAIN ANALYZE、索引检查、表统计信息
  B) iter_vehicle_tracks() —— 完整车辆分组迭代吞吐量
  C) iter_points() —— 绕过车辆分组，测纯 parse + I/O 吞吐量
  D) 分组开销 = B - C

用法 / Usage::

    # PG 模式（默认，使用 track_test 数据库）
    python -m track.v2.manual_thin_benchmark.manual_bench_iter_vehicle_tracks

    # CSV 模式
    python -m track.v2.manual_thin_benchmark.manual_bench_iter_vehicle_tracks --csv path/to/data.csv

    # 限制行数/车辆数
    python -m track.v2.manual_thin_benchmark.manual_bench_iter_vehicle_tracks --max-rows 500000
    python -m track.v2.manual_thin_benchmark.manual_bench_iter_vehicle_tracks --max-vehicles 5000

    # 自定义源表（PG 模式）
    TRACK_BENCH_SOURCE_TABLE=trajectory_points python -m ...

    # 生产库
    TRACK_BENCH_DB=postgres python -m ...

环境变量 / Environment variables:
    TRACK_BENCH_DB              PG 数据库名（默认 track_test）
    TRACK_BENCH_SOURCE_TABLE    PG 源表名（默认 trajectory_points）
    TRACK_BENCH_VERBOSE         是否逐窗打印（默认 1）
    TRACK_BENCH_MAX_ROWS        最大行数（0=不限制）
    TRACK_BENCH_MAX_VEHICLES    最大车辆数（0=不限制）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import closing

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from track.v2.models import CsvSource, PostgresSource
from track.v2.parser import (
    StreamVehicleSource,
    get_database,
    source_row_hash,
)
# ZH: 我们不会直接 import psycopg —— PG 模式通过 helpers 连接。
#     仅在 SQL 诊断环节按需导入。

from track.v2.manual_thin_benchmark._thin_bench_helpers import (
    connect_test_db,
    load_test_env,
    TEST_DB_NAME,
)

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG —— 命令行参数覆盖
# ═══════════════════════════════════════════════════════════════════════════

# 默认值，会被命令行或环境变量覆盖
CFG = {
    "source_type": "pg",           # "pg" | "csv"
    "csv_path": "",
    "pg_source_table": os.environ.get("TRACK_BENCH_SOURCE_TABLE", "trajectory_points"),
    "max_rows": int(os.environ.get("TRACK_BENCH_MAX_ROWS", "0")),
    "max_vehicles": int(os.environ.get("TRACK_BENCH_MAX_VEHICLES", "0")),
    "verbose": os.environ.get("TRACK_BENCH_VERBOSE", "1") == "1",
    "report_every_n_vehicles": 500,  # 每 N 辆车打印一次进度
}


# ═══════════════════════════════════════════════════════════════════════════
# A) SQL 层诊断（仅 PG 模式）
# ═══════════════════════════════════════════════════════════════════════════

def _pg_sql_diagnostics(source_table: str) -> None:
    """诊断 PG 查询的数据库内性能：执行计划、索引、表统计。

    ZH: 运行 EXPLAIN ANALYZE 看实际执行计划，检查关键索引是否存在，
        打印 pg_stats 中的行数估算和 vehicle_id 区分度。
    """
    import psycopg

    env = load_test_env()
    conn = psycopg.connect(
        host=env["TRACK_PG_HOST"],
        port=env["TRACK_PG_PORT"],
        dbname=TEST_DB_NAME,
        user=env["TRACK_PG_USER"],
        password=env["TRACK_PG_PASSWORD"],
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
    )
    conn.execute("SET statement_timeout TO '2min'")

    try:
        source_query = f"SELECT * FROM {source_table} ORDER BY vehicle_id, timestamp"

        # ── A1: 表统计信息 ──
        print(f"\n{'─' * 72}")
        print("  [A1] 表统计信息 / Table Statistics")
        print(f"{'─' * 72}")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.reltuples::bigint AS est_rows, "
                "pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size, "
                "pg_size_pretty(pg_relation_size(c.oid)) AS data_size "
                "FROM pg_class c WHERE c.relname = %s",
                (source_table,),
            )
            row = cur.fetchone()
            if row:
                print(f"  估算行数 / Est. rows   : {row[0]:,}")
                print(f"  总大小   / Total size   : {row[1]}")
                print(f"  数据大小 / Data size    : {row[2]}")

            cur.execute(
                "SELECT last_analyze, last_autoanalyze, n_live_tup, n_dead_tup "
                "FROM pg_stat_user_tables WHERE relname = %s",
                (source_table,),
            )
            row = cur.fetchone()
            if row:
                print(f"  last_analyze     : {row[0]}")
                print(f"  last_autoanalyze : {row[1]}")
                print(f"  live tuples      : {row[2]:,}")
                print(f"  dead tuples      : {row[3]:,}")

            # pg_stats for vehicle_id
            cur.execute(
                "SELECT n_distinct, most_common_vals IS NOT NULL AS has_mcv, "
                "null_frac, avg_width "
                "FROM pg_stats WHERE tablename = %s AND attname = 'vehicle_id'",
                (source_table,),
            )
            row = cur.fetchone()
            if row:
                nd, has_mcv, null_frac, avg_w = row
                print(f"  pg_stats.vehicle_id.n_distinct : {nd}")
                print(f"    → 估算车辆数 / Est. vehicles ≈ {int(nd) if nd and nd > 0 else '?'}")
                print(f"    null_frac={null_frac:.4f}, avg_width={avg_w}")
                if not has_mcv:
                    print(f"    ⚠ 无 MCV 列表——可能是高基数列或统计不完整")
            else:
                print(f"  ⚠ pg_stats 中无 vehicle_id 条目（需 ANALYZE {source_table}）")

        # ── A2: 索引检查 ──
        print(f"\n{'─' * 72}")
        print("  [A2] 索引检查 / Index Check")
        print(f"{'─' * 72}")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = %s ORDER BY indexname",
                (source_table,),
            )
            indexes = cur.fetchall()
            if not indexes:
                print("  (无索引 / No indexes)")
            else:
                has_vehicle_idx = False
                has_vehicle_ts_idx = False
                for name, definition in indexes:
                    marker = ""
                    if "vehicle_id" in definition.lower():
                        has_vehicle_idx = True
                        if "timestamp" in definition.lower():
                            has_vehicle_ts_idx = True
                            marker = "  ★ 覆盖 ORDER BY vehicle_id, timestamp"
                        else:
                            marker = "  ✓ 覆盖 vehicle_id"
                    print(f"  {name}")
                    print(f"    {definition}{marker}")

                if not has_vehicle_ts_idx:
                    print(f"\n  ⚠ 缺少 (vehicle_id, timestamp) 复合索引！")
                    print(f"    ORDER BY vehicle_id, timestamp 会触发 FileSort 或全表扫描。")
                    print(f"    建议: CREATE INDEX CONCURRENTLY idx_{source_table}_veh_ts "
                          f"ON {source_table} (vehicle_id, timestamp);")

        # ── A3: EXPLAIN ANALYZE（仅计划，不实际执行以节省时间） ──
        print(f"\n{'─' * 72}")
        print("  [A3] EXPLAIN（执行计划预估 / Plan estimate, no execution）")
        print(f"{'─' * 72}")
        print(f"  Query: {source_query}")
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN (ANALYZE false, COSTS true, BUFFERS true) {source_query}")
            for row in cur.fetchall():
                print(f"  {row[0]}")

        # ── A4: EXPLAIN ANALYZE（实际执行，限制行数） ──
        print(f"\n{'─' * 72}")
        print("  [A4] EXPLAIN ANALYZE（实际执行 / Actual execution, LIMIT 50000）")
        print(f"{'─' * 72}")
        limit_query = (
            f"SELECT * FROM ({source_query}) AS _sub LIMIT 50000"
        )
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN ANALYZE {limit_query}")
            plan_rows = cur.fetchall()
        t_explain = time.perf_counter() - t0
        # Print last 10 lines (most relevant)
        for row in plan_rows[-10:]:
            print(f"  {row[0]}")
        print(f"  ─── EXPLAIN ANALYZE wall: {t_explain:.1f}s ───")

        # ── A5: 纯 SQL 执行时间（无 Python 解析开销） ──
        print(f"\n{'─' * 72}")
        print("  [A5] 纯 SQL 执行 / Raw SQL execution (FETCH ALL, no Python parse)")
        print(f"{'─' * 72}")
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout TO '5min'")
            t0 = time.perf_counter()
            cur.execute(f"SELECT COUNT(*) FROM ({source_query}) AS _sub")
            row_count = cur.fetchone()[0]
            t_count = time.perf_counter() - t0
            print(f"  COUNT(*) 结果: {row_count:,}")
            print(f"  耗时: {t_count:.2f}s  ({row_count / t_count:,.0f} rows/s SQL-level)")

        print()

    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# B+C+D) Python 层迭代性能测试
# ═══════════════════════════════════════════════════════════════════════════


def _build_handle(source_type: str, csv_path: str, pg_table: str):
    """构建 source handle，统一返回 (handle, source_kind, source_label)。"""
    if source_type == "csv":
        source = CsvSource(path=csv_path)
        handle = get_database(source)
        return handle, "csv", csv_path
    else:
        source = PostgresSource(
            config=None,  # type: ignore[arg-type]
            query=f"SELECT * FROM {pg_table} ORDER BY vehicle_id, timestamp",
            source_id=pg_table,
            primary_key_field="id",
            connection_factory=lambda _cfg: connect_test_db(),
        )
        handle = get_database(source)
        return handle, "pg", f"postgresql://{TEST_DB_NAME}/{pg_table}"


def _bench_iter_vehicle_tracks(
    handle,
    max_rows: int,
    max_vehicles: int,
    verbose: bool,
    report_every_n: int,
) -> dict:
    """测试 iter_vehicle_tracks() 的吞吐量。

    返回的字典包含各环节耗时和归一化指标。
    """
    stream = StreamVehicleSource(handle)

    total_pts = 0
    total_vehicles = 0
    t_loop_start = time.perf_counter()

    # ── 累加器：因为 iter_vehicle_tracks 内部不可插桩，
    #     使用 "残差法" —— total_wall - 外部操作 = 生成器内部成本
    t_extend = 0.0  # list.extend 开销（模拟 run.py 中的 all_kept_subtracks.extend）

    # 用于模拟 run.py 中 all_kept_subtracks 的累积（但不真的存数据，只计开销）
    dummy_list: list = []

    last_report_t = t_loop_start
    last_report_pts = 0

    try:
        for vehicle_points in stream.iter_vehicle_tracks():
            n_pts = len(vehicle_points)
            total_pts += n_pts
            total_vehicles += 1

            # 模拟 run.py: all_kept_subtracks.extend(vresult.kept_subtracks)
            # 但不真的存数据——只测 extend() + len() 的开销
            t1 = time.perf_counter()
            dummy_list.clear()  # 保持内存不膨胀
            dummy_list.extend(vehicle_points)
            t_extend += time.perf_counter() - t1

            # ── 逐窗进度打印 ──
            now = time.perf_counter()
            if verbose and (total_vehicles <= 5 or total_vehicles % report_every_n == 0):
                elapsed = now - t_loop_start
                window_pts = total_pts - last_report_pts
                window_t = now - last_report_t
                rate = window_pts / window_t if window_t > 0 else 0
                print(
                    f"  [veh {total_vehicles:>6d}] "
                    f"pts={n_pts:>5d}  "
                    f"total_pts={total_pts:>10,}  "
                    f"总速率={total_pts / elapsed:,.0f} pts/s  "
                    f"窗速率={rate:,.0f} pts/s  "
                    f"elapsed={elapsed:.1f}s"
                )
                last_report_t = now
                last_report_pts = total_pts

            # ── 提前终止 ──
            if max_vehicles > 0 and total_vehicles >= max_vehicles:
                break
            if max_rows > 0 and total_pts >= max_rows:
                break

    finally:
        stream.close()

    t_loop_total = time.perf_counter() - t_loop_start

    # 残差 = 生成器内部成本（FETCH + parse_row + hash + vehicle_id 分组）
    t_generator_internal = t_loop_total - t_extend

    return {
        "test_name": "iter_vehicle_tracks()",
        "total_pts": total_pts,
        "total_vehicles": total_vehicles,
        "t_loop_total": t_loop_total,
        "t_generator_internal": t_generator_internal,
        "t_extend": t_extend,
        "pts_per_s": total_pts / t_loop_total if t_loop_total > 0 else 0,
        "veh_per_s": total_vehicles / t_loop_total if t_loop_total > 0 else 0,
        "us_per_pt": t_loop_total / total_pts * 1e6 if total_pts > 0 else 0,
        "ms_per_veh": t_loop_total / total_vehicles * 1e3 if total_vehicles > 0 else 0,
    }


def _bench_iter_points(
    handle,
    max_rows: int,
    verbose: bool,
    report_every_n: int,
) -> dict:
    """测试 iter_points() 的吞吐量——绕过车辆分组。

    重新 open handle 以获取全新的 iterator。
    """
    t_loop_start = time.perf_counter()
    total_pts = 0

    last_report_t = t_loop_start
    last_report_pts = 0

    for point in handle.iter_points():
        total_pts += 1

        now = time.perf_counter()
        if verbose and (total_pts <= 5 or total_pts % report_every_n == 0):
            elapsed = now - t_loop_start
            window_pts = total_pts - last_report_pts
            window_t = now - last_report_t
            rate = window_pts / window_t if window_t > 0 else 0
            print(
                f"  [pt {total_pts:>9,}] "
                f"总速率={total_pts / elapsed:,.0f} pts/s  "
                f"窗速率={rate:,.0f} pts/s  "
                f"elapsed={elapsed:.1f}s"
            )
            last_report_t = now
            last_report_pts = total_pts

        if max_rows > 0 and total_pts >= max_rows:
            break

    t_loop_total = time.perf_counter() - t_loop_start

    return {
        "test_name": "iter_points()",
        "total_pts": total_pts,
        "t_loop_total": t_loop_total,
        "pts_per_s": total_pts / t_loop_total if t_loop_total > 0 else 0,
        "us_per_pt": t_loop_total / total_pts * 1e6 if total_pts > 0 else 0,
    }


def _bench_iter_points_with_hash(
    handle,
    max_rows: int,
    verbose: bool,
    report_every_n: int,
) -> dict:
    """测试 iter_points() + source_row_hash() —— 分离 hash 开销。

    仅对 CSV 模式有意义（PG 模式已跳过 hash）。
    """
    t_hash = 0.0
    t_loop_start = time.perf_counter()
    total_pts = 0

    last_report_t = t_loop_start
    last_report_pts = 0

    for point in handle.iter_points():
        total_pts += 1

        # 模拟 hash 成本——实际 hash 在 iter_points 内部已完成，
        # 这里单独计时一次 hash 来看增量
        t1 = time.perf_counter()
        # 注意：TrajectoryPoint 没有 raw row dict，所以这里无法实际计算 hash。
        # 这是故意留的占位——真正的 hash 已在 iter_points 内部完成，
        # 本测试与 _bench_iter_points 的差值不来自 hash，而是来自其他因素。
        t_hash += time.perf_counter() - t1

        now = time.perf_counter()
        if verbose and (total_pts <= 5 or total_pts % report_every_n == 0):
            elapsed = now - t_loop_start
            window_pts = total_pts - last_report_pts
            window_t = now - last_report_t
            rate = window_pts / window_t if window_t > 0 else 0
            print(
                f"  [pt {total_pts:>9,}] "
                f"速率={total_pts / elapsed:,.0f} pts/s  "
                f"窗速率={rate:,.0f} pts/s"
            )
            last_report_t = now
            last_report_pts = total_pts

        if max_rows > 0 and total_pts >= max_rows:
            break

    t_loop_total = time.perf_counter() - t_loop_start
    t_parse_only = t_loop_total - t_hash

    return {
        "test_name": "iter_points() + hash timing",
        "total_pts": total_pts,
        "t_loop_total": t_loop_total,
        "t_hash_stub": t_hash,
        "pts_per_s": total_pts / t_loop_total if t_loop_total > 0 else 0,
        "us_per_pt": t_loop_total / total_pts * 1e6 if total_pts > 0 else 0,
    }


def _measure_hash_cost(handle, sample_size: int = 10000) -> dict:
    """单独测量 source_row_hash() 的纯 CPU 成本。

    ZH: 从 iter_points() 取前 sample_size 行，对每行的原始字典计算 SHA-256。
        注意：iter_points() 已消费点数据，hash 是在 TrajectoryPoint 构造之前
        基于原始 row dict 计算的，这里近似测量——我们只能测量已有数据的 hash 成本。
        实际 hash 在 CsvTrajectorySourceHandle.iter_points() 的 parse_row() 之前完成。

        本测试仅供 CSV 模式参考。
    """
    # 这个测试较难在不修改 sources.py 的前提下完成，因为 iter_points()
    # 已经把 row dict 转成了 TrajectoryPoint，我们拿不到原始 row。
    # 提供一个粗略估算：生成模拟 row dict 并 hash。
    import hashlib
    import json

    # 取一些实际点来获取字段名
    sample_rows: list[dict] = []
    for i, point in enumerate(handle.iter_points()):
        if i >= sample_size:
            break
        # 从 TrajectoryPoint 反推近似 row dict（字段尽可能真实）
        row = {
            "vehicle_id": point.vehicle_id,
            "timestamp": str(point.timestamp),
            "lon": str(point.lon),
            "lat": str(point.lat),
            "heading": str(point.heading),
            "speed": str(point.speed),
            **{k: str(v) for k, v in (point.extra or {}).items()},
        }
        sample_rows.append(row)

    if not sample_rows:
        return {"hash_total_s": 0, "hash_per_row_us": 0, "sample_size": 0}

    t0 = time.perf_counter()
    for row in sample_rows:
        source_row_hash(row)
    t_hash = time.perf_counter() - t0

    return {
        "sample_size": len(sample_rows),
        "hash_total_s": t_hash,
        "hash_per_row_us": t_hash / len(sample_rows) * 1e6,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 汇总报告
# ═══════════════════════════════════════════════════════════════════════════

def _print_summary(
    source_kind: str,
    source_label: str,
    sql_timing: dict | None,  # PG only: raw SQL execution timing
    r_veh: dict,
    r_pts: dict,
    hash_cost: dict | None,
) -> None:
    """打印汇总对比报告。"""
    print()
    print("=" * 72)
    print("  SUMMARY / 汇总报告")
    print("=" * 72)
    print(f"  数据源 / Source       : {source_label}")
    print(f"  类型   / Kind         : {source_kind}")

    if sql_timing:
        print(f"  SQL COUNT(*)          : {sql_timing['count']:,} rows in {sql_timing['t_count']:.1f}s")
        print(f"  SQL 吞吐量/Throughput : {sql_timing['rows_per_s']:,.0f} rows/s (纯 DB, 无 Python 解析)")

    print()
    print(f"  {'Test':<30} {'Points':>12} {'Time':>8} {'pts/s':>10} {'us/pt':>8}")
    print(f"  {'─' * 30} {'─' * 12} {'─' * 8} {'─' * 10} {'─' * 8}")

    for r in [r_veh, r_pts]:
        name = r["test_name"]
        pts = r.get("total_pts", 0)
        t = r["t_loop_total"]
        pts_s = r["pts_per_s"]
        us_pt = r["us_per_pt"]
        print(f"  {name:<30} {pts:>12,} {t:>7.1f}s {pts_s:>10,.0f} {us_pt:>8.1f}")

    # 分组开销
    if r_veh["total_pts"] > 0 and r_pts["total_pts"] > 0:
        grouping_overhead_s = r_veh["t_loop_total"] - r_pts["t_loop_total"] * (
            r_veh["total_pts"] / max(r_pts["total_pts"], 1)
        )
        # ZH: 更精确：归一化到相同点数
        pts_per_s_veh = r_veh["pts_per_s"]
        pts_per_s_raw = r_pts["pts_per_s"]
        overhead_pct = (pts_per_s_raw - pts_per_s_veh) / pts_per_s_raw * 100 if pts_per_s_raw > 0 else 0
        print(f"  {'分组开销 / Grouping overhead':<30} {'':>12} {'':>8} {'':>10} "
              f"{'':>7}({overhead_pct:+.0f}%)")

    print()

    # ── 归一化指标 ──
    print(f"  {'─' * 68}")
    print(f"  归一化指标 / Normalized metrics")
    print(f"  {'─' * 68}")
    if r_veh["total_pts"] > 0:
        print(f"  iter_vehicle_tracks:  {r_veh['pts_per_s']:,.0f} pts/s  "
              f"| {r_veh['us_per_pt']:.1f} us/pt  "
              f"| {r_veh['ms_per_veh']:.2f} ms/veh")
        print(f"    ├─ 生成器内部:      {r_veh['t_generator_internal']:.1f}s  "
              f"({r_veh['t_generator_internal'] / r_veh['t_loop_total'] * 100:.1f}%)")
        print(f"    └─ list.extend:     {r_veh['t_extend']:.3f}s  "
              f"({r_veh['t_extend'] / r_veh['t_loop_total'] * 100:.1f}%)")
    if r_pts["total_pts"] > 0:
        print(f"  iter_points:         {r_pts['pts_per_s']:,.0f} pts/s  "
              f"| {r_pts['us_per_pt']:.1f} us/pt")

    if hash_cost and hash_cost.get("hash_per_row_us", 0) > 0:
        print(f"\n  source_row_hash (SHA-256):  {hash_cost['hash_per_row_us']:.1f} us/row  "
              f"(n={hash_cost['sample_size']})")

    # ── 瓶颈诊断 ──
    print()
    print("=" * 72)
    print("  DIAGNOSIS / 瓶颈诊断")
    print("=" * 72)

    recommendations: list[tuple[str, str]] = []

    # 检查吞吐量是否合理
    pts_per_s = r_veh["pts_per_s"]
    if source_kind == "pg":
        if pts_per_s < 20_000:
            recommendations.append((
                "P0",
                f"PG 吞吐量仅 {pts_per_s:,.0f} pts/s —— "
                f"远低于预期（命名游标 + 无 hash 应达 50k+）。"
                f"检查：(1) 网络延迟 (2) itersize 过小 (3) 服务端磁盘 I/O"
            ))
        elif pts_per_s < 50_000:
            recommendations.append((
                "P1",
                f"PG 吞吐量 {pts_per_s:,.0f} pts/s —— 中等，检查 itersize 和索引覆盖"
            ))

        if sql_timing and sql_timing.get("rows_per_s", 0) > 0:
            sql_rate = sql_timing["rows_per_s"]
            python_ratio = pts_per_s / sql_rate * 100 if sql_rate > 0 else 0
            if python_ratio < 30:
                recommendations.append((
                    "P1",
                    f"Python 层仅达 SQL 层 {python_ratio:.0f}% 吞吐量 —— "
                    f"parse_row + 对象构造是主要瓶颈"
                ))

    elif source_kind == "csv":
        if hash_cost and hash_cost.get("hash_per_row_us", 0) > 0:
            hash_us = hash_cost["hash_per_row_us"]
            total_us = r_veh["us_per_pt"]
            hash_pct = hash_us / total_us * 100 if total_us > 0 else 0
            if hash_pct > 20:
                recommendations.append((
                    "P0",
                    f"source_row_hash 占 {hash_pct:.0f}% ({hash_us:.1f} us/pt) —— "
                    f"CSV 模式每个点都做 SHA-256；可考虑采样 hash 或去掉 replay 校验"
                ))

    if r_veh["total_vehicles"] > 0:
        avg_pts_per_veh = r_veh["total_pts"] / r_veh["total_vehicles"]
        if avg_pts_per_veh < 10:
            recommendations.append((
                "P2",
                f"平均每车仅 {avg_pts_per_veh:.1f} 点 —— 车辆碎片化严重，"
                f"分组开销占比偏高"
            ))

    if not recommendations:
        recommendations.append((
            "OK",
            "吞吐量在正常范围内，无明显瓶颈。"
        ))

    for pri, msg in recommendations:
        print(f"  [{pri}] {msg}")

    print()


# ═══════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="iter_vehicle_tracks() 性能基准 + SQL 层诊断"
    )
    parser.add_argument(
        "--csv", dest="csv_path", type=str, default="",
        help="CSV 文件路径（指定后切换到 CSV 模式）"
    )
    parser.add_argument(
        "--pg-table", dest="pg_table", type=str, default=CFG["pg_source_table"],
        help=f"PG 源表名（默认 {CFG['pg_source_table']}）"
    )
    parser.add_argument(
        "--max-rows", type=int, default=CFG["max_rows"],
        help="最大处理行数，0 = 不限制"
    )
    parser.add_argument(
        "--max-vehicles", type=int, default=CFG["max_vehicles"],
        help="最大处理车辆数，0 = 不限制"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="关闭逐窗进度打印（等同于 VERBOSE=0）"
    )
    parser.add_argument(
        "--skip-sql-diag", action="store_true",
        help="跳过 SQL 层诊断（PG 模式）"
    )
    parser.add_argument(
        "--report-every", type=int, default=500,
        help="每 N 辆车/点打印一次进度（默认 500）"
    )
    args = parser.parse_args()

    # 确定数据源类型
    if args.csv_path:
        source_type = "csv"
        source_label = args.csv_path
        pg_table = ""
    else:
        source_type = "pg"
        pg_table = args.pg_table
        source_label = f"postgresql://{TEST_DB_NAME}/{pg_table}"

    max_rows = args.max_rows
    max_vehicles = args.max_vehicles
    verbose = not args.quiet
    report_every_n = args.report_every
    skip_sql_diag = args.skip_sql_diag

    print("=" * 72)
    print("  manual_bench_iter_vehicle_tracks")
    print(f"  数据源 / Source       : {source_label}")
    print(f"  类型   / Kind         : {source_type}")
    print(f"  限制   / Limits       : max_rows={max_rows if max_rows > 0 else 'ALL'}, "
          f"max_vehicles={max_vehicles if max_vehicles > 0 else 'ALL'}")
    print(f"  逐窗打印 / Verbose    : {verbose}")
    print(f"  SQL 诊断 / SQL Diag   : {'skip' if skip_sql_diag else 'yes'}")
    print(f"  {"─" * 72}")

    # ═════════════════════════════════════════════════════════════════════
    # PG: SQL 层诊断
    # ═════════════════════════════════════════════════════════════════════
    sql_timing = None
    if source_type == "pg" and not skip_sql_diag:
        print("\n" + "█" * 72)
        print("  PHASE A: SQL 层诊断 / SQL-level Diagnostics")
        print("█" * 72)
        _pg_sql_diagnostics(pg_table)

        # 额外：纯 SQL 执行计时
        print(f"{'─' * 72}")
        print("  [A6] 纯 SQL 执行（无 Python 解析）计时")
        print(f"{'─' * 72}")
        import psycopg
        env = load_test_env()
        conn = psycopg.connect(
            host=env["TRACK_PG_HOST"],
            port=env["TRACK_PG_PORT"],
            dbname=TEST_DB_NAME,
            user=env["TRACK_PG_USER"],
            password=env["TRACK_PG_PASSWORD"],
            connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
        )
        try:
            conn.execute("SET statement_timeout TO '5min'")
            source_query = f"SELECT * FROM {pg_table} ORDER BY vehicle_id, timestamp"
            t0 = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM ({source_query}) AS _sub")
                count = cur.fetchone()[0]
            t_count = time.perf_counter() - t0
            sql_timing = {
                "count": count,
                "t_count": t_count,
                "rows_per_s": count / t_count if t_count > 0 else 0,
            }
            print(f"  COUNT(*) = {count:,} rows")
            print(f"  耗时 = {t_count:.2f}s")
            print(f"  吞吐量 = {count / t_count:,.0f} rows/s (纯 SQL，无 Python 对象)")
        finally:
            conn.close()

    # ═════════════════════════════════════════════════════════════════════
    # Python 层性能测试
    # ═════════════════════════════════════════════════════════════════════

    print("\n" + "█" * 72)
    print("  PHASE B+C+D: Python 层迭代性能测试")
    print("█" * 72)

    # ── B: iter_vehicle_tracks ──
    print(f"\n{'─' * 72}")
    print("  [B] iter_vehicle_tracks() —— 车辆分组迭代")
    print(f"{'─' * 72}")
    handle_b, kind, label = _build_handle(source_type, args.csv_path, pg_table)
    r_veh = _bench_iter_vehicle_tracks(
        handle_b, max_rows, max_vehicles, verbose, report_every_n,
    )

    # ── C: iter_points ──
    print(f"\n{'─' * 72}")
    print("  [C] iter_points() —— 纯点迭代（绕过车辆分组）")
    print(f"{'─' * 72}")
    handle_c, _, _ = _build_handle(source_type, args.csv_path, pg_table)
    # 如果 B 限制了行数，C 也限制相同的行数以确保可比
    pts_cap = max_rows if max_rows > 0 else r_veh["total_pts"]
    r_pts = _bench_iter_points(handle_c, pts_cap, verbose, report_every_n)

    # ── D: hash 成本测量（仅 CSV） ──
    hash_cost = None
    if source_type == "csv":
        print(f"\n{'─' * 72}")
        print("  [D] source_row_hash() 成本测量")
        print(f"{'─' * 72}")
        handle_d, _, _ = _build_handle(source_type, args.csv_path, pg_table)
        hash_cost = _measure_hash_cost(handle_d, sample_size=min(10000, r_veh["total_pts"]))
        if hash_cost["sample_size"] > 0:
            print(f"  样本数: {hash_cost['sample_size']}")
            print(f"  hash 总耗时: {hash_cost['hash_total_s']:.3f}s")
            print(f"  hash 每行:   {hash_cost['hash_per_row_us']:.1f} us/row")
        handle_d.close()

    # ── 清理 ──
    handle_b.close()
    handle_c.close()

    # ═════════════════════════════════════════════════════════════════════
    # 汇总
    # ═════════════════════════════════════════════════════════════════════
    _print_summary(kind, label, sql_timing, r_veh, r_pts, hash_cost)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
