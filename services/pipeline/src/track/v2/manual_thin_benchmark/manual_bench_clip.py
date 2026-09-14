"""Thin in-memory benchmark for schema-v4 resolved active-set Clip assembly.

Detect owns geometric root solving in v3. This fixture supplies a final p/i/c
sequence and measures Clip validation, active-set updates, minimal
``ClipPoint`` construction, and polyline distance calculation.
"""

from __future__ import annotations

import argparse
import math
import statistics
import time

from track.v2.clip import extract_clips
from track.v2.models import (
    ClipInput,
    ClipIntersection,
    ClipTrack,
    ProjectionConfig,
    ResolvedTrackPoint,
)
from track.v2.projection import create_spatial_context


_METERS_PER_DEGREE = 111_194.9266
_SPATIAL_CONTEXT = create_spatial_context(ProjectionConfig(processing_srid=3857))


def _fixture(point_count: int, intersection_count: int) -> ClipInput:
    if point_count < intersection_count * 3 + 2:
        raise ValueError("--points must be at least 3 * --intersections + 2")
    points: list[ResolvedTrackPoint] = [
        ResolvedTrackPoint(1, -100.0 / _METERS_PER_DEGREE, 0.0)
    ]
    intersections: list[ClipIntersection] = []
    next_resolved_id = 2
    for intersection_id in range(1, intersection_count + 1):
        center_x = intersection_id * 100.0
        intersections.append(
            ClipIntersection(
                intersection_id,
                center_x / _METERS_PER_DEGREE,
                0.0,
                20.0,
            )
        )
        for offset, crossing_type in ((-20.0, 1), (20.0, 0)):
            points.append(
                ResolvedTrackPoint(
                    resolved_point_id=next_resolved_id,
                    lon=(center_x + offset) / _METERS_PER_DEGREE,
                    lat=0.0,
                    crossing_id=intersection_id * 2 + crossing_type,
                    intersection_id=intersection_id,
                    crossing_type=crossing_type,
                )
            )
            next_resolved_id += 1
            if crossing_type == 1:
                points.append(
                    ResolvedTrackPoint(
                        next_resolved_id,
                        center_x / _METERS_PER_DEGREE,
                        0.0,
                    )
                )
                next_resolved_id += 1

    final_x = (intersection_count + 1) * 100.0
    while len(points) < point_count:
        progress = (len(points) - intersection_count) / max(
            point_count - intersection_count - 1,
            1,
        )
        points.append(
            ResolvedTrackPoint(
                next_resolved_id,
                (final_x + progress * 10_000.0) / _METERS_PER_DEGREE,
                10.0 * math.sin(progress * math.tau) / _METERS_PER_DEGREE,
            )
        )
        next_resolved_id += 1

    return ClipInput(
        intersections=tuple(intersections),
        tracks=(
            ClipTrack(
                "benchmark-vehicle",
                "benchmark-trip",
                tuple(points),
                subtrack_index=1,
            ),
        ),
        spatial_context=_SPATIAL_CONTEXT,
        detect_id="benchmark",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark resolved active-set Clip")
    parser.add_argument("--points", type=int, default=20_000)
    parser.add_argument("--intersections", type=int, default=2_500)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.points < 2 or args.intersections < 1 or args.repetitions < 1:
        parser.error("points >= 2, intersections >= 1, and repetitions >= 1")
    try:
        source = _fixture(args.points, args.intersections)
    except ValueError as exc:
        parser.error(str(exc))

    samples: list[float] = []
    result = None
    for _ in range(args.repetitions):
        started = time.perf_counter()
        result = extract_clips(source)
        samples.append(time.perf_counter() - started)
    assert result is not None
    median = statistics.median(samples)
    print(f"points={args.points:,}")
    print(f"intersections={args.intersections:,}")
    print(f"repetitions={args.repetitions:,}")
    print(f"assembly_samples_s={samples}")
    print(f"assembly_median_s={median:.6f}")
    print(f"assembly_per_point_us={median / args.points * 1_000_000:.3f}")
    print(f"clips={result.report.total_clips:,}")
    print(f"between_intersection={result.report.between_intersection_clips:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
