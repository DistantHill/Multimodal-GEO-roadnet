"""Cleaner 1 tests — PRD §10.2 (10 items)."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from track.v2.cleaner1 import (
    CleanerConfig,
    CleanerResult,
    clean,
    _split_by_gaps,
    _exclude_stationary,
    _inject_orthogonal,
    _ray_intersection,
)
from track.v2.exceptions import ConfigError, FieldSelectionRequired
from track.v2.models import (
    CleanReport,
    PointRef,
    TrajectoryPoint,
    TrackModel,
)
from track.v2.parser0 import CsvSource, get_database


# ── Helpers ──────────────────────────────────────────────────────────


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
    """C1-01: Cleaner separates active and excluded points."""

    def test_cleaner_separates_active_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td, "test.csv",
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116.0,39.0,90,40,1700000000000\n"
                "v1,116.001,39.001,90,40,1700000000030\n",
            )
            source = CsvSource(csv_path)
            handle = get_database(source)
            result = clean(handle)
            self.assertIsInstance(result, CleanerResult)
            self.assertIsNotNone(result.active)
            self.assertIsNotNone(result.excluded)
            self.assertIsNotNone(result.report)
            handle.close()


class OrthogonalHeadingInjectionTest(unittest.TestCase):
    """C1-02: Orthogonal heading (80-100°) injects derived point."""

    def test_orthogonal_heading_injection(self):
        # A at (116.01, 39.0) heading east (90°) → reverse west
        # B at (116.0, 39.01) heading north (0°) → reverse south
        # Both reverse rays should intersect
        a = _make_point("v1", "1", 1000.0, 116.01, 39.0, 90.0, 10.0)
        b = _make_point("v1", "2", 1030.0, 116.0, 39.01, 0.0, 10.0)
        cfg = CleanerConfig(max_extension_m=5000.0)
        result, injected = _inject_orthogonal([a, b], cfg, 116.0, 39.0)
        # a + derived + b
        self.assertGreaterEqual(len(result), 3)
        self.assertEqual(injected, 1)
        derived = [p for p in result if p.point_ref.generated_reason == "orthogonal_heading"]
        self.assertEqual(len(derived), 1)
        self.assertFalse(derived[0].point_ref.stable)
        self.assertEqual(derived[0].point_ref.from_ref, "1")
        self.assertEqual(derived[0].point_ref.to_ref, "2")

    def test_non_orthogonal_no_injection(self):
        """C1-03: Heading delta outside [80,100] → no injection."""
        a = _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 10.0)
        b = _make_point("v1", "2", 1030.0, 116.002, 39.000, 120.0, 10.0)  # 30° delta
        cfg = CleanerConfig()
        result, injected = _inject_orthogonal([a, b], cfg, 116.0, 39.0)
        self.assertEqual(injected, 0)
        self.assertEqual(len(result), 2)


class MaxExtensionTest(unittest.TestCase):
    """C1-04: Intersection at distance > max_extension_m → no injection."""

    def test_too_far_no_injection(self):
        # Two far-apart points with orthogonal headings
        a = _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 10.0)
        b = _make_point("v1", "2", 1030.0, 120.0, 42.000, 0.0, 10.0)  # ~400km away
        cfg = CleanerConfig(max_extension_m=80.0)
        result, injected = _inject_orthogonal([a, b], cfg, 116.0, 39.0)
        self.assertEqual(injected, 0)


class DerivedPointRefTest(unittest.TestCase):
    """C1-05: Derived PointRef has from_ref, to_ref, stable=False."""

    def test_derived_point_ref_traceability(self):
        a = _make_point("v1", "10", 1000.0, 116.01, 39.0, 90.0, 10.0)
        b = _make_point("v1", "20", 1030.0, 116.0, 39.01, 0.0, 10.0)
        cfg = CleanerConfig(max_extension_m=5000.0)
        result, _ = _inject_orthogonal([a, b], cfg, 116.0, 39.0)
        derived = [p for p in result if p.point_ref.generated_reason == "orthogonal_heading"]
        self.assertTrue(derived)
        self.assertEqual(derived[0].point_ref.from_ref, "10")
        self.assertEqual(derived[0].point_ref.to_ref, "20")
        self.assertFalse(derived[0].point_ref.stable)


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
        cfg = CleanerConfig(min_speed_mps=0.3, max_stationary_duration_s=300.0)
        active, excluded = _exclude_stationary(points, cfg)
        # Duration of stationary: 1400 - 1000 = 400s > 300 → exclude interior
        self.assertGreaterEqual(len(excluded), 1)

    def test_short_stationary_not_excluded(self):
        points = [
            _make_point("v1", "1", 1000.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "2", 1030.0, 116.0, 39.0, 90.0, 0.1),
            _make_point("v1", "3", 1060.0, 116.0, 39.0, 90.0, 10.0),
        ]
        cfg = CleanerConfig(min_speed_mps=0.3, max_stationary_duration_s=300.0)
        active, excluded = _exclude_stationary(points, cfg)
        # Duration: 1060 - 1000 = 60s < 300 → not excluded
        self.assertEqual(len(excluded), 0)


class CleanReportSerializationTest(unittest.TestCase):
    """C1-08: CleanReport serializable via as_dict()."""

    def test_clean_report_as_dict(self):
        report = CleanReport(
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


class TrackModelFormatPrintCleanerTest(unittest.TestCase):
    """C1-09: TrackModel.format_print() works for cleaner models."""

    def test_clean_config_format_print(self):
        import io
        cfg = CleanerConfig(max_gap_s=120.0, min_speed_mps=0.3)
        buf = io.StringIO()
        cfg.format_print(title="Cleaner Config", file=buf)
        self.assertIn("Cleaner Config", buf.getvalue())


class NoSourceModificationTest(unittest.TestCase):
    """C1-10: Cleaner does not modify source data."""

    def test_cleaner_no_modify_source(self):
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
            result = clean(handle)
            # Consume active points
            list(result.active.iter_points())
            # Source file unchanged
            after = csv_path.read_text(encoding="utf-8")
            self.assertEqual(original, after)
            handle.close()


# ── Geometry unit tests ──────────────────────────────────────────────


class RayIntersectionTest(unittest.TestCase):
    """Test _ray_intersection geometry — covers orthogonal, parallel, origin, and diverging."""

    def test_orthogonal_intersection(self):
        # A at (10,5) heading east (90°) → reverse ray west: (10-t, 5) t>=0
        # B at (5,10) heading north (0°) → reverse ray south: (5, 10-s) s>=0
        # Intersection at (5, 5)
        hit = _ray_intersection((10, 5), 90.0, (5, 10), 0.0)
        self.assertIsNotNone(hit)
        x, y = hit
        self.assertAlmostEqual(x, 5.0, delta=0.05)
        self.assertAlmostEqual(y, 5.0, delta=0.05)

    def test_parallel_no_hit(self):
        # Both heading north → reverse south — parallel
        hit = _ray_intersection((0, 0), 0.0, (10, 0), 0.0)
        self.assertIsNone(hit)

    def test_same_origin_orthogonal(self):
        # Same starting point, heading 90° vs 0° → intersection at origin (t=0, s=0)
        hit = _ray_intersection((0, 0), 90.0, (0, 0), 0.0)
        self.assertIsNotNone(hit)
        x, y = hit
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 0.0)

    def test_near_parallel_but_intersecting(self):
        # Slight angle divergence — should still find intersection
        hit = _ray_intersection((10, 5), 90.0, (5, 10), 1.0)
        self.assertIsNotNone(hit)


if __name__ == "__main__":
    unittest.main()
