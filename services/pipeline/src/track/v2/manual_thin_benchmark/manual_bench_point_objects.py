"""Synthetic Parser object profile and Refine copy benchmark; no database I/O.

Run: python -m track.v2.manual_thin_benchmark.manual_bench_point_objects
"""

from __future__ import annotations

import cProfile
from dataclasses import replace
import pstats
from timeit import repeat
from unittest.mock import patch

from ..field_config import load_field_config
from ..models import PointRef, ProjectionConfig, TrajectoryPoint
from ..projection import CoordinateTransformer, create_spatial_context
from ..refine import refine_vehicle
from ..parser import _parse_row_from_tuple


def main() -> None:
    specs = {spec.canonical: spec for spec in load_field_config().specs}
    columns = {key: index for index, key in enumerate(
        ("vehicle_id", "timestamp", "lon", "lat", "heading", "speed")
    )}
    row = ("v", 1700000000000, 116.0, 39.0, 90.0, 36.0, 1)
    profile = cProfile.Profile()
    originals = {cls: cls.__init__.__code__ for cls in (PointRef, TrajectoryPoint)}
    try:
        # Generated dataclass initializers share <string>:2:__init__, which
        # pstats otherwise merges. Give each a distinct profiling filename.
        for cls, code in originals.items():
            cls.__init__.__code__ = code.replace(co_filename=f"<{cls.__name__}>")
        profile.enable()
        for ordinal in range(100_000):
            _parse_row_from_tuple(row, ordinal, columns, specs, "source", 6)
        profile.disable()
    finally:
        profile.disable()
        for cls, code in originals.items():
            cls.__init__.__code__ = code
    stats = pstats.Stats(profile)
    print(f"Parser: 100,000 synthetic rows, profiled time {stats.total_tt:.3f}s")
    for key, value in stats.stats.items():
        if key[0] in ("<PointRef>", "<TrajectoryPoint>"):
            print(f"  {key[0]}: {value[0]:,} calls, self {value[2]:.3f}s "
                  f"({value[2] / stats.total_tt:.1%})")

    context = create_spatial_context(ProjectionConfig(processing_srid=32650))
    transformer = CoordinateTransformer(context)
    points = [TrajectoryPoint(
        PointRef("source", str(index), True), "v", float(index),
        116.0 + index * 0.000001, 39.0, 90.0, 10.0,
    ) for index in range(20_000)]

    def run():
        return refine_vehicle(points, spatial_context=context, _transformer=transformer)

    def replace_copy(point, x, y, vehicle_id):
        return replace(point, x=x, y=y, vehicle_id=vehicle_id)

    with patch("track.v2.refine._projected_point", replace_copy):
        baseline = run()
        before = min(repeat(run, number=5, repeat=5)) / 5
    assert run() == baseline
    after = min(repeat(run, number=5, repeat=5)) / 5
    print(f"Refine: 20,000 moving points, replace {before:.4f}s, "
          f"direct copy {after:.4f}s, speedup {before / after:.2f}x")
    print("Output equality verified. Excludes database I/O; not a production speedup claim.")


if __name__ == "__main__":
    main()
