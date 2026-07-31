"""手动诊断转向事件检测 —— 逐车统计每个过滤条件的拦截量。

ZH: 当你跑 ``python -m track.v2.cli.run --pg-table ...`` 发现
    "转向事件 / Turn events: 0" 时，用这个脚本逐车诊断是哪个条件把全部
    候选都过滤掉了。直接复刻 ``_detect_turn_events()`` 的逻辑，但在每个
    if-continue 处埋计数器。

用法：
    python track/v2/manual_thin_benchmark/manual_diag_turn_events.py

    默认连接 track_test。临时切到其他库：``TRACK_BENCH_DB=postgres ...``

    --table      目标 trajectory_points 表（默认 trajectory_points）
    --max-tracks 最多检查 N 辆车（默认 500，-1 = 全部）
    --verbose    打印被拦截的样本轨迹详情
    --source-id  按 source_id 过滤（可选）

依赖：PostgreSQL 已运行且表中有 active 数据。
"""

from __future__ import annotations

import math
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from track.v2.manual_thin_benchmark._thin_bench_helpers import connect_test_db, TEST_DB_NAME
from track.v2.intersect2 import (
    IntersectConfig,
    _query_spatial_extent,
    _stream_vehicle_tracks_from_db,
)
from track.v2._geometry import (
    _angle_delta_deg,
    _circular_mean_deg,
    _distance_m,
    _to_xy,
)


