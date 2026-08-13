"""Behavior tests for the v2 Clip stage."""

from __future__ import annotations

import unittest

from track.v2 import (
    ClipConfig,
    ClipInput,
    ClipIntersection,
    ClipTrack,
    PointRef,
    TrajectoryPoint,
    extract_clips,
    extract_clips_for_detect,
)


_METERS_PER_DEGREE = 111_194.92664455874


def _point(vehicle_id: str, index: int, x_m: float, y_m: float = 0.0) -> TrajectoryPoint:
    return TrajectoryPoint(
        point_ref=PointRef("synthetic", str(index), stable=True),
        vehicle_id=vehicle_id,
        timestamp=float(index * 10),
        lon=x_m / _METERS_PER_DEGREE,
        lat=y_m / _METERS_PER_DEGREE,
        heading=90.0,
        speed=5.0,
    )


class ClipExtractionTest(unittest.TestCase):
    def test_two_intersections_produce_one_clip_with_interpolated_endpoints(self) -> None:
        points = tuple(
            _point("vehicle-1", index, x_m)
            for index, x_m in enumerate(range(-300, 301, 50))
        )
        source = ClipInput(
            detect_id="detect-1",
            intersections=(
                ClipIntersection(cluster_id=10, lon=-100 / _METERS_PER_DEGREE, lat=0.0, radius_m=20.0),
                ClipIntersection(cluster_id=20, lon=100 / _METERS_PER_DEGREE, lat=0.0, radius_m=20.0),
            ),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual(result.report.total_clips, 3)
        self.assertEqual(result.report.inter_intersection_clips, 1)
        middle = next(clip for clip in result.clips if clip.clip_type == "inter_intersection")
        self.assertEqual(middle.start.kind, "exit")
        self.assertEqual(middle.start.cluster_id, 10)
        self.assertAlmostEqual(middle.start.lon * _METERS_PER_DEGREE, -80.0, places=6)
        self.assertEqual(middle.end.kind, "entry")
        self.assertEqual(middle.end.cluster_id, 20)
        self.assertAlmostEqual(middle.end.lon * _METERS_PER_DEGREE, 80.0, places=6)
        self.assertEqual([point.lon * _METERS_PER_DEGREE for point in middle.points], [-50.0, 0.0, 50.0])
        self.assertAlmostEqual(middle.distance_m, 160.0, places=5)

    def test_track_without_intersections_is_one_full_boundary_clip(self) -> None:
        points = tuple(_point("vehicle-1", index, x_m) for index, x_m in enumerate((-50, 0, 50)))
        source = ClipInput(
            detect_id="detect-1",
            intersections=(),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual(result.report.total_clips, 1)
        self.assertEqual(result.report.boundary_clips, 1)
        self.assertEqual(result.clips[0].clip_type, "full_track")
        self.assertEqual(result.clips[0].start.kind, "trip_start")
        self.assertEqual(result.clips[0].end.kind, "trip_end")

    def test_track_entirely_inside_an_intersection_produces_no_road_clip(self) -> None:
        source = ClipInput(
            intersections=(ClipIntersection(10, 0.0, 0.0, 20.0),),
            tracks=(
                ClipTrack(
                    "vehicle-1",
                    "trip-1",
                    (_point("vehicle-1", 0, -10), _point("vehicle-1", 1, 10)),
                ),
            ),
        )

        result = extract_clips(source)

        self.assertEqual(result.clips, ())

    def test_outside_track_reversing_at_boundary_is_not_split(self) -> None:
        source = ClipInput(
            intersections=(ClipIntersection(10, 0.0, 0.0, 20.0),),
            tracks=(
                ClipTrack(
                    "vehicle-1",
                    "trip-1",
                    (
                        _point("vehicle-1", 0, -100),
                        _point("vehicle-1", 1, -20),
                        _point("vehicle-1", 2, -100),
                    ),
                ),
            ),
        )

        result = extract_clips(source)

        self.assertEqual([clip.clip_type for clip in result.clips], ["full_track"])

    def test_one_long_segment_can_enter_and_exit_the_same_intersection(self) -> None:
        points = (_point("vehicle-1", 0, -200), _point("vehicle-1", 1, 200))
        source = ClipInput(
            intersections=(ClipIntersection(cluster_id=10, lon=0.0, lat=0.0, radius_m=20.0),),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual([clip.clip_type for clip in result.clips], ["boundary_start", "boundary_end"])
        self.assertAlmostEqual(result.clips[0].end.lon * _METERS_PER_DEGREE, -20.0, places=6)
        self.assertAlmostEqual(result.clips[1].start.lon * _METERS_PER_DEGREE, 20.0, places=6)

    def test_boundary_clips_can_be_disabled(self) -> None:
        points = tuple(
            _point("vehicle-1", index, x_m)
            for index, x_m in enumerate(range(-300, 301, 50))
        )
        source = ClipInput(
            intersections=(
                ClipIntersection(cluster_id=10, lon=-100 / _METERS_PER_DEGREE, lat=0.0, radius_m=20.0),
                ClipIntersection(cluster_id=20, lon=100 / _METERS_PER_DEGREE, lat=0.0, radius_m=20.0),
            ),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source, config=ClipConfig(include_boundary_clips=False))

        self.assertEqual([clip.clip_type for clip in result.clips], ["inter_intersection"])
        self.assertEqual(result.report.boundary_clips, 0)

    def test_same_intersection_reentry_does_not_leave_a_stale_exit(self) -> None:
        points = tuple(_point("vehicle-1", index, x_m) for index, x_m in enumerate((0, 100, 0)))
        source = ClipInput(
            intersections=(ClipIntersection(cluster_id=10, lon=0.0, lat=0.0, radius_m=20.0),),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual(result.clips, ())
        self.assertEqual(result.report.total_clips, 0)

    def test_multiple_intersections_on_one_segment_are_ordered_by_fraction(self) -> None:
        points = (_point("vehicle-1", 0, -300), _point("vehicle-1", 1, 300))
        source = ClipInput(
            intersections=(
                ClipIntersection(cluster_id=10, lon=-100 / _METERS_PER_DEGREE, lat=0.0, radius_m=20.0),
                ClipIntersection(cluster_id=20, lon=100 / _METERS_PER_DEGREE, lat=0.0, radius_m=20.0),
            ),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        middle = next(clip for clip in result.clips if clip.clip_type == "inter_intersection")
        self.assertEqual((middle.start.cluster_id, middle.end.cluster_id), (10, 20))
        self.assertLess(middle.start.timestamp, middle.end.timestamp)

    def test_tangent_contact_does_not_count_as_entering_an_intersection(self) -> None:
        points = (_point("vehicle-1", 0, -100, 20), _point("vehicle-1", 1, 100, 20))
        source = ClipInput(
            intersections=(ClipIntersection(cluster_id=10, lon=0.0, lat=0.0, radius_m=20.0),),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual([clip.clip_type for clip in result.clips], ["full_track"])

    def test_unordered_track_is_rejected_at_the_public_interface(self) -> None:
        first = _point("vehicle-1", 2, -50)
        second = _point("vehicle-1", 1, 50)
        source = ClipInput(
            intersections=(),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=(first, second)),),
        )

        with self.assertRaisesRegex(ValueError, "not ordered by timestamp"):
            extract_clips(source)

    def test_invalid_intersection_radius_is_rejected_at_the_public_interface(self) -> None:
        source = ClipInput(
            intersections=(ClipIntersection(1, 0.0, 0.0, 0.0),),
            tracks=(
                ClipTrack(
                    "vehicle-1",
                    "trip-1",
                    (_point("vehicle-1", 0, -10), _point("vehicle-1", 1, 10)),
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "positive radius"):
            extract_clips(source)


class _ReplayCursor:
    def __init__(self, connection: "_ReplayConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "_ReplayCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.connection.executions.append((sql, params))
        self.rows = self.connection.responses.pop(0)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _ReplayConnection:
    def __init__(self, responses: list[list[tuple[object, ...]]]) -> None:
        self.responses = responses
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def cursor(self) -> _ReplayCursor:
        return _ReplayCursor(self)


class ClipDatabaseReplayTest(unittest.TestCase):
    def test_completed_detect_loads_intersections_then_parent_clean_members(self) -> None:
        point_rows = [
            (
                "vehicle-1", 1, index, "synthetic", str(index + 1),
                float(index * 10), x / _METERS_PER_DEGREE, 0.0, 90.0, 5.0, {},
                None, None, "",
            )
            for index, x in enumerate(range(-300, 301, 50))
        ]
        point_rows.append(
            (
                "vehicle-1", 1, 99, "synthetic", "6->7:orthogonal_heading",
                55.0, 0.0, 0.0, 45.0, 0.0, {}, "6", "7", "orthogonal_heading",
            )
        )
        point_rows.sort(key=lambda row: float(row[5]))
        conn = _ReplayConnection([
            [("clean-1", "synthetic", {"target_table": "trajectory_points"}, "completed")],
            [
                (10, -100 / _METERS_PER_DEGREE, 0.0, 20.0),
                (20, 100 / _METERS_PER_DEGREE, 0.0, 20.0),
            ],
            point_rows,
        ])

        result = extract_clips_for_detect(conn, "detect-1")

        self.assertEqual(result.detect_id, "detect-1")
        self.assertEqual(result.report.inter_intersection_clips, 1)
        point_query, point_params = conn.executions[2]
        self.assertIn("kept_trajectory_points", point_query)
        self.assertIn("injected_points", point_query)
        self.assertIn("ktp.source_id", point_query)
        self.assertNotIn("is_active", point_query)
        self.assertIn(
            "ORDER BY vehicle_id, subtrack_index, anchor_sequence, point_kind",
            point_query,
        )
        self.assertNotIn("ORDER BY vehicle_id, subtrack_index, timestamp", point_query)
        self.assertEqual(point_params, ("clean-1", "clean-1"))
        self.assertEqual(result.clips[0].points[0].point_ref.source_id, "synthetic")

    def test_incomplete_run_is_rejected_before_loading_artifacts(self) -> None:
        conn = _ReplayConnection([
            [("clean-1", "synthetic", {"target_table": "trajectory_points"}, "running")],
        ])

        with self.assertRaisesRegex(ValueError, "is not completed"):
            extract_clips_for_detect(conn, "detect-1")

        self.assertEqual(len(conn.executions), 1)

    def test_run_without_persisted_clip_radius_is_rejected(self) -> None:
        conn = _ReplayConnection([
            [("clean-1", "synthetic", {"target_table": "trajectory_points"}, "completed")],
            [(10, 0.0, 0.0, None)],
        ])

        with self.assertRaisesRegex(ValueError, "predates Clip radius persistence"):
            extract_clips_for_detect(conn, "legacy-detect")


if __name__ == "__main__":
    unittest.main()
