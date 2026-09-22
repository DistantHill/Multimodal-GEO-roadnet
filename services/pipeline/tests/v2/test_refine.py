"""Refine tests."""

from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from track.v2.refine import refine_vehicle as _refine_vehicle
from track.v2.refine import (
    RefineConfig,
    RefineResult,
    refine as _refine,
    _split_by_gaps,
    _exclude_stationary,
    ray_intersection,
    heading_vector,
)
from track.v2.models import (
    RefineReport,
    PointRef,
    SpatialContext,
    TrajectoryPoint,
)
from track.v2.parser import CsvSource, get_database as _get_database


# ── Helpers ──────────────────────────────────────────────────────────

_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=32650,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": 0.01},
)


def get_database(source, **kwargs):
    from .config_factory import pipeline_config
    config = pipeline_config()
    kwargs.setdefault("field_config", config.parser.fields)
    kwargs.setdefault("runtime", config.runtime)
    return _get_database(source, **kwargs)


def refine(*args, **kwargs):
    kwargs.setdefault("config", refine_config())
    return _refine(*args, **kwargs)


def refine_vehicle(*args, **kwargs):
    kwargs.setdefault("config", refine_config())
    return _refine_vehicle(*args, **kwargs)


def _make_csv(tmpdir: str, filename: str, content: str, encoding: str = "utf-8") -> Path:
    p = Path(tmpdir) / filename
    p.write_text(content, encoding=encoding)
    return p


def _make_point(
    vehicle_id: str,
    ref_key: str,
    ts: float,
    lon: float,
    lat: float,
    heading: float,
    speed: float,
    source_id: str = "test",
) -> TrajectoryPoint:
    return TrajectoryPoint(
        point_ref=PointRef(
            source_id=source_id,
            source_row_key=ref_key,
            stable=True,
        ),
        vehicle_id=vehicle_id,
        timestamp=ts,
        lon=lon,
        lat=lat,
        heading=heading,
        speed=speed,
    )


# ── Test cases ───────────────────────────────────────────────────────


class ActiveExcludedSeparationTest(unittest.TestCase):
    """C1-01: Refine separates active and excluded points."""

    def test_refine_separates_active_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td, "test.csv",
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116.0,39.0,90,40,1700000000000\n"
                "v1,116.001,39.001,90,40,1700000000030\n",
            )
            source = CsvSource(csv_path)
            handle = get_database(source)
            result = refine(handle, spatial_context=_SPATIAL_CONTEXT)
            self.assertIsInstance(result, RefineResult)
            active = list(result.active.iter_points())
            self.assertEqual(len(active), 2)
            self.assertEqual(result.report.active_count, 2)
            self.assertEqual(result.report.excluded_count, 0)
            self.assertEqual(result.report.injected_count, 0)
            self.assertEqual(len(result.kept_subtracks), 1)
            self.assertIsNotNone(result.active)
            self.assertIsNotNone(result.excluded)
            self.assertIsNotNone(result.report)
            self.assertEqual(result.spatial_context, _SPATIAL_CONTEXT)
            self.assertEqual(result.active.spatial_context, _SPATIAL_CONTEXT)
            self.assertTrue(
                all(
                    point.x is not None and point.y is not None
                    for point in active
                )
            )
            handle.close()

    def test_refine_vehicle_reprojects_unscoped_kept_xy_and_carries_context(self):
        points = [
            replace(
                _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 10.0),
                x=1.0,
                y=2.0,
            ),
            replace(
                _make_point("v1", "2", 1030.0, 116.001, 39.0, 90.0, 10.0),
                x=3.0,
                y=4.0,
            ),
        ]

        result = refine_vehicle(points, spatial_context=_SPATIAL_CONTEXT)

        self.assertEqual(result.spatial_context, _SPATIAL_CONTEXT)
        self.assertNotEqual((result.kept[0].x, result.kept[0].y), (1.0, 2.0))

class GapSplittingTest(unittest.TestCase):
    """C1-06: Temporal gap > max_gap_s creates independent sub-tracks."""

    def test_gap_splitting(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 10.0),
            _make_point("v1", "2", 1030.0, 116.0, 39.1, 90.0, 10.0),
            # gap: 200s → split here
            _make_point("v1", "3", 1230.0, 116.0, 39.2, 90.0, 10.0),
            _make_point("v1", "4", 1260.0, 116.0, 39.3, 90.0, 10.0),
        ]
        sub_tracks = _split_by_gaps(points, max_gap_s=120.0)
        self.assertEqual(len(sub_tracks), 2)
        self.assertEqual(len(sub_tracks[0]), 2)
        self.assertEqual(len(sub_tracks[1]), 2)

    def test_no_gap_no_split(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 10.0),
            _make_point("v1", "2", 1030.0, 116.0, 39.1, 90.0, 10.0),
        ]
        sub_tracks = _split_by_gaps(points, max_gap_s=120.0)
        self.assertEqual(len(sub_tracks), 1)


