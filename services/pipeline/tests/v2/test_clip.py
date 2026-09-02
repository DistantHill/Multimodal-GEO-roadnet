"""Behavior tests for the v2 Clip stage."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from unittest.mock import patch

import track.v2.clip as clip_module

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
from track.v2.models import SpatialContext
from track.v2.projection import CoordinateTransformer


_METERS_PER_DEGREE = 111_319.49079327358
_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=3857,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
)
_TRANSFORMER = CoordinateTransformer(_SPATIAL_CONTEXT)


def _point(vehicle_id: str, index: int, x_m: float, y_m: float = 0.0) -> TrajectoryPoint:
    lon, lat = _TRANSFORMER.inverse(x_m, y_m)
    return TrajectoryPoint(
        point_ref=PointRef("synthetic", str(index), stable=True),
        vehicle_id=vehicle_id,
        timestamp=float(index * 10),
        lon=lon,
        lat=lat,
        heading=90.0,
        speed=5.0,
        x=x_m,
        y=y_m,
    )


def _clip_input(
    *,
    intersections: tuple[ClipIntersection, ...],
    tracks: tuple[ClipTrack, ...],
    detect_id: str | None = None,
) -> ClipInput:
    return ClipInput(
        intersections=intersections,
        tracks=tracks,
        spatial_context=_SPATIAL_CONTEXT,
        detect_id=detect_id,
    )


class ClipExtractionTest(unittest.TestCase):
    def test_intersection_grid_is_reused_across_tracks(self) -> None:
        source = _clip_input(
            intersections=(ClipIntersection(10, 0.0, 0.0, 20.0),),
            tracks=tuple(
                ClipTrack(
                    vehicle_id,
                    f"trip-{vehicle_id}",
                    (_point(vehicle_id, 0, -100), _point(vehicle_id, 1, 100)),
                )
                for vehicle_id in ("vehicle-1", "vehicle-2")
            ),
        )

        with patch.object(
            clip_module,
            "_IntersectionGrid",
            wraps=clip_module._IntersectionGrid,
        ) as grid_factory:
            extract_clips(source)

        self.assertEqual(grid_factory.call_count, 1)

    def test_segment_endpoints_are_projected_once_per_track(self) -> None:
        track = ClipTrack(
            "vehicle-1",
            "trip-1",
            tuple(_point("vehicle-1", index, x) for index, x in enumerate((-100, 0, 100))),
        )
        grid = clip_module._IntersectionGrid(
            (ClipIntersection(10, 0.0, 0.0, 20.0),),
            _TRANSFORMER,
        )
        original_points_xy = grid.points_xy
        projection_count = 0

        def counted_points_xy(
            points: tuple[TrajectoryPoint, ...],
        ) -> tuple[tuple[float, float], ...]:
            nonlocal projection_count
            projection_count += len(points)
            return original_points_xy(points)

        grid.points_xy = counted_points_xy  # type: ignore[method-assign]
        clip_module._crossing_events(track, grid)

        self.assertEqual(projection_count, len(track.points))

    def test_public_clip_input_does_not_trust_unscoped_existing_xy(self) -> None:
        points = tuple(
            _point("vehicle-1", index, x)
            for index, x in enumerate((-200.0, 200.0))
        )
        stale_points = tuple(
            replace(point, x=9_000_000.0 + index, y=-9_000_000.0)
            for index, point in enumerate(points)
        )
        intersections = (
            ClipIntersection(10, -100.0 / _METERS_PER_DEGREE, 0.0, 20.0),
            ClipIntersection(20, 100.0 / _METERS_PER_DEGREE, 0.0, 20.0),
        )

        expected = extract_clips(
            _clip_input(
                intersections=intersections,
                tracks=(ClipTrack("vehicle-1", "trip-1", points),),
            )
        )
        actual = extract_clips(
            _clip_input(
                intersections=intersections,
                tracks=(ClipTrack("vehicle-1", "trip-1", stale_points),),
            )
        )

        self.assertEqual(actual, expected)

    def test_long_segment_scans_intersections_instead_of_all_aabb_cells(self) -> None:
        grid = clip_module._IntersectionGrid((
            ClipIntersection(10, -100 / _METERS_PER_DEGREE, 0.0, 20.0),
            ClipIntersection(20, 100 / _METERS_PER_DEGREE, 0.0, 20.0),
        ), _TRANSFORMER)

        class NoCellLookup(dict):
            def get(self, key: object, default: object = None) -> object:
                raise AssertionError("long AABB should not scan grid cells")

        grid.cells = NoCellLookup(grid.cells)
        candidates = grid.candidates((-10_000.0, 0.0), (10_000.0, 0.0))

        self.assertEqual(
            [candidate.value.cluster_id for candidate in candidates],
            [10, 20],
        )

    def test_two_intersections_produce_one_clip_with_interpolated_endpoints(self) -> None:
        points = tuple(
            _point("vehicle-1", index, x_m)
            for index, x_m in enumerate(range(-300, 301, 50))
        )
        source = _clip_input(
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
        source = _clip_input(
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
        self.assertAlmostEqual(result.clips[0].distance_m, 100.0, places=6)

    def test_fixed_crs_is_independent_of_intersection_order(self) -> None:
        context = SpatialContext(
            source_srid=4326,
            processing_srid=32650,
            projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
        )
        transformer = CoordinateTransformer(context)
        centre_x, centre_y = transformer.forward(116.4, 39.9)
        first_xy = (centre_x - 10_000.0, centre_y - 50_000.0)
        second_xy = (centre_x + 10_000.0, centre_y + 50_000.0)
        length = math.hypot(
            second_xy[0] - first_xy[0],
            second_xy[1] - first_xy[1],
        )
        unit_x = (second_xy[0] - first_xy[0]) / length
        unit_y = (second_xy[1] - first_xy[1]) / length

        def point(index: int, x: float, y: float) -> TrajectoryPoint:
            lon, lat = transformer.inverse(x, y)
            return TrajectoryPoint(
                PointRef("fixed-crs", str(index), True),
                "vehicle-fixed",
                float(index * 10),
                lon,
                lat,
                0.0,
                10.0,
                x=x,
                y=y,
            )

        first_lonlat = transformer.inverse(*first_xy)
        second_lonlat = transformer.inverse(*second_xy)
        intersections = (
            ClipIntersection(10, *first_lonlat, 20.0),
            ClipIntersection(20, *second_lonlat, 20.0),
        )
        track = ClipTrack(
            "vehicle-fixed",
            "trip-fixed",
            (
                point(0, first_xy[0] - 100.0 * unit_x, first_xy[1] - 100.0 * unit_y),
                point(1, second_xy[0] + 100.0 * unit_x, second_xy[1] + 100.0 * unit_y),
            ),
        )

        forward = extract_clips(ClipInput(intersections, (track,), context))
        reversed_order = extract_clips(
            ClipInput(tuple(reversed(intersections)), (track,), context)
        )

        self.assertEqual(forward, reversed_order)
        middle = next(
            clip for clip in forward.clips if clip.clip_type == "inter_intersection"
        )
        for endpoint, centre in (
            (middle.start, first_xy),
            (middle.end, second_xy),
        ):
            endpoint_xy = transformer.forward(endpoint.lon, endpoint.lat)
            self.assertAlmostEqual(
                math.hypot(endpoint_xy[0] - centre[0], endpoint_xy[1] - centre[1]),
                20.0,
                places=6,
            )

    def test_track_entirely_inside_an_intersection_produces_no_road_clip(self) -> None:
        source = _clip_input(
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
        source = _clip_input(
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
        source = _clip_input(
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
        source = _clip_input(
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
        source = _clip_input(
            intersections=(ClipIntersection(cluster_id=10, lon=0.0, lat=0.0, radius_m=20.0),),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual(result.clips, ())
        self.assertEqual(result.report.total_clips, 0)

    def test_multiple_intersections_on_one_segment_are_ordered_by_fraction(self) -> None:
        points = (_point("vehicle-1", 0, -300), _point("vehicle-1", 1, 300))
        source = _clip_input(
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
        source = _clip_input(
            intersections=(ClipIntersection(cluster_id=10, lon=0.0, lat=0.0, radius_m=20.0),),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=points),),
        )

        result = extract_clips(source)

        self.assertEqual([clip.clip_type for clip in result.clips], ["full_track"])

    def test_unordered_track_is_rejected_at_the_public_interface(self) -> None:
        first = _point("vehicle-1", 2, -50)
        second = _point("vehicle-1", 1, 50)
        source = _clip_input(
            intersections=(),
            tracks=(ClipTrack(vehicle_id="vehicle-1", trip_id="trip-1", points=(first, second)),),
        )

        with self.assertRaisesRegex(ValueError, "not ordered by timestamp"):
            extract_clips(source)

    def test_invalid_intersection_radius_is_rejected_at_the_public_interface(self) -> None:
        source = _clip_input(
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
        point_rows: list[
            tuple[
                str, int, int, str, str, float, float, float, float, float,
                dict[object, object], str | None, str | None, str, float, float,
            ]
        ] = [
            (
                "vehicle-1", 1, index, "synthetic", str(index + 1),
                float(index * 10), x / _METERS_PER_DEGREE, 0.0, 90.0, 5.0, {},
                None, None, "", float(x), 0.0,
            )
            for index, x in enumerate(range(-300, 301, 50))
        ]
        point_rows.append(
            (
                "vehicle-1", 1, 99, "synthetic", "6->7:orthogonal_heading",
                55.0, 0.0, 0.0, 45.0, 0.0, {}, "6", "7", "orthogonal_heading",
                0.0, 0.0,
            )
        )
        point_rows.sort(key=lambda row: float(row[5]))
        conn = _ReplayConnection([
            [(
                "clean-1", "synthetic", {"target_table": "trajectory_points"},
                "completed", 4326, 3857, _SPATIAL_CONTEXT.projection_config,
            )],
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
            [(
                "clean-1", "synthetic", {"target_table": "trajectory_points"},
                "running", 4326, 3857, _SPATIAL_CONTEXT.projection_config,
            )],
        ])

        with self.assertRaisesRegex(ValueError, "is not completed"):
            extract_clips_for_detect(conn, "detect-1")

        self.assertEqual(len(conn.executions), 1)

    def test_run_without_persisted_clip_radius_is_rejected(self) -> None:
        conn = _ReplayConnection([
            [(
                "clean-1", "synthetic", {"target_table": "trajectory_points"},
                "completed", 4326, 3857, _SPATIAL_CONTEXT.projection_config,
            )],
            [(10, 0.0, 0.0, None)],
        ])

        with self.assertRaisesRegex(ValueError, "predates Clip radius persistence"):
            extract_clips_for_detect(conn, "legacy-detect")


if __name__ == "__main__":
    unittest.main()
