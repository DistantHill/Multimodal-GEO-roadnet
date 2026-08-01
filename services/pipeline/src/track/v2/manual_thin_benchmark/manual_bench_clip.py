"""Thin benchmark for Clip's segment/intersection scan.

ZH: 生成一条长轨迹和规则分布的交叉口圆，分别累计坐标转换、空间索引
候选查询和线圆求交时间，并单独测量公开 ``extract_clips`` 的墙钟时间。

EN: Generates one long track plus regularly distributed intersection circles,
accumulates coordinate conversion, grid lookup, and root-solving time, and
separately measures the public ``extract_clips`` wall clock.

Usage::

    conda run -n torch python track/v2/manual_thin_benchmark/manual_bench_clip.py
    set VERBOSE=1
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from track.v2.clip import (
    _IntersectionGrid,
    _circle_crossing_fractions,
    extract_clips,
)
from track.v2.models import (
    ClipInput,
    ClipIntersection,
    ClipTrack,
    PointRef,
    TrajectoryPoint,
)


_METERS_PER_DEGREE = 111_194.9266


def _point(index: int, x_m: float, y_m: float) -> TrajectoryPoint:
    return TrajectoryPoint(
        point_ref=PointRef("clip-benchmark", str(index), stable=True),
        vehicle_id="benchmark-vehicle",
        timestamp=float(index),
        lon=x_m / _METERS_PER_DEGREE,
        lat=y_m / _METERS_PER_DEGREE,
        heading=90.0,
        speed=10.0,
    )


def _fixture(point_count: int, intersection_count: int) -> ClipInput:
    route_length_m = 100_000.0
    points = tuple(
        _point(
            index,
            -route_length_m / 2 + route_length_m * index / (point_count - 1),
            300.0 * math.sin(index / 180.0),
        )
        for index in range(point_count)
    )
    rows = 4
    columns = math.ceil(intersection_count / rows)
    spacing_m = route_length_m / max(columns - 1, 1)
    y_offsets = (-300.0, -100.0, 100.0, 300.0)
    intersections = tuple(
        ClipIntersection(
            cluster_id=index,
            lon=(-route_length_m / 2 + (index % columns) * spacing_m) / _METERS_PER_DEGREE,
            lat=y_offsets[index // columns] / _METERS_PER_DEGREE,
            radius_m=55.0,
        )
        for index in range(intersection_count)
    )
    return ClipInput(
        intersections=intersections,
        tracks=(ClipTrack("benchmark-vehicle", "benchmark-trip", points),),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Clip spatial candidate scanning.")
    parser.add_argument("--points", type=int, default=20_000)
    parser.add_argument("--intersections", type=int, default=2_500)
    args = parser.parse_args()
    if args.points < 2 or args.intersections < 1:
        parser.error("--points must be >= 2 and --intersections must be positive")
    verbose = os.environ.get("VERBOSE") == "1"
    source = _fixture(args.points, args.intersections)
    track = source.tracks[0]

    started = time.perf_counter()
    grid = _IntersectionGrid(source.intersections)
    grid_build_s = time.perf_counter() - started

    coordinate_s = 0.0
    candidate_lookup_s = 0.0
    root_s = 0.0
    candidate_count = 0
    crossing_count = 0
    loop_started = time.perf_counter()
    for segment_index, (start, end) in enumerate(zip(track.points, track.points[1:]), start=1):
        phase_started = time.perf_counter()
        start_xy = grid.point_xy(start)
        end_xy = grid.point_xy(end)
        coordinate_s += time.perf_counter() - phase_started

        phase_started = time.perf_counter()
        candidates = grid.candidates(start_xy, end_xy)
        candidate_lookup_s += time.perf_counter() - phase_started
        candidate_count += len(candidates)

        phase_started = time.perf_counter()
        for candidate in candidates:
            relative_start = (start_xy[0] - candidate.x, start_xy[1] - candidate.y)
            relative_end = (end_xy[0] - candidate.x, end_xy[1] - candidate.y)
            crossing_count += len(
                _circle_crossing_fractions(
                    relative_start,
                    relative_end,
                    candidate.value.radius_m,
                )
            )
        root_s += time.perf_counter() - phase_started

        if verbose and segment_index % 2_000 == 0:
            print(
                f"  segments={segment_index:,}/{args.points - 1:,} "
                f"candidates={candidate_count:,} crossings={crossing_count:,}"
            )
    loop_wall_s = time.perf_counter() - loop_started

    started = time.perf_counter()
    result = extract_clips(source)
    public_api_s = time.perf_counter() - started

    accounted_s = coordinate_s + candidate_lookup_s + root_s
    segment_count = args.points - 1
    print(f"points={args.points:,}")
    print(f"intersections={args.intersections:,}")
    print(f"segments={segment_count:,}")
    print(f"grid_build_seconds={grid_build_s:.6f}")
    print(f"scan_wall_seconds={loop_wall_s:.6f}")
    print(f"coordinate_seconds={coordinate_s:.6f} ({coordinate_s / loop_wall_s:.1%})")
    print(f"candidate_lookup_seconds={candidate_lookup_s:.6f} ({candidate_lookup_s / loop_wall_s:.1%})")
    print(f"root_seconds={root_s:.6f} ({root_s / loop_wall_s:.1%})")
    print(f"loop_overhead_seconds={max(0.0, loop_wall_s - accounted_s):.6f}")
    print(f"candidate_checks={candidate_count:,}")
    print(f"crossing_roots={crossing_count:,}")
    print(f"scan_per_segment_us={loop_wall_s / segment_count * 1_000_000:.3f}")
    print(f"candidate_checks_per_segment={candidate_count / segment_count:.4f}")
    print(f"public_extract_seconds={public_api_s:.6f}")
    print(f"public_extract_per_point_us={public_api_s / args.points * 1_000_000:.3f}")
    print(f"clips={result.report.total_clips:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
