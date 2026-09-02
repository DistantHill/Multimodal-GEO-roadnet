"""薄壳脚本：测试 SpatialWindow 滑窗循环各环节耗时。

用法：
    python manual_bench_spatial_windows.py

    VSCode: 右键 → "Run Python File" 或 F5。
    可以改下面的 CONFIG 区域来调整参数。

依赖：PostgreSQL 已运行且 trajectory_points/injected_points 表有数据。
默认连接 track_test 数据库（由 manual_setup_test_db.py 创建）。
临时切回生产库：``TRACK_BENCH_DB=postgres python manual_bench_spatial_windows.py``
"""

from __future__ import annotations

import os
import sys
import time

# 确保项目根目录在 sys.path 中，免去 pip install -e .
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from track.v2.manual_thin_benchmark._thin_bench_helpers import connect_test_db, TEST_DB_NAME
from track.v2.intersect2 import (
    IntersectConfig,
    SpatialWindowIterator,
    _extract_orthogonal_cross_track_candidates,
    _query_active_points_in_window,
    _query_spatial_extent,
    _tracks_from_points,
)
from track.v2.db.queries import _build_points_from_rows

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG —— 在这里改参数
# ═══════════════════════════════════════════════════════════════════════════

# 留 None 则自动查询全量空间范围
MANUAL_EXTENT: tuple[float, float, float, float] | None = None

CONFIG_OVERRIDES: dict = {
    "use_cross_track_candidates": True,
    "db_table": "trajectory_points",
    "spatial_window_size": (2000.0, 2000.0),   # 窗大小（米）
    "spatial_window_overlap": 0.15,             # 每侧重叠比
}

# 测试前 N 个窗口（0 = 全量）
MAX_WINDOWS: int = 0

# 是否打印每个窗口的详情（窗口多时关闭以减少输出）
VERBOSE_WINDOWS: bool = True


def main() -> None:
    cfg = IntersectConfig(**CONFIG_OVERRIDES)

    print("=" * 60)
    print(f"Connecting to PostgreSQL test DB ({TEST_DB_NAME}) ...")
    t0 = time.perf_counter()

    conn = connect_test_db()
    print(f"  Connected: {conn.info.host}:{conn.info.port}/{conn.info.dbname} "
          f"(server {conn.info.server_version})")

    # 空间范围
    if MANUAL_EXTENT is not None:
        extent = MANUAL_EXTENT
        print(f"\n  Extent (manual): {extent}")
    else:
        extent = _query_spatial_extent(conn, table=cfg.db_table)
        if extent is None:
            print("  ERROR: No active points found in DB!")
            conn.close()
            return
        print(f"\n  Extent (auto): lon=[{extent[0]:.6f}, {extent[2]:.6f}], "
              f"lat=[{extent[1]:.6f}, {extent[3]:.6f}]")

    origin_lon, origin_lat = extent[0], extent[1]

    windows = SpatialWindowIterator(
        extent,
        size=cfg.spatial_window_size,
        overlap=cfg.spatial_window_overlap,
    )
    total = windows.count_windows()
    print(f"\n  Window: size={cfg.spatial_window_size}, overlap={cfg.spatial_window_overlap}")
    print(f"  Total windows: {total:,}")
    limit = min(MAX_WINDOWS, total) if MAX_WINDOWS > 0 else total
    print(f"  Will test: {limit:,} windows")
    print("=" * 60)

    # ═════════════════════════════════════════════════════════════════════
    # 计时累加器（按子步骤拆分）
    # ═════════════════════════════════════════════════════════════════════
    t_query = 0.0        # _query_active_points_in_window
    t_tracks = 0.0       # _tracks_from_points
    t_extract = 0.0      # _extract_orthogonal_cross_track_candidates
    total_points = 0
    total_candidates = 0
    empty_windows = 0

    t_loop_start = time.perf_counter()
    window_count = 0

    for window in windows.iter_windows():
        window_count += 1
        if window_count > limit:
            break

        # ── Step 1: DB query ──
        t1 = time.perf_counter()
        points = _query_active_points_in_window(conn, window, table=cfg.db_table)
        t_query += time.perf_counter() - t1

        if not points:
            empty_windows += 1
            if VERBOSE_WINDOWS:
                print(f"  [{window_count:4d}/{limit}] EMPTY "
                      f"({time.perf_counter() - t_loop_start:.1f}s)")
            continue

        total_points += len(points)

        # ── Step 2: group into tracks ──
        t2 = time.perf_counter()
        window_tracks = _tracks_from_points(points)
        t_tracks += time.perf_counter() - t2

        # ── Step 3: extract candidates ──
        t3 = time.perf_counter()
        candidates, _, _ = _extract_orthogonal_cross_track_candidates(
            window_tracks, origin_lon, origin_lat, cfg,
        )
        t_extract += time.perf_counter() - t3
        total_candidates += len(candidates)

        if VERBOSE_WINDOWS:
            print(
                f"  [{window_count:4d}/{limit}] "
                f"points={len(points):5d}  "
                f"tracks={len(window_tracks):4d}  "
                f"candidates={len(candidates):3d}  "
                f"(Q:{time.perf_counter() - t2:.3f}s | "
                f"T:{t3 - t2:.3f}s | "
                f"E:{time.perf_counter() - t3:.3f}s)"
            )

    t_total = time.perf_counter() - t_loop_start

    # ═════════════════════════════════════════════════════════════════════
    # 汇总
    # ═════════════════════════════════════════════════════════════════════
    print()
    print("=" * 60)
    print("SUMMARY / 汇总")
    print("=" * 60)
    print(f"  Windows processed:     {window_count:,}")
    print(f"  Empty windows:         {empty_windows:,} ({empty_windows/max(window_count,1)*100:.1f}%)")
    print(f"  Total points fetched:  {total_points:,}")
    print(f"  Total candidates:      {total_candidates:,}")
    print()
    print(f"  Total loop time:       {t_total:.2f}s")
    print(f"    ├─ DB query:          {t_query:.2f}s ({t_query/t_total*100:.1f}%)")
    print(f"    ├─ tracks_from_points:{t_tracks:.2f}s ({t_tracks/t_total*100:.1f}%)")
    print(f"    └─ extract candidates:{t_extract:.2f}s ({t_extract/t_total*100:.1f}%)")
    print()
    non_empty = window_count - empty_windows
    if window_count > 0:
        print(f"  Per-window avg:        {t_total/window_count*1000:.1f} ms")
    if non_empty > 0:
        print(f"  Per-non-empty avg:     {t_total/non_empty*1000:.1f} ms")
        print(f"    ├─ DB query:          {t_query/non_empty*1000:.1f} ms")
        print(f"    ├─ tracks_from_points:{t_tracks/non_empty*1000:.1f} ms")
        print(f"    └─ extract candidates:{t_extract/non_empty*1000:.1f} ms")
    if total_points > 0:
        print(f"  Per-point query:       {t_query/total_points*1e6:.1f} µs")
    print()

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
