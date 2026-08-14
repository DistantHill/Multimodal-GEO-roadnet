from __future__ import annotations

import unittest
from dataclasses import replace

from track.v2 import (
    ClipEndpoint,
    ClipReport,
    ClipResult,
    ClipIntersection,
    IntersectResult,
    TrackClip,
    TrajectoryPoint,
    PointRef,
    fit_centerlines,
    build_network,
)
from track.v2.models import HotspotCluster, IntersectionTopology, IntersectReport


_METERS_PER_DEGREE = 111_194.92664455874


def _lon(x_m: float) -> float:
    return x_m / _METERS_PER_DEGREE


def _endpoint(kind: str, cluster_id: int, x_m: float, y_m: float) -> ClipEndpoint:
    return ClipEndpoint(
        kind=kind,
        lon=_lon(x_m),
        lat=_lon(y_m),
        timestamp=x_m,
        cluster_id=cluster_id,
        segment_index=0,
        segment_fraction=0.0,
    )


def _point(vehicle_id: str, row_key: str, x_m: float, y_m: float) -> TrajectoryPoint:
    return TrajectoryPoint(
        point_ref=PointRef("fit-test", row_key, stable=True),
        vehicle_id=vehicle_id,
        timestamp=x_m,
        lon=_lon(x_m),
        lat=_lon(y_m),
        heading=90.0,
        speed=10.0,
    )


def _clip(
    clip_id: str,
    start_cluster_id: int,
    end_cluster_id: int,
    coords_m: list[tuple[float, float]],
) -> TrackClip:
    vehicle_id = f"vehicle-{clip_id}"
    start_x, start_y = coords_m[0]
    end_x, end_y = coords_m[-1]
    return TrackClip(
        clip_id=clip_id,
        vehicle_id=vehicle_id,
        trip_id=f"trip-{clip_id}",
        clip_type="inter_intersection",
        start=_endpoint("exit", start_cluster_id, start_x, start_y),
        end=_endpoint("entry", end_cluster_id, end_x, end_y),
        points=tuple(
            _point(vehicle_id, f"{clip_id}-{index}", x_m, y_m)
            for index, (x_m, y_m) in enumerate(coords_m[1:-1], start=1)
        ),
        distance_m=200.0,
        point_count=len(coords_m),
    )


def _clip_result(*clips: TrackClip) -> ClipResult:
    return ClipResult(
        clips=clips,
        report=ClipReport(
            total_vehicles=len(clips),
            total_clips=len(clips),
            inter_intersection_clips=len(clips),
            boundary_clips=0,
            total_points_in_clips=sum(clip.point_count for clip in clips),
        ),
        detect_id="detect-fit-test",
    )


class FittingStageTest(unittest.TestCase):
    def test_bidirectional_tracks_fit_one_canonical_centerline_at_50m_spacing(self) -> None:
        forward = _clip("forward", 10, 20, [(0, 10), (100, 10), (200, 10)])
        reverse = _clip("reverse", 20, 10, [(200, -10), (100, -10), (0, -10)])

        result = fit_centerlines(_clip_result(forward, reverse))

        self.assertEqual(len(result.centerlines), 1)
        centerline = result.centerlines[0]
        self.assertEqual((centerline.from_cluster_id, centerline.to_cluster_id), (10, 20))
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
        boundary = TrackClip(
            clip_id=boundary.clip_id,
            vehicle_id=boundary.vehicle_id,
            trip_id=boundary.trip_id,
            clip_type="boundary_start",
            start=boundary.start,
            end=boundary.end,
            points=boundary.points,
            distance_m=boundary.distance_m,
            point_count=boundary.point_count,
        )

        result = fit_centerlines(_clip_result(first_pair, second_pair, boundary))

        self.assertEqual(
            [(line.from_cluster_id, line.to_cluster_id) for line in result.centerlines],
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
        fits = fit_centerlines(_clip_result(forward))
        intersections = (
            ClipIntersection(10, _lon(-20), 0.0, 20.0),
            ClipIntersection(20, _lon(220), 0.0, 20.0),
        )

        network = build_network(fits, intersections)

        self.assertEqual([node.cluster_id for node in network.nodes], [10, 20])
        self.assertEqual([node.degree for node in network.nodes], [1, 1])
        self.assertEqual(len(network.edges), 1)
        edge = network.edges[0]
        self.assertEqual(edge.edge_id, "10--20")
        self.assertEqual((edge.from_cluster_id, edge.to_cluster_id), (10, 20))
        self.assertEqual(edge.points, fits.centerlines[0].points)
        self.assertEqual(edge.source_clip_count, 1)
        self.assertEqual(network.report.isolated_node_count, 0)

    def test_intersect_result_contributes_only_complete_intersections(self) -> None:
        fits = fit_centerlines(
            _clip_result(_clip("forward", 10, 20, [(0, 0), (200, 0)]))
        )
        intersect_result = IntersectResult(
            turn_events=[],
            hotspot_events=[],
            orthogonal_candidates=[],
            clusters=[
                HotspotCluster(10, 0.0, 0.0, _lon(-20), 0.0, 10.0),
                HotspotCluster(20, 0.0, 0.0, _lon(220), 0.0, 10.0),
                HotspotCluster(30, 0.0, 0.0, _lon(400), 0.0, 10.0),
            ],
            topologies=[
                IntersectionTopology(10, [], {}, 1, True, "complete", 20.0),
                IntersectionTopology(20, [], {}, 1, True, "complete", 20.0),
                IntersectionTopology(30, [], {}, 0, False, "incomplete", 20.0),
            ],
            report=IntersectReport(
                track_count=0,
                turn_event_count=0,
                hotspot_event_count=0,
                orthogonal_candidate_count=0,
                cross_track_candidate_count=0,
                cluster_count=3,
                cluster_count_turn=0,
                cluster_count_ortho=0,
                cluster_count_cross_track=0,
                complete_intersection_count=2,
            ),
        )

        network = build_network(fits, intersect_result)

        self.assertEqual([node.cluster_id for node in network.nodes], [10, 20])
        self.assertEqual(network.report.edge_count, 1)

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
                            from_cluster_id=20,
                            to_cluster_id=10,
                        ),
                    ),
                ),
                intersections,
            )


if __name__ == "__main__":
    unittest.main()
