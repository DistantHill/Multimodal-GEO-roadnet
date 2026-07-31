"""薄壳脚本：量化 PG 模式下 read→clean 主循环各环节耗时。

ZH: 对 run_pipeline() 中 Parser 0 + Cleaner 1 的逐车迭代循环做分步计时，
    量化 source_row_hash、INSERT、clean_vehicle 子步骤、mark_excluded_points
    各自占比，定位 1.2w 点/s 的性能瓶颈。

用法：
    python -m track.v2.manual_thin_benchmark.manual_bench_read_clean

    VSCode: 右键 → "Run Python File" 或 F5。
    可以改下面的 CONFIG 区域来调整参数。

依赖：PostgreSQL 已运行且 trajectory_points 表有数据。
默认连接 track_test 数据库（由 manual_setup_test_db.py 创建）。
临时切回生产库：``TRACK_BENCH_DB=postgres python manual_bench_read_clean.py``

EN: Thin benchmark for the per-vehicle read→clean hot loop in run_pipeline().
    Times each sub-step independently to quantify bottlenecks behind the
    ~12k points/s throughput in PG mode.
"""

from __future__ import annotations

import os
import sys
import time

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from track.v2.manual_thin_benchmark._thin_bench_helpers import connect_test_db, TEST_DB_NAME
from track.v2.models import PostgresSource
from track.v2 import get_database
from track.v2.models import CleanerConfig
from track.v2.cleaner import _split_by_gaps, _exclude_stationary, _inject_orthogonal
from track.v2.sources import StreamVehicleSource
from track.v2.db_writer import insert_trajectory_points, mark_excluded_points

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG —— 在这里改参数
# ═══════════════════════════════════════════════════════════════════════════

# 源表名（PG 模式从此表读取）
SOURCE_TABLE: str = os.environ.get("TRACK_BENCH_SOURCE_TABLE", "trajectory_points")

# 目标表名（写入 trajectory_points 的目标表；None = 与 SOURCE_TABLE 相同 → 原地模式跳过 INSERT）
TARGET_TABLE: str | None = os.environ.get("TRACK_BENCH_TARGET_TABLE") or None
# 若 TARGET_TABLE is None，自动取 SOURCE_TABLE（原地模式，跳过 insert_trajectory_points）
# 设 TARGET_TABLE="trajectory_points_copy" 可测试非原地 INSERT 开销

# 测试前 N 辆车（0 = 全量）
MAX_VEHICLES: int = int(os.environ.get("TRACK_BENCH_MAX_VEHICLES", "100000"))

# 是否打印每辆车的详情（车辆多时关闭以减少输出）
VERBOSE: bool = os.environ.get("TRACK_BENCH_VERBOSE", "1") == "1"