def diagnose_turn_events(
    *,
    table: str = "trajectory_points",
    source_id: str | None = None,
    max_tracks: int = 500,
    verbose: bool = False,
) -> dict:
    """ZH: 逐车统计转向事件过滤条件，返回汇总 dict。"""
    cfg = IntersectConfig(use_orthogonal_candidates=False, use_turn_events=True)

    conn = connect_test_db()
    print(f"Connected: {conn.info.host}:{conn.info.port}/{conn.info.dbname}")
    print(f"Table: {table}, Source ID filter: {source_id or '(none)'}")

    # 获取空间范围以确定原点
    extent = _query_spatial_extent(conn, table=table, source_id=source_id)
    if extent is None:
        print("ERROR: No active points found in DB!")
        conn.close()
        return {}

    origin_lon = (extent[0] + extent[2]) / 2.0
    origin_lat = (extent[1] + extent[3]) / 2.0
    print(f"Extent: lon=[{extent[0]:.6f}, {extent[2]:.6f}], lat=[{extent[1]:.6f}, {extent[3]:.6f}]")
    print(f"Origin: ({origin_lon:.6f}, {origin_lat:.6f})")

    # ── 配置参数 ──
    w = cfg.turn_window
    min_leg = cfg.min_leg_m
    min_speed = cfg.min_movement_speed_mps
    min_turn = cfg.min_turn_deg
    max_turn = cfg.max_turn_deg

    print(f"Turn config: window={w}, min_leg={min_leg}m, min_speed={min_speed}m/s, "
          f"min_turn={min_turn}°, max_turn={max_turn}°")
    print(f"Required track length: >= {2*w + 1} points")

    # ── 计数器 ──
    stats = {
        "total_tracks": 0,
        "total_points": 0,
        "too_short": 0,              # 点数不足 2*w+1
        "candidates_checked": 0,     # 总共检查的候选窗口数
        "reject_in_leg": 0,          # in_leg < min_leg_m
        "reject_out_leg": 0,         # out_leg < min_leg_m
        "reject_speed": 0,           # movement_speed < min_speed
        "reject_turn_too_small": 0,  # abs_turn < min_turn
        "reject_turn_too_large": 0,  # abs_turn > max_turn
        "reject_degenerate_ts": 0,   # elapsed <= 0
        "accepted": 0,               # 通过所有过滤
        "tracks_with_events": 0,     # 至少产生一个 turn event 的轨迹数
    }

    # 收集一些样本
    sample_turns: list[dict] = []  # 成功检测到的转向（最多保留 10 个）
    sample_rejects: list[dict] = []  # 被拦截的候选（最多保留 20 个）
    heading_stats: list[float] = []  # 收集所有 abs_turn 值用于分布分析

    t_start = time.perf_counter()
    track_iter = _stream_vehicle_tracks_from_db(
        conn, table=table, source_id=source_id,
    )

    for track in track_iter:
        stats["total_tracks"] += 1
        stats["total_points"] += len(track)

        if max_tracks > 0 and stats["total_tracks"] > max_tracks:
            break

        vid = track[0].vehicle_id

        if len(track) < 2 * w + 1:
            stats["too_short"] += 1
            if verbose and stats["too_short"] <= 3:
                print(f"  [TOO_SHORT] vid={vid}, len={len(track)}, "
                      f"need>={2*w+1}")
            continue

        # ── 坐标转换 ──
        xy = [_to_xy(p.lon, p.lat, origin_lon, origin_lat) for p in track]
        headings = [p.heading for p in track]

        track_has_event = False

        for i in range(w, len(track) - w):
            stats["candidates_checked"] += 1

            p_i = track[i]

            in_leg = _distance_m(xy[i - w], xy[i])
            if in_leg < min_leg:
                stats["reject_in_leg"] += 1
                if verbose and len(sample_rejects) < 20:
                    sample_rejects.append({
                        "vid": vid, "idx": i, "reason": "in_leg",
                        "value": f"{in_leg:.1f}m < {min_leg}m",
                        "lon": p_i.lon, "lat": p_i.lat,
                    })
                continue

            out_leg = _distance_m(xy[i], xy[i + w])
            if out_leg < min_leg:
                stats["reject_out_leg"] += 1
                if verbose and len(sample_rejects) < 20:
                    sample_rejects.append({
                        "vid": vid, "idx": i, "reason": "out_leg",
                        "value": f"{out_leg:.1f}m < {min_leg}m",
                        "lon": p_i.lon, "lat": p_i.lat,
                    })
                continue

            elapsed = track[i + w].timestamp - track[i - w].timestamp
            if elapsed <= 0.0:
                stats["reject_degenerate_ts"] += 1
                continue

            movement_speed = (in_leg + out_leg) / elapsed
            if movement_speed < min_speed:
                stats["reject_speed"] += 1
                if verbose and len(sample_rejects) < 20:
                    sample_rejects.append({
                        "vid": vid, "idx": i, "reason": "speed",
                        "value": f"{movement_speed:.2f}m/s < {min_speed}m/s",
                        "lon": p_i.lon, "lat": p_i.lat,
                    })
                continue

            in_angles = headings[i - w : i]
            out_angles = headings[i : i + w]
            in_bearing = _circular_mean_deg(in_angles)
            out_bearing = _circular_mean_deg(out_angles)
            turn = _angle_delta_deg(in_bearing, out_bearing)
            abs_turn = abs(turn)

            # 收集转向角度用于分布统计
            if len(heading_stats) < 10000:
                heading_stats.append(abs_turn)

            if abs_turn < min_turn:
                stats["reject_turn_too_small"] += 1
                continue

            if abs_turn > max_turn:
                stats["reject_turn_too_large"] += 1
                continue

            # ── 通过！ ──
            stats["accepted"] += 1
            track_has_event = True
            if len(sample_turns) < 10:
                sample_turns.append({
                    "vid": vid, "idx": i,
                    "lon": p_i.lon, "lat": p_i.lat,
                    "in_leg": f"{in_leg:.1f}m",
                    "out_leg": f"{out_leg:.1f}m",
                    "speed": f"{movement_speed:.2f}m/s",
                    "abs_turn": f"{abs_turn:.1f}°",
                    "in_bearing": f"{in_bearing:.1f}°",
                    "out_bearing": f"{out_bearing:.1f}°",
                })

        if track_has_event:
            stats["tracks_with_events"] += 1

        # 进度
        if stats["total_tracks"] % 100 == 0:
            elapsed = time.perf_counter() - t_start
            print(f"  ... {stats['total_tracks']:,} tracks, "
                  f"{stats['accepted']} events ({elapsed:.1f}s)")

    conn.close()
    stats["_elapsed"] = time.perf_counter() - t_start

    # ── Heading angle distribution ──
    if heading_stats:
        heading_stats.sort()
        n = len(heading_stats)
        stats["heading_p50"] = heading_stats[n // 2]
        stats["heading_p90"] = heading_stats[n * 90 // 100]
        stats["heading_p95"] = heading_stats[n * 95 // 100]
        stats["heading_p99"] = heading_stats[n * 99 // 100]
        stats["heading_max"] = heading_stats[-1]
        stats["heading_over_45"] = sum(1 for a in heading_stats if a >= 45.0)

    # ── Print summary ──
    _print_diagnosis(stats, sample_turns, sample_rejects, verbose)

    return stats


def _print_diagnosis(stats, sample_turns, sample_rejects, verbose):
    """ZH: 打印诊断报告。"""
    print()
    print("=" * 65)
    print("DIAGNOSIS REPORT / 诊断报告")
    print("=" * 65)

    total = stats["total_tracks"]
    candidates = stats["candidates_checked"]

    print(f"  Total tracks:              {total:>8,}")
    print(f"  Total points:              {stats['total_points']:>8,}")
    print(f"  Tracks too short (<5pts):  {stats['too_short']:>8,}  ({stats['too_short']/max(total,1)*100:5.1f}%)")
    print()

    if candidates == 0:
        print("  ** NO CANDIDATE WINDOWS CHECKED **")
        print("  所有轨迹都因长度不足被跳过，或数据集中根本没有足够多的点。")
        print("  可能原因：")
        print("    1. 清洗阶段把所有长轨迹都切成了 <5 点的短段")
        print("    2. 数据集中车辆轨迹本身就极短")
        print("    3. 时间间隙切分 (max_gap_s) 过小导致过度切分")
        return

    print(f"  Candidate windows checked: {candidates:>8,}")
    print(f"  ── Rejection breakdown ──")
    _bar("in_leg < min_leg_m",       stats["reject_in_leg"],       candidates)
    _bar("out_leg < min_leg_m",      stats["reject_out_leg"],      candidates)
    _bar("movement_speed < min",     stats["reject_speed"],        candidates)
    _bar("abs_turn < min_turn_deg",  stats["reject_turn_too_small"], candidates)
    _bar("abs_turn > max_turn_deg",  stats["reject_turn_too_large"], candidates)
    _bar("degenerate timestamps",    stats["reject_degenerate_ts"], candidates)
    print(f"  {'─' * 50}")
    _bar("ACCEPTED (turn events)",   stats["accepted"],            candidates, style="GREEN")
    print()
    print(f"  Tracks with >=1 event:     {stats['tracks_with_events']:>8,}  ({stats['tracks_with_events']/max(total,1)*100:5.1f}%)")

    # ── Heading angle percentiles ──
    if "heading_p50" in stats:
        print()
        print(f"  ── Abs-turn angle distribution ({len([k for k in stats if k.startswith('heading_')]) - 3} percentiles) ──")
        print(f"  P50={stats['heading_p50']:.1f}°  "
              f"P90={stats['heading_p90']:.1f}°  "
              f"P95={stats['heading_p95']:.1f}°  "
              f"P99={stats['heading_p99']:.1f}°  "
              f"Max={stats['heading_max']:.1f}°")
        n_heading = sum(1 for k in stats if k == "heading_over_45")
        if "heading_over_45" in stats:
            total_angles = sum(1 for k in stats if k.startswith("heading_") and k != "heading_over_45" and k != "heading_max" and "_" not in k[8:] and k not in ("heading_p50","heading_p90","heading_p95","heading_p99"))
            # Actually just use the heading_stats count
            print(f"  ≥45° turns: {stats['heading_over_45']:,}/{candidates} candidates "
                  f"({stats['heading_over_45']/max(candidates,1)*100:.2f}%)")

    # ── 诊断建议 ──
    print()
    print(f"  ── 诊断建议 / Recommendations ──")
    if stats["too_short"] == total:
        print(f"  !! 全部 {total} 条轨迹点数均 < {2*2+1} → 无候选可选。")
        print(f"  !! 检查 CleanerConfig.max_gap_s（默认 300s）是否切得太碎。")
    elif stats["reject_in_leg"] > candidates * 0.5:
        print(f"  !! {stats['reject_in_leg']/candidates*100:.0f}% 候选因 in_leg < 35m 被拒。")
        print(f"  !! 可能原因：点太密（GPS 1Hz→w=2 时 2 点间距仅 ~10-30m）")
        print(f"  !! 尝试：增大 turn_window（如 3→5）让窗口腿长翻倍")
    elif stats["reject_turn_too_small"] > candidates * 0.8:
        print(f"  !! {stats['reject_turn_too_small']/candidates*100:.0f}% 候选因 abs_turn < 45° 被拒。")
        print(f"  !! 可能原因：数据是高速公路场景，弯曲度不足以触发 min_turn_deg={stats.get('_min_turn',45)}°")
        print(f"  !! 尝试：降低 min_turn_deg（如 30°）")
    elif stats["reject_speed"] > candidates * 0.5:
        print(f"  !! {stats['reject_speed']/candidates*100:.0f}% 候选因速度过低被拒。")
        print(f"  !! 可能原因：城市拥堵数据或清洗后注入点速度=0")
        print(f"  !! 尝试：降低 min_movement_speed_mps（如 0.5 m/s）")
    elif stats["accepted"] == 0:
        print(f"  !! 候选被多种条件分散拦截，无单一主导原因。")
        print(f"  !! 运行 --verbose 查看样本拦截详情。")
    else:
        print(f"  OK: {stats['accepted']} turn events detected → 转向检测本身正常。")
        print(f"  如果 pipeline 仍报告 0，检查 G* 热点筛选是否过严（g_star_min_z={stats.get('_g_star_min_z',0.5)}）。")

    # ── Samples ──
    if sample_turns:
        print()
        print(f"  ── 成功检测样本 / Accepted samples ({len(sample_turns)}) ──")
        for s in sample_turns:
            print(f"    vid={s['vid']}, idx={s['idx']}, "
                  f"in_leg={s['in_leg']}, out_leg={s['out_leg']}, "
                  f"speed={s['speed']}, abs_turn={s['abs_turn']}, "
                  f"pos=({s['lon']:.5f},{s['lat']:.5f})")

    if verbose and sample_rejects:
        print()
        print(f"  ── 拦截样本 / Rejected samples ({len(sample_rejects)}) ──")
        for s in sample_rejects:
            print(f"    vid={s['vid']}, idx={s['idx']}, {s['reason']}: {s['value']}, "
                  f"pos=({s['lon']:.5f},{s['lat']:.5f})")

    print()
    print(f"  Total elapsed: {stats['_elapsed']:.1f}s")


def _bar(label: str, count: int, total: int, style: str = ""):
    pct = count / max(total, 1) * 100
    bar_len = 24
    filled = int(bar_len * count / max(total, 1))
    bar_str = "#" * filled + "-" * (bar_len - filled)
    prefix = ">> " if style == "GREEN" else "   "
    print(f"  {prefix}{label:<25s} {count:>8,} ({pct:5.1f}%) [{bar_str}]")


# ═══════════════════════════════════════════════════════════════════════════


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Diagnose why turn event detection returns zero events."
    )
    parser.add_argument("--table", default="trajectory_points",
                        help="Target trajectory_points table (default: trajectory_points).")
    parser.add_argument("--max-tracks", type=int, default=500,
                        help="Max tracks to inspect (-1 = all, default: 500).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print sample rejected candidates.")
    parser.add_argument("--source-id",
                        help="Filter by source_id column (optional).")
    args = parser.parse_args()

    print(f"Target DB: {TEST_DB_NAME}")
    print(f"TRACK_BENCH_DB override: {os.environ.get('TRACK_BENCH_DB', '(not set)')}")
    print()

    result = diagnose_turn_events(
        table=args.table,
        source_id=args.source_id,
        max_tracks=args.max_tracks if args.max_tracks > 0 else 2**31,
        verbose=args.verbose,
    )

    if not result:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