class RefineVehicleAggregationTest(unittest.TestCase):
    """Refine retains every split subtrack and each exclusion exactly once."""

    def test_accumulates_kept_points_across_gap_subtracks(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 10.0),
            _make_point("v1", "2", 1030.0, 116.0, 39.1, 90.0, 10.0),
            _make_point("v1", "3", 1230.0, 116.0, 39.2, 90.0, 10.0),
            _make_point("v1", "4", 1260.0, 116.0, 39.3, 90.0, 10.0),
        ]

        result = refine_vehicle(
            points,
            config=refine_config(max_gap_s=120.0),
            spatial_context=_SPATIAL_CONTEXT,
        )

        self.assertEqual(
            [point.point_ref.source_row_key for point in result.kept],
            ["1", "2", "3", "4"],
        )
        self.assertEqual(len(result.subtracks), 2)
        self.assertEqual(
            [
                [point.point_ref.source_row_key for point in subtrack.points]
                for subtrack in result.subtracks
            ],
            [["1", "2"], ["3", "4"]],
        )
        self.assertEqual(
            [subtrack.vehicle_id for subtrack in result.subtracks],
            ["v1_seg01", "v1_seg02"],
        )

    def test_accumulates_stationary_exclusions_once_across_gap_subtracks(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "2", 1005.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "3", 1015.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "4", 1100.0, 116.1, 39.0, 90.0, 0.1),
            _make_point("v1", "5", 1105.0, 116.1, 39.0, 90.0, 0.1),
            _make_point("v1", "6", 1115.0, 116.1, 39.0, 90.0, 0.1),
        ]
        cfg = refine_config(
            max_gap_s=50.0,
            min_speed_mps=0.3,
            max_stationary_duration_s=10.0,
        )

        result = refine_vehicle(points, config=cfg, spatial_context=_SPATIAL_CONTEXT)

        self.assertEqual(
            [point.point_ref.source_row_key for point in result.kept],
            ["1", "3", "4", "6"],
        )
        self.assertEqual(
            [item.point_ref.source_row_key for item in result.excluded],
            ["2", "5"],
        )
        self.assertEqual(len(result.subtracks), 2)


class StationaryExclusionTest(unittest.TestCase):
    """C1-07: Long stationary segments exclude interior points, keep first/last."""

    def test_stationary_exclusion(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "2", 1100.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "3", 1200.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "4", 1400.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "5", 1430.0, 116.0, 39.1, 90.0, 10.0),  # moving now
        ]
        cfg = refine_config(min_speed_mps=0.3, max_stationary_duration_s=300.0)
        active, excluded = _exclude_stationary(points, cfg)
        # Duration of stationary: 1400 - 1000 = 400s > 300 → exclude interior
        self.assertGreaterEqual(len(excluded), 1)

    def test_short_stationary_not_excluded(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "2", 1030.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "3", 1060.0, 116.0, 39.0, 90.0, 10.0),
        ]
        cfg = refine_config(min_speed_mps=0.3, max_stationary_duration_s=300.0)
        active, excluded = _exclude_stationary(points, cfg)
        # Duration: 1060 - 1000 = 60s < 300 → not excluded
        self.assertEqual(len(excluded), 0)


class RefineReportSerializationTest(unittest.TestCase):
    """C1-08: RefineReport serializable via as_dict()."""

    def test_refine_report_as_dict(self):
        report = RefineReport(
            active_count=100,
            excluded_count=5,
            injected_count=3,
            excluded_reasons={"stationary": 5},
            sample_excluded=[],
        )
        d = report.as_dict()
        self.assertEqual(d["active_count"], 100)
        self.assertEqual(d["excluded_count"], 5)
        self.assertEqual(d["injected_count"], 3)


class TrackModelFormatPrintRefineTest(unittest.TestCase):
    """C1-09: TrackModel.format_print() works for refine models."""

    def test_refine_config_format_print(self):
        import io
        cfg = refine_config(max_gap_s=120.0, min_speed_mps=0.3)
        buf = io.StringIO()
        cfg.format_print(title="Refine Config", file=buf)
        self.assertIn("Refine Config", buf.getvalue())


class NoSourceModificationTest(unittest.TestCase):
    """C1-10: Refine does not modify source data."""

    def test_refine_no_modify_source(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td, "test.csv",
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116.0,39.0,90,40,1700000000000\n"
                "v1,116.001,39.001,90,40,1700000000030\n",
            )
            original = csv_path.read_text(encoding="utf-8")
            source = CsvSource(csv_path)
            handle = get_database(source)
            result = refine(handle, spatial_context=_SPATIAL_CONTEXT)
            # Consume active points
            list(result.active.iter_points())
            # Source file unchanged
            after = csv_path.read_text(encoding="utf-8")
            self.assertEqual(original, after)
            handle.close()


# ── Geometry unit tests ──────────────────────────────────────────────


class RayIntersectionTest(unittest.TestCase):
    """Test ray geometry using the canonical vector-based interface."""

    def test_orthogonal_intersection(self):
        # A at (10,5) heading east (90°) → reverse ray west: (10-t, 5) t>=0
        # B at (5,10) heading north (0°) → reverse ray south: (5, 10-s) s>=0
        # Intersection at (5, 5)
        hit = ray_intersection((10, 5), heading_vector(270.0), (5, 10), heading_vector(180.0))
        self.assertIsNotNone(hit)
        x, y, ta, tb = hit
        self.assertGreaterEqual(ta, 0.0)
        self.assertGreaterEqual(tb, 0.0)
        self.assertAlmostEqual(x, 5.0, delta=0.05)
        self.assertAlmostEqual(y, 5.0, delta=0.05)

    def test_parallel_no_hit(self):
        # Both heading north → reverse south — parallel
        hit = ray_intersection((0, 0), heading_vector(180.0), (10, 0), heading_vector(180.0))
        self.assertIsNone(hit)

    def test_same_origin_orthogonal(self):
        # Same starting point, heading 90° vs 0° → intersection at origin (t=0, s=0)
        hit = ray_intersection((0, 0), heading_vector(270.0), (0, 0), heading_vector(180.0))
        self.assertIsNotNone(hit)
        x, y, _ta, _tb = hit
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 0.0)

    def test_near_parallel_but_intersecting(self):
        # Slight angle divergence — should still find intersection
        hit = ray_intersection((10, 5), heading_vector(270.0), (5, 10), heading_vector(181.0))
        self.assertIsNotNone(hit)


if __name__ == "__main__":
    unittest.main()
