"""手动测试 _extract_cross_track_windows 的薄壳脚本。

用法：
    python manual_test_cross_track.py

    VSCode 调试：直接按 F5 或右键 → "Run Python File" 即可。
    可以改下面的 CONFIG 区域来调整参数。

依赖：需要 PostgreSQL 已运行且 trajectory_points/injected_points 表有数据。
默认连接 track_test 数据库（由 manual_setup_test_db.py 创建）。
临时切回生产库：``TRACK_BENCH_DB=postgres python manual_test_cross_track.py``
"""

from __future__ import annotations

import os
import sys
import time

# 确保项目根目录在 sys.path 中，免去 pip install -e .
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from _thin_bench_helpers import connect_test_db, TEST_DB_NAME
from track.v2.intersect2 import (
    IntersectConfig,
    _extract_cross_track_windows,
    _query_spatial_extent,
)

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG —— 在这里改参数
# ═══════════════════════════════════════════════════════════════════════════

# 留 None 则自动查询全量空间范围；也可以手动指定 (lon_min, lat_min, lon_max, lat_max)
MANUAL_EXTENT: tuple[float, float, float, float] | None = None

# IntersectConfig 参数（需要改什么就覆盖什么）
CONFIG_OVERRIDES: dict = {
    "use_orthogonal_candidates": True,
    "db_table": "trajectory_points",
    "spatial_window_size": (2000.0, 2000.0),   # 滑窗大小（米）
    "spatial_window_overlap": 0.15,             # 每侧重叠比例
    # 下面两个跨车求交的核心参数也可以改
    # "cross_track_straightness_deg": 15.0,
    # "cross_track_min_angle_deg": 80.0,
    # "cross_track_max_angle_deg": 100.0,
    # "cross_track_dbscan_eps_m": 100.0,
    # "cross_track_dbscan_min_samples": 3,
}


def main() -> None:
    cfg = IntersectConfig(**CONFIG_OVERRIDES)

    print("=" * 60)
    print(f"Connecting to PostgreSQL test DB ({TEST_DB_NAME}) ...")
    t_start = time.perf_counter()

    conn = connect_test_db()
    print(f"  Connected: {conn.info.host}:{conn.info.port}/{conn.info.dbname} "
          f"(server {conn.info.server_version})")

    # 查询或使用手动指定的空间范围
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

    # 用数据第一个点的 lon/lat 作为坐标原点
    origin_lon, origin_lat = extent[0], extent[1]

    print(f"\n  Config: use_orthogonal={cfg.use_orthogonal_candidates}")
    print(f"          db_table={cfg.db_table}")
    print(f"          window_size={cfg.spatial_window_size}")
    print(f"          window_overlap={cfg.spatial_window_overlap}")
    print(f"          origin=({origin_lon:.6f}, {origin_lat:.6f})")
    print("=" * 60)

    # ── 调用目标函数 ──────────────────────────────────────────────
    candidates = _extract_cross_track_windows(
        conn, cfg,
        extent=extent,
        origin_lon=origin_lon,
        origin_lat=origin_lat,
    )

    elapsed = time.perf_counter() - t_start
    print(f"\n  总耗时 / Total: {elapsed:.1f}s")
    print(f"  候选数 / Candidates: {len(candidates)}")

    # 打印前几个候选
    for i, c in enumerate(candidates[:10]):
        print(f"    [{i}] vehicle={c.vehicle_id}, "
              f"lon={c.lon:.6f}, lat={c.lat:.6f}, "
              f"ts={c.timestamp:.1f}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
