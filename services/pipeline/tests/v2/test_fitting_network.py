from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import math
import numpy as np
import unittest
from dataclasses import replace

from track.v2 import (
    ClipReport,
    ClipResult,
    ClipIntersection,
    TrackClip,
    fit_centerlines as _fit_centerlines,
    fit_centerline_variants as _fit_centerline_variants,
    build_network as _build_network,
)
from track.v2.models import (
    FittingConfig,
    ProjectionConfig,
)
from track.v2.projection import CoordinateTransformer, create_spatial_context


_METERS_PER_DEGREE = 111_319.49079327358
_SPATIAL_CONTEXT = create_spatial_context(projection_config(processing_srid=3857))
_TRANSFORMER = CoordinateTransformer(_SPATIAL_CONTEXT)


def fit_centerlines(*args, **kwargs):
    kwargs.setdefault("config", fitting_config())
    return _fit_centerlines(*args, **kwargs)


def fit_centerline_variants(*args, **kwargs):
    kwargs.setdefault("config", fitting_config())
    return _fit_centerline_variants(*args, **kwargs)


def build_network(*args, **kwargs):
    kwargs.setdefault("config", network_config())
    return _build_network(*args, **kwargs)


def _lon(x_m: float) -> float:
    return _TRANSFORMER.inverse(x_m, 0.0)[0]


def _clip(clip_id: str, start_intersection_id: int, end_intersection_id: int,
          coords_m: list[tuple[float, float]]) -> TrackClip:
    xy = np.asarray(coords_m, dtype=np.float64)
    lonlat = np.asarray([_TRANSFORMER.inverse(x,y) if np.isfinite((x,y)).all() else (x,y)
                         for x,y in xy], dtype=np.float64)
    return TrackClip(
        clip_id=clip_id, vehicle_id=f"vehicle-{clip_id}", trip_id=f"trip-{clip_id}",
        clip_type="between_intersection", xy=xy, lonlat=lonlat,
        resolved_point_ids=np.arange(1,len(xy)-1,dtype=np.int64),
        start_index=0, end_index=len(xy)-1, interior_start=1, interior_end=len(xy)-1,
        start_kind="exit", end_kind="entry", start_intersection_id=start_intersection_id,
        end_intersection_id=end_intersection_id, start_crossing_id=start_intersection_id*10,
        end_crossing_id=end_intersection_id*10+1, distance_m=200., point_count=len(xy),
    )


def _clip_result(*clips: TrackClip) -> ClipResult:
    return ClipResult(
        clips=clips,
        report=ClipReport(
            total_vehicles=len(clips),
            total_subtracks=len(clips),
            total_clips=len(clips),
            between_intersection_clips=sum(
                clip.clip_type == "between_intersection" for clip in clips
            ),
            boundary_start_clips=sum(
                clip.clip_type == "boundary_start" for clip in clips
            ),
            boundary_end_clips=sum(
                clip.clip_type == "boundary_end" for clip in clips
            ),
            full_track_clips=sum(clip.clip_type == "full_track" for clip in clips),
            total_points_in_clips=sum(clip.point_count for clip in clips),
        ),
        spatial_context=_SPATIAL_CONTEXT,
        detect_id="detect-fit-test",
    )