def main() -> None:
    source_table = SOURCE_TABLE
    target_table = TARGET_TABLE or source_table
    is_inplace = (target_table == source_table)

    print("=" * 72)
    print(f"  manual_bench_read_clean — Read→Clean 热循环性能分析")
    print(f"  DB: {TEST_DB_NAME}")
    print(f"  Source table: {source_table}")
    print(f"  Target table: {target_table}  {'(in-place → skip INSERT)' if is_inplace else '(non-in-place → INSERT enabled)'}")
    print(f"  Max vehicles: {MAX_VEHICLES if MAX_VEHICLES > 0 else 'ALL'}")
    print(f"  Verbose: {VERBOSE}")
    print("=" * 72)

    # ── Connect ─────────────────────────────────────────────────────────
    conn = connect_test_db()
    conn.execute("SET statement_timeout TO '10min'")
    conn.execute("SET idle_in_transaction_session_timeout TO '10min'")
    print(f"\n  Connected: {conn.info.host}:{conn.info.port}/{conn.info.dbname} "
          f"(server {conn.info.server_version})")

    try:
        # ── Build PG source handle ──────────────────────────────────────
        # connection_factory 为 handle.iter_points() 创建独立的读连接，
        # 与主循环中的 conn（写连接）分离，精确复刻 run_pipeline 的双连接架构。
        source = PostgresSource(
            config=None,  # type: ignore[arg-type] — connection_factory bypasses config
            query=f"SELECT * FROM {source_table} ORDER BY vehicle_id, timestamp",
            source_id=source_table,
            primary_key_field="id",
            connection_factory=lambda _cfg: connect_test_db(),
        )

        handle = get_database(source)
        stream = StreamVehicleSource(handle)

        # ── Quick row count (pg_class estimate) ─────────────────────────
        t0 = time.perf_counter()
        total_rows = handle.count_rows()
        t_count = time.perf_counter() - t0
        print(f"  Estimated rows: {total_rows:,}  ({t_count:.3f}s)")

        # ═════════════════════════════════════════════════════════════════
        # 计时累加器（分步独立）
        # ═════════════════════════════════════════════════════════════════
        # 注意：iter_vehicle_tracks 内部的 PG fetch + parse 是隐式成本，
        #       通过 "read 残留时间" 来推算：
        #       t_read_residual = t_loop_total - (t_insert + t_clean + t_mark_excluded)
        t_insert = 0.0           # insert_trajectory_points (batch)
        t_clean = 0.0            # clean_vehicle 总耗时（含三个子步骤）
        t_clean_split = 0.0      #   └─ _split_by_gaps
        t_clean_exclude = 0.0    #   └─ _exclude_stationary
        t_clean_inject = 0.0     #   └─ _inject_orthogonal
        t_mark_excluded = 0.0    # mark_excluded_points (batched UPDATE)

        total_raw = 0
        total_excluded = 0
        total_injected = 0
        vehicle_count = 0

        tp_id_map: dict[tuple[str, str], int] = {}
        cfg = CleanerConfig()
        _pending_excluded: list = []

        # ═════════════════════════════════════════════════════════════════
        # 主循环 —— 精确复制 run_pipeline() 逻辑 + 计时累加器
        # ═════════════════════════════════════════════════════════════════
        t_loop_start = time.perf_counter()

        for vehicle_points in stream.iter_vehicle_tracks():
            vehicle_count += 1
            total_raw += len(vehicle_points)

            # ── Step 1: insert_trajectory_points ──
            if not is_inplace:
                t1 = time.perf_counter()
                batch_map = insert_trajectory_points(conn, vehicle_points, table=target_table)
                tp_id_map.update(batch_map)
                t_insert += time.perf_counter() - t1

            # ── Step 2: clean_vehicle (子步骤拆分) ──
            t_clean_start = time.perf_counter()

            # _split_by_gaps
            t1 = time.perf_counter()
            subtracks = _split_by_gaps(vehicle_points, cfg.max_gap_s)
            t_clean_split += time.perf_counter() - t1

            # _exclude_stationary
            t1 = time.perf_counter()
            excluded: list = []
            kept_subtracks: list[list] = []
            for subtrack in subtracks:
                kept, removed = _exclude_stationary(subtrack, cfg)
                excluded.extend(removed)
                kept_subtracks.append(kept)
            t_clean_exclude += time.perf_counter() - t1

            # _inject_orthogonal
            t1 = time.perf_counter()
            active: list = []
            injected_count = 0
            for subtrack in kept_subtracks:
                inj_subtrack, count = _inject_orthogonal(subtrack, cfg)
                active.extend(inj_subtrack)
                injected_count += count
            t_clean_inject += time.perf_counter() - t1

            t_clean += time.perf_counter() - t_clean_start

            total_excluded += len(excluded)
            total_injected += injected_count

            # ── Step 3: mark_excluded_points (batch-accumulated, matching run_pipeline) ──
            _pending_excluded.extend(excluded)
            if len(_pending_excluded) >= 2000 or vehicle_count % 200 == 0:
                t1 = time.perf_counter()
                mark_excluded_points(conn, _pending_excluded, table=target_table, tp_id_map=tp_id_map)
                t_mark_excluded += time.perf_counter() - t1
                _pending_excluded.clear()

            # ── Verbose per-vehicle output ──
            if VERBOSE and (vehicle_count <= 5 or vehicle_count % 100 == 0):
                elapsed = time.perf_counter() - t_loop_start
                rate = total_raw / elapsed if elapsed > 0 else 0
                print(
                    f"  [veh {vehicle_count:5d}] "
                    f"pts={len(vehicle_points):4d}  excl={len(excluded):3d}  "
                    f"inj={injected_count:2d}  "
                    f"total={total_raw:>10,}  "
                    f"rate={rate:.0f} pts/s  "
                    f"elapsed={elapsed:.1f}s"
                )

            if MAX_VEHICLES > 0 and vehicle_count >= MAX_VEHICLES:
                break

        # Final flush of pending excluded points
        if _pending_excluded:
            t1 = time.perf_counter()
            mark_excluded_points(conn, _pending_excluded, table=target_table, tp_id_map=tp_id_map)
            t_mark_excluded += time.perf_counter() - t1
            _pending_excluded.clear()

        t_loop_total = time.perf_counter() - t_loop_start
        stream.close()
        handle.close()

        # ═════════════════════════════════════════════════════════════════
        # 计算 read 残留时间
        # read = iter_vehicle_tracks 内部开销：
        #        PG 命名游标 FETCH + parse_row + source_row_hash + vehicle_id 分组
        # ═════════════════════════════════════════════════════════════════
        t_steps_sum = t_insert + t_clean + t_mark_excluded
        t_read_residual = t_loop_total - t_steps_sum

        # ═════════════════════════════════════════════════════════════════
        # 汇总报告
        # ═════════════════════════════════════════════════════════════════
        print()
        print("=" * 72)
        print("SUMMARY / 汇总")
        print("=" * 72)
        print(f"  Vehicles processed:      {vehicle_count:,}")
        print(f"  Total points:            {total_raw:,}")
        print(f"  Total excluded:          {total_excluded:,}")
        print(f"  Total injected:          {total_injected:,}")
        print(f"  In-place mode:           {is_inplace}  ({'INSERT skipped' if is_inplace else 'INSERT enabled'})")
        print()
        print(f"  Total loop time:         {t_loop_total:>8.2f}s  (100.0%)")
        print(f"  ├─ read+parse (残差):     {t_read_residual:>8.2f}s  ({t_read_residual/t_loop_total*100:5.1f}%)")
        print(f"  │   (PG模式 source_row_hash 已跳过 / hash skipped in PG mode)")
        if not is_inplace:
            print(f"  ├─ insert_trajectory:     {t_insert:>8.2f}s  ({t_insert/t_loop_total*100:5.1f}%)")
        print(f"  ├─ clean_vehicle (合计):  {t_clean:>8.2f}s  ({t_clean/t_loop_total*100:5.1f}%)")
        print(f"  │   ├─ _split_by_gaps:    {t_clean_split:>8.2f}s  ({t_clean_split/t_loop_total*100:5.1f}%)")
        print(f"  │   ├─ _exclude_stationary:{t_clean_exclude:>8.2f}s  ({t_clean_exclude/t_loop_total*100:5.1f}%)")
        print(f"  │   └─ _inject_orthogonal: {t_clean_inject:>8.2f}s  ({t_clean_inject/t_loop_total*100:5.1f}%)")
        print(f"  └─ mark_excluded (batch): {t_mark_excluded:>8.2f}s  ({t_mark_excluded/t_loop_total*100:5.1f}%)")
        print()

        # ── 归一化指标 ──
        if vehicle_count > 0:
            print(f"  Per-vehicle avg:         {t_loop_total/vehicle_count*1000:.2f} ms")
            print(f"    ├─ read+parse:          {t_read_residual/vehicle_count*1000:.2f} ms")
            if not is_inplace:
                print(f"    ├─ insert:              {t_insert/vehicle_count*1000:.2f} ms")
            print(f"    ├─ clean (total):       {t_clean/vehicle_count*1000:.2f} ms")
            print(f"    │   ├─ split_by_gaps:   {t_clean_split/vehicle_count*1000:.2f} ms")
            print(f"    │   ├─ excl_stationary: {t_clean_exclude/vehicle_count*1000:.2f} ms")
            print(f"    │   └─ inject_orthog:   {t_clean_inject/vehicle_count*1000:.2f} ms")
            print(f"    └─ mark_excluded:       {t_mark_excluded/vehicle_count*1000:.2f} ms")
            print()
        if total_raw > 0:
            print(f"  Per-point total:         {t_loop_total/total_raw*1e6:.1f} us")
            print(f"  Per-point read+parse:    {t_read_residual/total_raw*1e6:.1f} us")
            print(f"  Throughput:              {total_raw/t_loop_total:,.0f} pts/s")
            print()

        # ── 瓶颈诊断 ──
        print("=" * 72)
        print("DIAGNOSIS / 瓶颈诊断")
        print("=" * 72)

        pct_read = t_read_residual / t_loop_total * 100
        pct_insert = t_insert / t_loop_total * 100 if not is_inplace else 0.0
        pct_clean = t_clean / t_loop_total * 100
        pct_mark = t_mark_excluded / t_loop_total * 100

        recommendations: list[tuple[str, str]] = []

        if pct_insert > 30:
            recommendations.append((
                "P0",
                f"insert_trajectory_points 占 {pct_insert:.0f}% —— "
                f"逐车 INSERT 往返是最大瓶颈；改为跨车辆攒批（N 行或 N 秒 flush 一次）"
            ))

        if pct_mark > 10:
            recommendations.append((
                "P1",
                f"mark_excluded_points (batched) 占 {pct_mark:.0f}% —— 可进一步增大攒批阈值"
            ))

        if pct_read > 50:
            recommendations.append((
                "P1",
                f"read+parse 占 {pct_read:.0f}% —— 命名游标 itersize 偏小（默认 2000），增大到 10000+；"
                f"parse_row 字典查找可改用元组下标"
            ))

        if pct_clean > 20:
            recommendations.append((
                "P2",
                f"clean_vehicle 占 {pct_clean:.0f}% —— CPU bound，如车辆点数少可考虑批量处理"
            ))

        if not recommendations:
            recommendations.append((
                "OK",
                "各环节分布均匀，source_row_hash 已跳过，mark_excluded 已批量化。"
            ))

        for pri, msg in recommendations:
            print(f"  [{pri}] {msg}")

        if pct_read > 40:
            print()
            print(f"  NOTE: read+parse 残差占比 {pct_read:.0f}% 偏高。")
            print(f"    建议单独运行 manual_bench_parse_only.py 进一步拆解 parse_row vs PG FETCH 占比。")

    finally:
        conn.close()
        print(f"\nDone. Connection closed.")


if __name__ == "__main__":
    main()
