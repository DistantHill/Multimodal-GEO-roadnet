"""manual_bench_perf_hooks.py — 验证 _perf.py 性能计时框架的正确性与开销.

薄壳脚本 / Thin benchmark script — validates the hierarchical performance-timing
framework in ``track/v2/_perf.py``, which is now wired into
``track/v2/detect.py`` (handle mode builds ``PerfTracker("detect_handle")``
and emits the report via ``log_info(tracker.report())``).

Test dimensions / 测试维度:

  1. Correctness smoke tests  嵌套结构 / track=False / 深度直通 / 提前退出 /
                              报告可解析性
  2. Overhead measurement     timed_iter(track=True) vs 裸循环 vs track=False
                              默认 N=100,000, 输出 per-iteration us + 百分比
  3. Nested depth behavior    5 层嵌套 —— 仅追踪迭代深度 1/2, 深度 >= 3 直通,
                              深度提醒只记在 depth-1 节点
  4. End-to-end detect    真实流水线跑通, 捕获 PerfTracker 报告,
                              验证功能不破坏

用法 / Usage::

    uv run python track/v2/manual_thin_benchmark/manual_bench_perf_hooks.py \
      --processing-srid 32650
    uv run python ... --quiet                 # 关闭逐窗 VERBOSE 打印
    uv run python ... --iterations 200000     # 覆盖开销测试的迭代数
    uv run python ... --data path/to.csv      # E2E 数据源（缺省自动探测
                                              # track/data/Hfm_130pts.csv,
                                              # 不存在则生成合成十字路口 CSV）

方法论 / Methodology (per CLAUDE.md perf rules):
  - 整个 benchmark 用 wall clock 包裹
  - 开销测试用独立累加器 (raw / tracked / untracked)
  - VERBOSE 逐步骤输出
  - 汇总报告含绝对耗时 + 百分比
  - 归一化指标: per-iteration us
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# ── 确保项目根目录在 sys.path 中（直接运行脚本时） ──────────────────────
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from track.v2._perf import PerfTracker, _DEFAULT_MAX_ITER_DEPTH
from track.v2.detect import IntersectConfig, detect_intersections
from track.v2.models import IntersectReport, PointRef, ProjectionConfig, TrajectoryPoint
from track.v2.parser import CsvSource, get_database
from track.v2.projection import create_spatial_context

VERBOSE = True


def _node_names(report: str) -> set[str]:
    """解析 report 的节点行, 提取节点名集合 (跳过 header 行).

    注意: 不能对整个 report 做子串匹配 —— tracker 名可能包含节点名子串
    (如 "nested5" 含 "d5")。必须按节点行解析。
    """
    names: set[str] = set()
    for ln in report.splitlines()[1:]:  # 第 0 行是 [perf] header
        m = re.match(r"^\s+([^:]+): total=", ln)
        if m:
            names.add(m.group(1))
    return names


# ═══════════════════════════════════════════════════════════════════════════
# 1. Correctness smoke tests
# ═══════════════════════════════════════════════════════════════════════════


def test_nesting_report() -> dict:
    """Stage + timed_iter 嵌套产生预期的层级报告结构."""
    tracker = PerfTracker("nesting_test")
    with tracker.stage("outer"):
        for i in tracker.timed_iter(range(10), name="iter_a", total=10):
            for j in tracker.timed_iter(range(5), name="iter_b"):
                _ = i + j
    report = tracker.report()
    lines = report.splitlines()
    assert lines[0].startswith("[perf] nesting_test"), f"bad header: {lines[0]!r}"

    def _indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip())

    idx_outer = next(i for i, ln in enumerate(lines) if "outer:" in ln)
    idx_a = next(i for i, ln in enumerate(lines) if "iter_a:" in ln)
    idx_b = next(i for i, ln in enumerate(lines) if "iter_b:" in ln)
    # 层级: iter_a 是 outer 的子节点, iter_b 是 iter_a 的子节点
    assert _indent(lines[idx_a]) > _indent(lines[idx_outer])
    assert _indent(lines[idx_b]) > _indent(lines[idx_a])
    assert "calls=10" in lines[idx_a], lines[idx_a]
    assert "calls=50" in lines[idx_b], lines[idx_b]
    assert "items=10" in lines[idx_a], lines[idx_a]
    assert re.search(r"\(\d+(\.\d+)?% of parent\)", lines[idx_a]), lines[idx_a]
    if VERBOSE:
        print(f"    {lines[0]}")
        for ln in lines[1:]:
            print(f"    {ln}")
    return {"report": report}


def test_track_false() -> dict:
    """track=False 产生零节点, 并原对象直通 (零开销)."""
    tracker = PerfTracker("track_false_test")
    data = list(range(50))
    with tracker.stage("s0"):
        for i in tracker.timed_iter(data, name="loop", track=False):
            _ = i
    report = tracker.report()
    node_names = _node_names(report)
    assert "loop" not in node_names, "track=False must not create a node"
    assert "s0" in node_names
    # 同一对象返回 → 零开销直通
    assert tracker.timed_iter(data, name="x", track=False) is data
    assert list(tracker._root.children.keys()) == ["s0"]
    if VERBOSE:
        print(f"    report has only stage node (no iter nodes): {report.splitlines()[0]}")
    return {"report": report}


def test_depth_limit() -> dict:
    """迭代深度 > 3 直通 (零开销), 提醒只记在 depth-1 节点."""
    tracker = PerfTracker("depth_test")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with tracker.stage("s0"):
            for a in tracker.timed_iter(range(3), name="l1"):
                for b in tracker.timed_iter(range(3), name="l2"):
                    for c in tracker.timed_iter(range(3), name="l3"):
                        for d in tracker.timed_iter(range(3), name="l4"):
                            _ = a + b + c + d
    report = tracker.report()
    node_names = _node_names(report)
    assert "l1" in node_names
    assert "l2" in node_names
    assert "l3" in node_names, "depth 3 must be tracked"
    assert "l4" not in node_names, "depth > 3 must passthrough (no node)"
    l1 = tracker._root.children["s0"].children["l1"]
    assert l1.skipped_depth == 4, f"expected skipped_depth=4, got {l1.skipped_depth}"
    assert "[深层迭代未记录]" in report, "report should mark deep-skip on depth-1 node"
    err_text = err.getvalue()
    assert "[perf]" in err_text, "depth reminder must be logged"
    assert err_text.count("[perf]") == 1, (
        f"expected exactly 1 reminder, got {err_text.count('[perf]')}"
    )

    # 深度 4 直通身份: 返回的必须是同一个 iterable 对象
    err2 = io.StringIO()
    with contextlib.redirect_stderr(err2):
        tracker2 = PerfTracker("depth_identity")
        g1 = tracker2.timed_iter(range(3), name="l1")
        next(g1)  # 进入 l1 迭代体 → _iter_depth == 1
        g2 = tracker2.timed_iter(range(3), name="l2")
        next(g2)  # → _iter_depth == 2
        g3 = tracker2.timed_iter(range(3), name="l3")
        next(g3)  # → _iter_depth == 3
        raw = range(5)
        assert tracker2.timed_iter(raw, name="l4") is raw, (
            "depth > 3 must return the same iterable object"
        )
        g3.close()
        g2.close()
        g1.close()
    assert "[perf]" in err2.getvalue(), (
        "depth reminder expected on identity-passthrough call"
    )

    if VERBOSE:
        print(f"    reminder: {err_text.strip()}")
    return {"skipped_depth": l1.skipped_depth, "reminder": err_text.strip()}


def test_nested_depth_5() -> dict:
    """5 层嵌套 —— 追踪迭代深度 1/2/3, 深度 > 3 直通, 提醒 1 次."""
    # _DEFAULT_MAX_ITER_DEPTH=3 表示最大追踪深度为 3, own_depth > 3 才直通,
    # 所以实际被追踪的 timed_iter 层数 = _DEFAULT_MAX_ITER_DEPTH = 3 层
    # (深度 1、2、3)。深度 4+ 直通。
    tracked_levels = _DEFAULT_MAX_ITER_DEPTH
    tracker = PerfTracker("nested5")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with tracker.stage("root"):
            for a in tracker.timed_iter(range(4), name="d1"):
                for b in tracker.timed_iter(range(3), name="d2"):
                    for c in tracker.timed_iter(range(2), name="d3"):
                        for d in tracker.timed_iter(range(2), name="d4"):
                            for e in tracker.timed_iter(range(2), name="d5"):
                                _ = a + b + c + d + e
    report = tracker.report()
    node_names = _node_names(report)
    for name in ("d1", "d2", "d3"):
        assert name in node_names, f"expected {name} tracked (depth 1-3)"
    for name in ("d4", "d5"):
        assert name not in node_names, (
            f"depth > {_DEFAULT_MAX_ITER_DEPTH} must not be tracked: {name}"
        )
    l1 = tracker._root.children["root"].children["d1"]
    assert l1.skipped_depth == 4
    err_text = err.getvalue()
    assert err_text.count("[perf]") == 1, (
        f"exactly one depth reminder expected, got {err_text.count('[perf]')}"
    )
    assert tracked_levels == 3
    if VERBOSE:
        print(
            f"    5-deep nested → tracked levels: d1,d2,d3 (depth 1,2,3); "
            f"d4/d5 passthrough"
        )
        print(f"    reminder logged once on depth-1 node: {err_text.strip()}")
    return {"tracked_levels": tracked_levels, "max_iter_depth": _DEFAULT_MAX_ITER_DEPTH}


def test_early_break() -> dict:
    """提前 break / 抛异常都不崩溃, generator finally 正常出栈."""
    tracker = PerfTracker("early_test")
    it = tracker.timed_iter(range(1000), name="loop")
    seen = 0
    for i, v in enumerate(it):
        if i >= 5:
            break
        seen += 1
    it.close()  # 显式触发 generator finally → _leave
    assert seen == 5
    assert tracker._stack == [], "generator finally must unwind the node stack"
    node = tracker._root.children["loop"]
    assert node.call_count == 5, f"expected 5 accumulated items, got {node.call_count}"

    # 异常路径
    tracker2 = PerfTracker("raise_test")
    it2 = tracker2.timed_iter(range(10), name="loop")
    try:
        for i in it2:
            if i == 3:
                raise ValueError("boom")
    except ValueError:
        pass
    finally:
        it2.close()
    assert tracker2._stack == []
    assert tracker2._root.children["loop"].call_count == 3

    if VERBOSE:
        print("    break after 5 items : stack unwound, node.call_count == 5")
        print("    raise at i=3        : stack unwound, node.call_count == 3")
    return {"break_call_count": node.call_count, "raise_call_count": 3}


def test_report_parseable() -> dict:
    """report 输出可解析, 缩进层级严格递增."""
    tracker = PerfTracker("parse_test")
    with tracker.stage("A"):
        for i in tracker.timed_iter(range(6), name="a_loop"):
            for j in tracker.timed_iter(range(4), name="b_loop"):
                _ = i + j
    report = tracker.report()
    lines = [ln for ln in report.splitlines() if ln.strip()]
    assert lines[0].startswith("[perf] parse_test")
    node_re = re.compile(r"^\s+\S+: total=[\d.]+(?:ns|us|ms|s) calls=\d+ avg=")
    body = lines[1:]
    for ln in body:
        assert node_re.match(ln), f"unparseable node line: {ln!r}"
    indents = [len(ln) - len(ln.lstrip()) for ln in body]
    assert indents[0] == 2, f"top-level node must be indented by 2, got {indents[0]}"
    assert indents == sorted(indents), f"indents must be non-decreasing: {indents}"
    assert max(indents) >= 6, f"expected >= 3 nesting levels, indents={indents}"
    if VERBOSE:
        print(f"    {len(body)} node lines parsed; indents = {indents}")
    return {"node_lines": len(body), "indents": indents}


# ═══════════════════════════════════════════════════════════════════════════
# 2. Overhead measurement
# ═══════════════════════════════════════════════════════════════════════════


def test_overhead(N: int) -> dict:
    """裸循环 vs timed_iter(track=True) vs track=False 的逐迭代开销."""
    # 预热, 稳定 CPU 频率/缓存
    acc = 0
    for i in range(10_000):
        acc += i

    # ── 基线: 裸循环 ──
    t0 = time.perf_counter()
    for i in range(N):
        _ = i * i + i
    raw = time.perf_counter() - t0

    # ── timed_iter(track=True) ──
    tracker = PerfTracker("overhead_test")
    t0 = time.perf_counter()
    for i in tracker.timed_iter(range(N), name="loop", track=True):
        _ = i * i + i
    tracked = time.perf_counter() - t0

    # ── timed_iter(track=False) ──
    t0 = time.perf_counter()
    for i in tracker.timed_iter(range(N), name="untracked", track=False):
        _ = i * i + i
    untracked = time.perf_counter() - t0

    node = tracker._root.children["loop"]
    assert node.call_count == N, f"expected {N} accumulated items, got {node.call_count}"
    assert "untracked" not in tracker._root.children, "track=False must not create a node"

    tracked_us = (tracked - raw) / N * 1e6
    untracked_us = (untracked - raw) / N * 1e6
    tracked_pct = (tracked - raw) / raw * 100.0 if raw > 0 else 0.0
    untracked_pct = (untracked - raw) / raw * 100.0 if raw > 0 else 0.0

    if VERBOSE:
        print(f"    N = {N:,}")
        print(f"    raw loop            : {raw:.4f}s  ({raw / N * 1e9:.0f} ns/iter)")
        print(
            f"    tracked  (track=T)  : {tracked:.4f}s  "
            f"({tracked_us:.2f} us/iter overhead, +{tracked_pct:.0f}% vs raw)"
        )
        print(
            f"    untracked (track=F) : {untracked:.4f}s  "
            f"({untracked_us:.2f} us/iter overhead, +{untracked_pct:.0f}% vs raw)"
        )

    return {
        "N": N,
        "raw": raw,
        "tracked": tracked,
        "untracked": untracked,
        "tracked_us_per_iter": tracked_us,
        "untracked_us_per_iter": untracked_us,
        "tracked_pct": tracked_pct,
        "untracked_pct": untracked_pct,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 3. Nested depth behavior (5 层嵌套见 test_nested_depth_5 上方)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# 4. End-to-end with detect
# ═══════════════════════════════════════════════════════════════════════════


def _make_point(
    vehicle_id: str,
    ref_key: str,
    ts: float,
    lon: float,
    lat: float,
    heading: float,
    speed: float,
    source_id: str = "synthetic",
) -> TrajectoryPoint:
    return TrajectoryPoint(
        point_ref=PointRef(source_id=source_id, source_row_key=ref_key, stable=True),
        vehicle_id=vehicle_id,
        timestamp=ts,
        lon=lon,
        lat=lat,
        heading=heading,
        speed=speed,
    )


def _build_track_through_intersection(
    vehicle_id: str,
    origin_lon: float,
    origin_lat: float,
    approach_bearing: float,
    exit_bearing: float,
) -> list[TrajectoryPoint]:
    """在原点做一次 90° 转弯的合成轨迹 (与 tests/v2/test_detect.py 一致)."""
    points: list[TrajectoryPoint] = []
    ts = 1000.0
    app_rad = math.radians(approach_bearing + 180)
    for i in range(4):
        dist = 80.0 - i * 20.0
        x = dist * math.sin(app_rad)
        y = dist * math.cos(app_rad)
        lon = origin_lon + x / 111320.0
        lat = origin_lat + y / 111320.0
        points.append(_make_point(vehicle_id, f"a{i}", ts, lon, lat, approach_bearing, 10.0))
        ts += 10.0
    exit_rad = math.radians(exit_bearing)
    for i in range(4):
        dist = i * 20.0 + 20.0
        x = dist * math.sin(exit_rad)
        y = dist * math.cos(exit_rad)
        lon = origin_lon + x / 111320.0
        lat = origin_lat + y / 111320.0
        points.append(_make_point(vehicle_id, f"e{i}", ts, lon, lat, exit_bearing, 10.0))
        ts += 10.0
    return points


def _build_cross_csv(tmpdir: Path) -> Path:
    """4 辆车在同一个原点各做 90° 转弯 → 合成十字路口 CSV."""
    origin_lon, origin_lat = 116.391, 39.907
    tracks_data = [
        ("v1", 0.0, 90.0),      # N → E
        ("v2", 90.0, 180.0),    # E → S
        ("v3", 180.0, 270.0),   # S → W
        ("v4", 270.0, 0.0),     # W → N
    ]
    points: list[TrajectoryPoint] = []
    for vid, app, exit_b in tracks_data:
        points.extend(
            _build_track_through_intersection(vid, origin_lon, origin_lat, app, exit_b)
        )
    rows = ["vid,lng,lat,included_angle,gps_speed,gps_time_a"]
    for p in sorted(points, key=lambda p: (p.vehicle_id, p.timestamp)):
        rows.append(f"{p.vehicle_id},{p.lon},{p.lat},{p.heading},{p.speed},{p.timestamp}")
    path = tmpdir / "cross_intersection.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _e2e_run(csv_path: Path, source_label: str, processing_srid: int) -> dict:
    cfg = IntersectConfig(
        use_turn_events=True,
        use_cross_track_candidates=False,  # 只用 Method 1 (与 test_detect I2-23 一致)
        turn_window=2,
        min_turn_deg=45.0,
        max_turn_deg=170.0,
        min_leg_m=20.0,
        min_movement_speed_mps=1.0,
        g_star_radius_m=500.0,
        g_star_min_z=-1.0,
        dbscan_eps_m_turn=500.0,
        dbscan_min_samples_turn=2,
        min_branches=2,
        min_observations=2,
        min_directed_transitions=2,
    )
    report_lines: list[str] = []
    spatial_context = create_spatial_context(
        ProjectionConfig(processing_srid=processing_srid)
    )
    # _detect_intersections_handle 内部调用 log_info(tracker.report()) —— 捕获它
    with patch("track.v2.detect.log_info") as mock_log:
        handle = get_database(CsvSource(csv_path))
        result = detect_intersections(
            handle,
            config=cfg,
            spatial_context=spatial_context,
        )
        handle.close()
        for call in mock_log.call_args_list:
            arg = call.args[0] if call.args else ""
            if isinstance(arg, str) and arg.startswith("[perf] detect_handle"):
                report_lines.append(arg)

    # ── 功能未被破坏 ──
    assert isinstance(result.report, IntersectReport)
    assert result.report.turn_event_count >= 4, (
        f"expected >= 4 turn events, got {result.report.turn_event_count}"
    )
    assert result.report.cluster_count >= 1, (
        f"expected >= 1 cluster, got {result.report.cluster_count}"
    )

    # ── PerfTracker 报告已生成且层级正确 ──
    assert report_lines, "PerfTracker report was not emitted via log_info(tracker.report())"
    report = report_lines[0]
    assert report.startswith("[perf] detect_handle")
    for stage in ("extract_methods_123", "cluster_pipeline", "topology"):
        assert stage in report, f"report missing stage node {stage!r}"
    assert "tracks_turn" in report, "report missing tracks_turn (depth-1) node"
    assert "gstar_events" in report, "report missing gstar_events (depth-1) node"

    if VERBOSE:
        print(f"    source          : {source_label}")
        print(f"    turn_events     : {result.report.turn_event_count}")
        print(f"    clusters        : {result.report.cluster_count}")
        print(f"    complete_topo   : {result.report.complete_intersection_count}")
        print("    ── PerfTracker report ──")
        for ln in report.splitlines():
            print(f"      {ln}")

    return {
        "source": source_label,
        "turn_events": result.report.turn_event_count,
        "clusters": result.report.cluster_count,
        "complete": result.report.complete_intersection_count,
        "report": report,
    }


def test_with_detect(data_path: str | None, processing_srid: int) -> dict:
    """E2E: 真实流水线跑通 + 捕获内部 PerfTracker 报告."""
    if data_path:
        csv_path = Path(data_path)
        assert csv_path.exists(), f"--data file not found: {csv_path}"
        return _e2e_run(csv_path, str(csv_path), processing_srid)

    hfm = Path(_PROJECT_ROOT) / "track" / "data" / "Hfm_130pts.csv"
    if hfm.exists():
        return _e2e_run(hfm, f"{hfm} (real data)", processing_srid)
    with tempfile.TemporaryDirectory() as td:
        csv_path = _build_cross_csv(Path(td))
        return _e2e_run(
            csv_path,
            f"{csv_path} (generated synthetic cross-intersection)",
            processing_srid,
        )


# ═══════════════════════════════════════════════════════════════════════════
# main — 运行全部测试并打印汇总
# ═══════════════════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(
        description="validate _perf.py timing framework (correctness + overhead)"
    )
    parser.add_argument("--quiet", action="store_true", help="disable VERBOSE output")
    parser.add_argument("--iterations", type=int, default=100_000, help="overhead loop N")
    parser.add_argument(
        "--data", type=str, default="",
        help="E2E CSV source (default: auto-detect Hfm_130pts.csv, else generate synthetic)",
    )
    parser.add_argument(
        "--processing-srid",
        type=int,
        required=True,
        help="explicit projected metre EPSG for the complete benchmark dataset",
    )
    args = parser.parse_args()

    # 强制 UTF-8 输出, 避免 GBK/ANSI 控制台把中文渲染成乱码
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass

    global VERBOSE
    VERBOSE = not args.quiet

    print("=" * 72)
    print("  manual_bench_perf_hooks — _perf.py 性能计时框架验证")
    print(f"  迭代数/iterations  : {args.iterations:,}")
    print(f"  E2E 数据/E2E data  : {args.data or 'auto (Hfm_130pts.csv → 合成十字路口)'}")
    print(f"  逐窗打印/Verbose   : {VERBOSE}")
    print("=" * 72)

    suite = [
        ("nesting_report", test_nesting_report, {}),
        ("track_false", test_track_false, {}),
        ("depth_limit", test_depth_limit, {}),
        ("nested_depth_5", test_nested_depth_5, {}),
        ("early_break", test_early_break, {}),
        ("report_parseable", test_report_parseable, {}),
        ("overhead", test_overhead, {"N": args.iterations}),
        (
            "e2e_detect",
            test_with_detect,
            {
                "data_path": args.data or None,
                "processing_srid": args.processing_srid,
            },
        ),
    ]

    wall0 = time.perf_counter()
    results: dict[str, dict] = {}
    timings: dict[str, float] = {}
    failures: list[str] = []

    for name, fn, kwargs in suite:
        print(f"\n── [{name}] ──")
        t0 = time.perf_counter()
        try:
            data = fn(**kwargs)
            dt = time.perf_counter() - t0
            timings[name] = dt
            results[name] = data
            print(f"[PASS] {name} ({dt:.3f}s)")
        except AssertionError as exc:
            dt = time.perf_counter() - t0
            timings[name] = dt
            failures.append(name)
            print(f"[FAIL] {name} ({dt:.3f}s): {exc}")
        except Exception as exc:  # noqa: BLE001 — benchmark 不吞错误, 上报即可
            dt = time.perf_counter() - t0
            timings[name] = dt
            failures.append(name)
            print(f"[ERROR] {name} ({dt:.3f}s): {type(exc).__name__}: {exc}")

    wall = time.perf_counter() - wall0

    # ── 汇总报告 ──
    print("\n" + "=" * 72)
    print("  SUMMARY / 汇总报告")
    print("=" * 72)
    print(f"  whole-run wall clock : {wall:.3f}s")
    print(f"  tests run / passed   : {len(suite)} / {len(suite) - len(failures)}")
    print(
        f"  max iter depth (cfg) : {_DEFAULT_MAX_ITER_DEPTH}  "
        f"(追踪深度 1/2/3, > {_DEFAULT_MAX_ITER_DEPTH} 直通 → 实际追踪 {_DEFAULT_MAX_ITER_DEPTH} 层 timed_iter)"
    )
    print()
    print(f"  {'test':<18} {'result':<8} {'time':>8}")
    print(f"  {'─' * 18} {'─' * 8} {'─' * 8}")
    for name, _fn, _kwargs in suite:
        status = "FAIL" if name in failures else "PASS"
        print(f"  {name:<18} {status:<8} {timings[name]:>7.3f}s")

    ov = results.get("overhead")
    if ov:
        print()
        print(f"  Overhead ({ov['N']:,} iterations, body = i*i+i):")
        print(
            f"    raw loop           : {ov['raw']:.4f}s  "
            f"({ov['raw'] / ov['N'] * 1e9:.0f} ns/iter)"
        )
        print(
            f"    tracked  (track=T) : {ov['tracked']:.4f}s  "
            f"(+{ov['tracked_us_per_iter']:.2f} us/iter, +{ov['tracked_pct']:.0f}%)"
        )
        print(
            f"    untracked (track=F): {ov['untracked']:.4f}s  "
            f"(+{ov['untracked_us_per_iter']:.2f} us/iter, +{ov['untracked_pct']:.0f}%)"
        )
        print(
            "    注: 相对百分比巨大是因循环体本身 ~40ns; "
            "timed_iter 绝对开销约 ~1-2 us/iter, 对真实流水线段影响很小."
        )

    if failures:
        print(f"\n  FAILURES: {', '.join(failures)}")
        return 1
    print("\n  ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
