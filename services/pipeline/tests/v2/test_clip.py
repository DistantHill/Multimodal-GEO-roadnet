"""Behavior tests for schema-v4 resolved-sequence clipping."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import ANY, patch

from track.v2 import (
    ClipConfig,
    ClipInput,
    ClipIntersection,
    ClipTrack,
    extract_clips,
    extract_clips_for_detect,
)
from track.v2.models import ResolvedTrackPoint, SpatialContext
from track.v2.projection import CoordinateTransformer


_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=3857,
    projection_config={
        "coordinate_semantics_version": 2,
        "enforce_area_of_use": True,
        "max_scale_error": 0.01,
    },
)
_TRANSFORMER = CoordinateTransformer(_CONTEXT)


def _point(
    resolved_point_id: int,
    x_m: float,
    *,
    crossing_id: int | None = None,
    intersection_id: int | None = None,
    crossing_type: int | None = None,
) -> ResolvedTrackPoint:
    lon, lat = _TRANSFORMER.inverse(x_m, 0.0)
    return ResolvedTrackPoint(
        resolved_point_id=resolved_point_id,
        lon=lon,
        lat=lat,
        crossing_id=crossing_id,
        intersection_id=intersection_id,
        crossing_type=crossing_type,
    )


def _crossing(
    resolved_point_id: int,
    x_m: float,
    *,
    crossing_id: int,
    intersection_id: int,
    crossing_type: int,
) -> ResolvedTrackPoint:
    return _point(
        resolved_point_id,
        x_m,
        crossing_id=crossing_id,
        intersection_id=intersection_id,
        crossing_type=crossing_type,
    )


def _input(
    points: tuple[ResolvedTrackPoint, ...],
    *,
    intersections: tuple[ClipIntersection, ...] = (),
    detect_id: str | None = "detect-1",
    subtrack_index: int = 1,
) -> ClipInput:
    return ClipInput(
        intersections=intersections,
        tracks=(ClipTrack("vehicle-1", "subtrack:1", points, subtrack_index),),
        spatial_context=_CONTEXT,
        detect_id=detect_id,
    )


class ResolvedSequenceClipTest(unittest.TestCase):
    def test_two_intersections_produce_boundary_and_between_clips(self) -> None:
        circles = (
            ClipIntersection(10, _point(100, -100.0).lon, 0.0, 20.0),
            ClipIntersection(20, _point(101, 100.0).lon, 0.0, 20.0),
        )
        points = (
            _point(1, -200.0),
            _point(2, -150.0),
            _crossing(3, -120.0, crossing_id=30, intersection_id=10, crossing_type=1),
            _crossing(4, -80.0, crossing_id=31, intersection_id=10, crossing_type=0),
            _point(5, 0.0),
            _crossing(6, 80.0, crossing_id=32, intersection_id=20, crossing_type=1),
            _crossing(7, 120.0, crossing_id=33, intersection_id=20, crossing_type=0),
            _point(8, 150.0),
            _point(9, 200.0),
        )

        result = extract_clips(_input(points, intersections=circles))

        self.assertEqual(
            [clip.clip_type for clip in result.clips],
            ["boundary_end", "between_intersection", "boundary_start"],
        )
        between = result.clips[1]
        self.assertEqual((between.start.kind, between.end.kind), ("exit", "entry"))
        self.assertEqual((between.start.crossing_id, between.end.crossing_id), (31, 32))
        self.assertEqual([point.resolved_point_id for point in between.points], [5])
        self.assertFalse(hasattr(between.points[0], "timestamp"))
        self.assertEqual(result.report.between_intersection_clips, 1)
        self.assertEqual(result.report.boundary_end_clips, 1)
        self.assertEqual(result.report.boundary_start_clips, 1)
        self.assertEqual(result.report.full_track_clips, 0)

    def test_two_point_between_clip_is_valid_sparse_evidence(self) -> None:
        circles = (
            ClipIntersection(10, _point(100, -50.0).lon, 0.0, 20.0),
            ClipIntersection(20, _point(101, 50.0).lon, 0.0, 20.0),
        )
        points = (
            _point(1, -100.0),
            _crossing(2, -70.0, crossing_id=10, intersection_id=10, crossing_type=1),
            _crossing(3, -30.0, crossing_id=11, intersection_id=10, crossing_type=0),
            _crossing(4, 30.0, crossing_id=12, intersection_id=20, crossing_type=1),
            _crossing(5, 70.0, crossing_id=13, intersection_id=20, crossing_type=0),
            _point(6, 100.0),
        )

        result = extract_clips(
            _input(points, intersections=circles),
            config=ClipConfig(include_boundary_clips=False),
        )

        self.assertEqual(len(result.clips), 1)
        self.assertEqual(result.clips[0].clip_type, "between_intersection")
        self.assertEqual(result.clips[0].points, ())
        self.assertEqual(result.clips[0].point_count, 2)
        self.assertGreater(result.clips[0].distance_m, 0.0)

    def test_clip_never_calculates_new_crossing_roots(self) -> None:
        points = (_point(1, -100.0), _point(2, 0.0), _point(3, 100.0))
        result = extract_clips(
            _input(points, intersections=(ClipIntersection(10, 0.0, 0.0, 20.0),))
        )

        # No persisted crossing rows means no split, even though the segment
        # geometry happens to pass through the circle.
        self.assertEqual([clip.clip_type for clip in result.clips], ["full_track"])

    def test_outside_track_without_crossings_is_full_track(self) -> None:
        result = extract_clips(_input((_point(1, -50.0), _point(2, 50.0))))

        self.assertEqual([clip.clip_type for clip in result.clips], ["full_track"])
        clip = result.clips[0]
        self.assertEqual((clip.start.kind, clip.end.kind), ("boundary", "boundary"))
        self.assertEqual(clip.points, ())
        self.assertEqual(result.report.full_track_clips, 1)

    def test_track_entirely_inside_circle_produces_no_clip(self) -> None:
        circle = ClipIntersection(10, 0.0, 0.0, 20.0)
        result = extract_clips(
            _input((_point(1, -10.0), _point(2, 10.0)), intersections=(circle,))
        )
        self.assertEqual(result.clips, ())

    def test_boundaries_can_be_suppressed_without_dropping_between_clip(self) -> None:
        circles = (
            ClipIntersection(10, _point(100, -50.0).lon, 0.0, 10.0),
            ClipIntersection(20, _point(101, 50.0).lon, 0.0, 10.0),
        )
        points = (
            _point(1, -100.0),
            _crossing(2, -60.0, crossing_id=10, intersection_id=10, crossing_type=1),
            _crossing(3, -40.0, crossing_id=11, intersection_id=10, crossing_type=0),
            _point(4, 0.0),
            _crossing(5, 40.0, crossing_id=12, intersection_id=20, crossing_type=1),
            _crossing(6, 60.0, crossing_id=13, intersection_id=20, crossing_type=0),
            _point(7, 100.0),
        )
        result = extract_clips(
            _input(points, intersections=circles),
            config=ClipConfig(include_boundary_clips=False),
        )
        self.assertEqual([clip.clip_type for clip in result.clips], ["between_intersection"])

    def test_same_position_exit_entry_batch_does_not_create_false_road(self) -> None:
        circles = (
            ClipIntersection(10, 0.0, 0.0, 20.0),
            ClipIntersection(20, _point(100, 30.0).lon, 0.0, 20.0),
        )
        points = (
            _point(1, 0.0),
            _crossing(2, 10.0, crossing_id=11, intersection_id=10, crossing_type=0),
            _crossing(3, 10.0, crossing_id=12, intersection_id=20, crossing_type=1),
            _crossing(4, 50.0, crossing_id=13, intersection_id=20, crossing_type=0),
            _point(5, 100.0),
        )

        result = extract_clips(_input(points, intersections=circles))

        self.assertEqual([clip.clip_type for clip in result.clips], ["boundary_start"])
        self.assertEqual(result.clips[0].start.crossing_id, 13)

    def test_invalid_state_transition_fails_instead_of_silently_pairing(self) -> None:
        points = (
            _point(1, -100.0),
            _crossing(2, -20.0, crossing_id=10, intersection_id=10, crossing_type=0),
            _point(3, 100.0),
        )
        with self.assertRaisesRegex(ValueError, "exits inactive intersection"):
            extract_clips(
                _input(points, intersections=(ClipIntersection(10, 0.0, 0.0, 20.0),))
            )

    def test_resolved_identity_and_crossing_metadata_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "repeats a resolved point"):
            extract_clips(_input((_point(1, -10.0), _point(1, 10.0))))
        with self.assertRaisesRegex(ValueError, "all-or-none"):
            extract_clips(
                _input((_point(1, -10.0), _point(2, 10.0, crossing_id=20)))
            )

    def test_duplicate_subtrack_and_invalid_radius_are_rejected(self) -> None:
        track = ClipTrack("v", "s", (_point(1, -10.0), _point(2, 10.0)), 7)
        with self.assertRaisesRegex(ValueError, "duplicate ClipTrack subtrack"):
            extract_clips(ClipInput((), (track, track), _CONTEXT))
        with self.assertRaisesRegex(ValueError, "radius must be positive"):
            extract_clips(
                ClipInput((ClipIntersection(1, 0.0, 0.0, 0.0),), (track,), _CONTEXT)
            )


class DetectClipLoadTest(unittest.TestCase):
    def test_completed_detect_loads_final_resolved_sequence(self) -> None:
        run = SimpleNamespace(
            status="completed",
            refine_id="refine-1",
            spatial_context=_CONTEXT,
        )
        tracks = (ClipTrack("v", "s", (_point(1, -10.0), _point(2, 10.0)), 1),)
        with (
            patch("track.v2.clip.fetch_detect_row", return_value=run),
            patch("track.v2.clip.verify_refine_source_fingerprint") as verify,
            patch("track.v2.clip.fetch_intersections", return_value=[]),
            patch("track.v2.clip.fetch_resolved_clip_tracks", return_value=list(tracks)),
        ):
            result = extract_clips_for_detect(object(), "detect-1")

        verify.assert_called_once_with(ANY, "refine-1")
        self.assertEqual(result.detect_id, "detect-1")
        self.assertEqual(result.report.full_track_clips, 1)

    def test_incomplete_detect_is_rejected_before_child_queries(self) -> None:
        run = SimpleNamespace(status="running", refine_id="refine-1")
        with (
            patch("track.v2.clip.fetch_detect_row", return_value=run),
            patch("track.v2.clip.fetch_intersections") as fetch,
        ):
            with self.assertRaisesRegex(ValueError, "is not completed"):
                extract_clips_for_detect(object(), "detect-1")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