class FittingStageTest(unittest.TestCase):
    def test_fixed_variants_preserve_combined_and_fit_both_directions(self) -> None:
        forward = _clip("forward", 10, 20, [(0, 10), (100, 10), (200, 10)])
        reverse = _clip("reverse", 20, 10, [(200, -10), (100, -10), (0, -10)])
        clips = _clip_result(forward, reverse)

        bundle = fit_centerline_variants(clips)

        self.assertEqual(bundle.combined.as_dict(), fit_centerlines(clips).as_dict())
        self.assertEqual(bundle.combined.centerlines[0].variant, "combined")
        self.assertEqual(bundle.a_to_b.centerlines[0].variant, "directed")
        self.assertEqual(bundle.b_to_a.centerlines[0].variant, "directed")
        self.assertEqual(bundle.a_to_b.centerlines[0].source_clip_ids, ("forward",))
        self.assertEqual(bundle.b_to_a.centerlines[0].source_clip_ids, ("reverse",))
        self.assertEqual(
            (
                bundle.a_to_b.centerlines[0].forward_clip_count,
                bundle.a_to_b.centerlines[0].reverse_clip_count,
            ),
            (1, 0),
        )
        self.assertEqual(
            (
                bundle.b_to_a.centerlines[0].forward_clip_count,
                bundle.b_to_a.centerlines[0].reverse_clip_count,
            ),
            (1, 0),
        )
        self.assertEqual(bundle.combined.spatial_context, _SPATIAL_CONTEXT)
        self.assertEqual(bundle.a_to_b.spatial_context, _SPATIAL_CONTEXT)
        self.assertEqual(bundle.b_to_a.spatial_context, _SPATIAL_CONTEXT)
        self.assertEqual(
            (
                bundle.b_to_a.centerlines[0].from_intersection_id,
                bundle.b_to_a.centerlines[0].to_intersection_id,
            ),
            (20, 10),
        )
        self.assertGreater(
            bundle.b_to_a.centerlines[0].points[0][0],
            bundle.b_to_a.centerlines[0].points[-1][0],
        )

    def test_minimum_clip_count_is_applied_independently_to_three_paths(self) -> None:
        forward = _clip("forward", 10, 20, [(0, 10), (100, 10), (200, 10)])
        reverse = _clip("reverse", 20, 10, [(200, -10), (100, -10), (0, -10)])

        bundle = fit_centerline_variants(
            _clip_result(forward, reverse),
            config=fitting_config(min_clips_per_pair=2),
        )

        self.assertEqual(len(bundle.combined.centerlines), 1)
        self.assertEqual(bundle.a_to_b.centerlines, ())
        self.assertEqual(bundle.b_to_a.centerlines, ())

    def test_bidirectional_tracks_fit_one_canonical_centerline_at_50m_spacing(self) -> None:
        forward = _clip("forward", 10, 20, [(0, 10), (100, 10), (200, 10)])
        reverse = _clip("reverse", 20, 10, [(200, -10), (100, -10), (0, -10)])

        result = fit_centerlines(_clip_result(forward, reverse))

        self.assertEqual(len(result.centerlines), 1)
        centerline = result.centerlines[0]
        self.assertEqual(
            (centerline.from_intersection_id, centerline.to_intersection_id),
            (10, 20),
        )
        self.assertEqual(centerline.source_clip_ids, ("forward", "reverse"))
        self.assertEqual(centerline.forward_clip_count, 1)
        self.assertEqual(centerline.reverse_clip_count, 1)
        self.assertEqual(len(centerline.points), 5)
        for index, (lon, lat) in enumerate(centerline.points):
            self.assertAlmostEqual(lon * _METERS_PER_DEGREE, index * 50.0, delta=0.1)
            self.assertAlmostEqual(lat * _METERS_PER_DEGREE, 0.0, delta=0.1)
        self.assertAlmostEqual(centerline.length_m, 200.0, delta=0.1)

    def test_one_call_fits_all_pairs_and_preserves_the_short_final_interval(self) -> None:
        first_pair = _clip("10-20", 10, 20, [(0, 0), (125, 0)])
        second_pair = _clip("20-30", 20, 30, [(200, 0), (300, 0)])
        boundary = _clip("boundary", 10, 20, [(0, 20), (125, 20)])
        boundary = replace(boundary, clip_type="boundary_start")

        result = fit_centerlines(_clip_result(first_pair, second_pair, boundary))

        self.assertEqual(
            [
                (line.from_intersection_id, line.to_intersection_id)
                for line in result.centerlines
            ],
            [(10, 20), (20, 30)],
        )
        first_x = [lon * _METERS_PER_DEGREE for lon, _ in result.centerlines[0].points]
        self.assertEqual(len(first_x), 4)
        for actual, expected in zip(first_x, (0.0, 50.0, 100.0, 125.0)):
            self.assertAlmostEqual(actual, expected, delta=0.1)
        self.assertEqual(result.report.input_clip_count, 3)
        self.assertEqual(result.report.eligible_clip_count, 2)
        self.assertEqual(result.report.connected_pair_count, 2)

    def test_invalid_clip_and_unfitted_pair_are_both_reported(self) -> None:
        invalid = _clip("invalid", 10, 20, [(0, 0), (float("nan"), 0)])

        result = fit_centerlines(_clip_result(invalid))

        self.assertEqual(result.centerlines, ())
        self.assertEqual(result.report.connected_pair_count, 1)
        self.assertEqual(result.report.skipped_clip_count, 1)
        self.assertEqual(result.report.skipped_pair_count, 1)
        self.assertEqual(
            [(issue.clip_id, issue.reason) for issue in result.issues],
            [("invalid", "invalid_geometry"), (None, "insufficient_clips")],
        )

    def test_conflicting_geometry_is_reported_instead_of_aborting_the_batch(self) -> None:
        eastbound = _clip("eastbound", 10, 20, [(0, 0), (100, 0)])
        mislabeled_westbound = _clip("mislabeled", 10, 20, [(100, 0), (0, 0)])

        result = fit_centerlines(_clip_result(eastbound, mislabeled_westbound))

        self.assertEqual(result.centerlines, ())
        self.assertEqual(result.report.skipped_pair_count, 1)
        self.assertEqual(
            [(issue.clip_id, issue.reason) for issue in result.issues],
            [(None, "degenerate_centerline")],
        )

    def test_unknown_clip_type_is_reported_while_boundaries_are_ignored(self) -> None:
        unknown = _clip("unknown", 10, 20, [(0, 0), (100, 0)])
        unknown = replace(unknown, clip_type="inter_intersecton")
        boundary = replace(unknown, clip_id="boundary", clip_type="boundary_end")

        result = fit_centerlines(_clip_result(unknown, boundary))

        self.assertEqual(result.centerlines, ())
        self.assertEqual(result.report.eligible_clip_count, 0)
        self.assertEqual(result.report.skipped_clip_count, 1)
        self.assertEqual(
            [(issue.clip_id, issue.reason) for issue in result.issues],
            [("unknown", "unsupported_clip_type")],
        )


