"""Thin wall-clock benchmark for fixed combined/A->B/B->A fitting variants."""

from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from track.v2.fitting import fit_centerlines
from track.v2.models import (
    ClipEndpoint,
    ClipReport,
    ClipResult,
    PointRef,
    ProjectionConfig,
    TrackClip,
    TrajectoryPoint,
)
from track.v2.projection import create_spatial_context


_SPATIAL_CONTEXT = create_spatial_context(ProjectionConfig(processing_srid=3857))


def _clip(index: int, reverse: bool) -> TrackClip:
    start_id, end_id = ((2, 1) if reverse else (1, 2))
    coordinates = list(reversed(range(21))) if reverse else list(range(21))
    vehicle = f"v-{index}"
    points = tuple(
        TrajectoryPoint(
            PointRef("bench", f"{index}-{value}", True), vehicle, float(value),
            value * 0.0001, index * 0.000001, 90.0, 10.0,
        )
        for value in coordinates[1:-1]
    )
    return TrackClip(
        clip_id=f"clip-{index:06d}", vehicle_id=vehicle, trip_id=vehicle,
        clip_type="inter_intersection",
        start=ClipEndpoint("exit", coordinates[0] * 0.0001, index * 0.000001, 0.0, start_id, 0, 0.0),
        end=ClipEndpoint("entry", coordinates[-1] * 0.0001, index * 0.000001, 20.0, end_id, 19, 1.0),
        points=points, distance_m=222.0, point_count=21,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=int, default=200)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    clips = tuple(_clip(index, bool(index % 2)) for index in range(args.clips))
    result = ClipResult(
        clips=clips,
        report=ClipReport(args.clips, args.clips, args.clips, 0, args.clips * 21),
        spatial_context=_SPATIAL_CONTEXT,
    )
    split_s = combined_s = a_to_b_s = b_to_a_s = 0.0
    tracemalloc.start()
    wall_start = time.perf_counter()
    for _ in range(args.iterations):
        start = time.perf_counter()
        forward = tuple(
            clip
            for clip in clips
            if clip.start.intersection_id < clip.end.intersection_id
        )
        reverse = tuple(
            clip
            for clip in clips
            if clip.start.intersection_id > clip.end.intersection_id
        )
        a_result = ClipResult(
            clips=forward,
            report=ClipReport(
                len(forward), len(forward), len(forward), 0, len(forward) * 21,
            ),
            spatial_context=_SPATIAL_CONTEXT,
        )
        b_result = ClipResult(
            clips=reverse,
            report=ClipReport(
                len(reverse), len(reverse), len(reverse), 0, len(reverse) * 21,
            ),
            spatial_context=_SPATIAL_CONTEXT,
        )
        split_s += time.perf_counter() - start
        start = time.perf_counter()
        fit_centerlines(result)
        combined_s += time.perf_counter() - start
        start = time.perf_counter()
        fit_centerlines(a_result)
        a_to_b_s += time.perf_counter() - start
        start = time.perf_counter()
        fit_centerlines(b_result)
        b_to_a_s += time.perf_counter() - start
    wall_s = time.perf_counter() - wall_start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"clips={args.clips} iterations={args.iterations}")
    print(f"split_s={split_s:.6f} share={split_s / wall_s:.1%}")
    print(f"combined_s={combined_s:.6f} share={combined_s / wall_s:.1%}")
    print(f"a_to_b_s={a_to_b_s:.6f} share={a_to_b_s / wall_s:.1%}")
    print(f"b_to_a_s={b_to_a_s:.6f} share={b_to_a_s / wall_s:.1%}")
    print(f"wall_s={wall_s:.6f} peak_mib={peak_bytes / 1024 / 1024:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
