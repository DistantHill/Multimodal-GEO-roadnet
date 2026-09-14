"""手动测试 _extract_cross_track_windows 的薄壳脚本。

用法：
    python manual_test_cross_track.py --processing-srid 32650

    VSCode 调试：请在 launch args 中显式设置 --processing-srid。
    可以改下面的 CONFIG 区域来调整参数。

依赖：需要 PostgreSQL 已运行且 trajectory_points/injected_points 表有数据。
默认连接 track_test 数据库（由 manual_setup_test_db.py 创建）。
临时切回生产库：``TRACK_BENCH_DB=postgres python manual_test_cross_track.py``
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 确保项目根目录在 sys.path 中，免去 pip install -e .
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from track.v2.manual_thin_benchmark._thin_bench_helpers import connect_test_db, TEST_DB_NAME
from track.v2.detect import (
    IntersectConfig,
    _extract_cross_track_windows,
    _query_spatial_extent,
)
from track.v2.models import ProjectionConfig
from track.v2.projection import CoordinateTransformer, create_spatial_context

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG —— 在这里改参数
# ═══════════════════════════════════════════════════════════════════════════

# 留 None 则自动查询全量空间范围；也可以手动指定投影米制 (x_min, y_min, x_max, y_max)
MANUAL_EXTENT: tuple[float, float, float, float] | None = None

# IntersectConfig 参数（需要改什么就覆盖什么）
CONFIG_OVERRIDES: dict = {
    "use_cross_track_candidates": True,
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--processing-srid",
        type=int,
        required=True,
        help="Projected metre-based EPSG code valid for the complete benchmark data region.",
    )
    args = parser.parse_args()
    spatial_context = create_spatial_context(
        ProjectionConfig(processing_srid=args.processing_srid)
    )
    transformer = CoordinateTransformer(spatial_context)
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
        extent = _query_spatial_extent(
            conn,
            table=cfg.db_table,
            spatial_context=spatial_context,
        )
        if extent is None:
            print("  ERROR: No active points found in DB!")
            conn.close()
            return
        print(f"\n  Extent (auto): x=[{extent[0]:.3f}, {extent[2]:.3f}]m, "
              f"y=[{extent[1]:.3f}, {extent[3]:.3f}]m")

    print(f"\n  Config: use_cross_track={cfg.use_cross_track_candidates}")
    print(f"          db_table={cfg.db_table}")
    print(f"          window_size={cfg.spatial_window_size}")
    print(f"          window_overlap={cfg.spatial_window_overlap}")
    print(f"          processing_srid=EPSG:{spatial_context.processing_srid}")
    print("=" * 60)

    # ── 调用目标函数 ──────────────────────────────────────────────
    candidates = _extract_cross_track_windows(
        conn, cfg,
        extent=extent,
        spatial_context=spatial_context,
        transformer=transformer,
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
