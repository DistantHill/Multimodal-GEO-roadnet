"""Behavior tests for schema-v4 resolved-sequence clipping."""

from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

from types import SimpleNamespace
import unittest
from unittest.mock import ANY, MagicMock, patch

from track.v2 import (
    ClipConfig,
    ClipInput,
    ClipIntersection,
    ClipTrack,
    extract_clips as _extract_clips,
    extract_clips_for_detect as _extract_clips_for_detect,
)
from track.v2.clips import extract_clips as clip_module
from track.v2.models import ResolvedTrackPoint, RuntimeConfig, SpatialContext
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


def extract_clips(*args, **kwargs):
    kwargs.setdefault("config", clip_config())
    return _extract_clips(*args, **kwargs)


def extract_clips_for_detect(*args, **kwargs):
    kwargs.setdefault("config", clip_config())
    kwargs.setdefault("verify_sha256", False)
    return _extract_clips_for_detect(*args, **kwargs)


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
            config=clip_config(include_boundary_clips=False),
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
            config=clip_config(include_boundary_clips=False),
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
    def test_formal_loader_has_no_legacy_postgres_point_stream_seams(self) -> None:
        self.assertFalse(hasattr(clip_module, "verify_refine_source_fingerprint"))
        self.assertFalse(hasattr(clip_module, "fetch_intersections"))
        self.assertFalse(hasattr(clip_module, "fetch_resolved_clip_tracks"))

    def test_completed_detect_loads_final_resolved_sequence(self) -> None:
        run = SimpleNamespace(
            status="completed",
            refine_id="refine-1",
            spatial_context=_CONTEXT,
        )
        tracks = (ClipTrack("v", "s", (_point(1, -10.0), _point(2, 10.0)), 1),)
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.intersections.return_value = ()
        reader.iter_tracks.return_value = iter(tracks)
        conn = MagicMock()
        with (
            patch("track.v2.clips.extract_clips.fetch_detect_row", return_value=run),
            patch("track.v2.clips.extract_clips.find_detect_artifact", return_value=SimpleNamespace(
                descriptor="descriptor", state="present",
            )),
            patch("track.v2.clips.extract_clips.DetectDatasetReader", return_value=reader) as factory,
        ):
            result = extract_clips_for_detect(
                conn, "detect-1", publication_root="/parquet", data_root="/tmp",
                runtime=runtime_config(),
            )

        factory.assert_called_once_with(
            "/parquet", "descriptor", "present", ANY, verify_sha256=False,
        )
        reader.__exit__.assert_called_once()
        conn.commit.assert_not_called()
        self.assertEqual(result.detect_id, "detect-1")
        self.assertEqual(result.report.full_track_clips, 1)
        result.clips.close()

    def test_artifact_failure_closes_spool_without_postgres_fallback(self) -> None:
        run = SimpleNamespace(status="completed", spatial_context=_CONTEXT)
        conn = MagicMock()
        spool = MagicMock()
        with (
            patch("track.v2.clips.extract_clips.fetch_detect_row", return_value=run),
            patch("track.v2.clips.extract_clips.find_detect_artifact", return_value=SimpleNamespace(
                descriptor="descriptor", state="deleted",
            )),
            patch("track.v2.clips.extract_clips.DetectDatasetReader", side_effect=ValueError("deleted")),
            patch("track.v2.clips.extract_clips.ClipRelation", return_value=spool),
        ):
            with self.assertRaisesRegex(ValueError, "deleted"):
                extract_clips_for_detect(
                    conn, "detect-1", publication_root="/parquet",
                    data_root="/tmp", runtime=runtime_config(),
                )
        spool.close.assert_called_once_with()

    def test_track_iteration_failure_closes_reader_and_spool(self) -> None:
        run = SimpleNamespace(status="completed", spatial_context=_CONTEXT)
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.intersections.return_value = ()
        reader.iter_tracks.side_effect = ValueError("broken relation")
        spool = MagicMock()
        with (
            patch("track.v2.clips.extract_clips.fetch_detect_row", return_value=run),
            patch("track.v2.clips.extract_clips.find_detect_artifact", return_value=SimpleNamespace(
                descriptor="descriptor", state="present",
            )),
            patch("track.v2.clips.extract_clips.DetectDatasetReader", return_value=reader),
            patch("track.v2.clips.extract_clips.ClipRelation", return_value=spool),
        ):
            with self.assertRaisesRegex(ValueError, "broken relation"):
                extract_clips_for_detect(
                    MagicMock(), "detect-1", publication_root="/parquet",
                    data_root="/tmp", runtime=runtime_config(),
                )
        reader.__exit__.assert_called_once()
        spool.close.assert_called_once_with()

    def test_incomplete_detect_is_rejected_before_child_queries(self) -> None:
        run = SimpleNamespace(status="running", refine_id="refine-1")
        with (
            patch("track.v2.clips.extract_clips.fetch_detect_row", return_value=run),
            patch("track.v2.clips.extract_clips.find_detect_artifact") as fetch,
        ):
            with self.assertRaisesRegex(ValueError, "is not completed"):
                extract_clips_for_detect(
                    object(), "detect-1", publication_root="/parquet",
                    data_root="/tmp", runtime=runtime_config(),
                )
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