class NetworkStageTest(unittest.TestCase):
    def test_centerline_becomes_one_edge_between_intersection_nodes(self) -> None:
        forward = _clip("forward", 10, 20, [(0, 0), (100, 0), (200, 0)])
        intersections = (
            ClipIntersection(10, _lon(-20), 0.0, 20.0),
            ClipIntersection(20, _lon(220), 0.0, 20.0),
        )
        clips = replace(_clip_result(forward), intersections=intersections)
        fits = fit_centerlines(clips)

        network = build_network(fits, clips.intersections)

        self.assertEqual([node.intersection_id for node in network.nodes], [10, 20])
        self.assertEqual([node.degree for node in network.nodes], [1, 1])
        self.assertEqual(len(network.edges), 1)
        edge = network.edges[0]
        self.assertEqual(edge.edge_id, "10--20")
        self.assertEqual(
            (edge.from_intersection_id, edge.to_intersection_id),
            (10, 20),
        )
        self.assertIs(edge.points, fits.centerlines[0].points)
        self.assertEqual(edge.source_clip_count, 1)
        self.assertEqual(network.report.isolated_node_count, 0)

    def test_duplicate_or_noncanonical_centerline_is_rejected(self) -> None:
        fits = fit_centerlines(
            _clip_result(_clip("forward", 10, 20, [(0, 0), (200, 0)]))
        )
        intersections = (
            ClipIntersection(10, _lon(-20), 0.0, 20.0),
            ClipIntersection(20, _lon(220), 0.0, 20.0),
        )

        with self.assertRaisesRegex(ValueError, "duplicate centerline"):
            build_network(
                replace(fits, centerlines=(fits.centerlines[0], fits.centerlines[0])),
                intersections,
            )
        with self.assertRaisesRegex(ValueError, "canonical direction"):
            build_network(
                replace(
                    fits,
                    centerlines=(
                        replace(
                            fits.centerlines[0],
                            from_intersection_id=20,
                            to_intersection_id=10,
                        ),
                    ),
                ),
                intersections,
            )
        with self.assertRaisesRegex(ValueError, "requires combined"):
            build_network(
                replace(
                    fits,
                    centerlines=(replace(fits.centerlines[0], variant="directed"),),
                ),
                intersections,
            )


if __name__ == "__main__":
    unittest.main()
