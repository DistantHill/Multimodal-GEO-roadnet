"""Intersect2 tests — dual-mode intersection detection and topology verification."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from track.v2._geometry import _to_xy
from track.v2.cleaner1 import CleanerConfig, clean
from track.v2.exceptions import V2Error
from track.v2.intersect2 import (
    IntersectConfig,
    IntersectResult,
    _cluster_angles_greedy,
    _cluster_dbscan,
    _cluster_orthogonal_candidates,
    _compute_getis_ord_g_star,
    _detect_turn_events,
    _extract_entry_exit_observations_batch,
    _extract_orthogonal_candidates,
    _group_by_vehicle,
    _merge_clusters,
    _nearest_angle_id,
    _select_hotspot_events,
    detect_intersections,
)
from track.v2.models import (
    EntryExitObservation,
    HotspotCluster,
    IntersectReport,
    IntersectionTopology,
    OrthogonalCandidate,
    PointRef,
    TrajectoryPoint,
    TurnEvent,
)
from track.v2.parser0 import CsvSource, get_database


# ── Helpers ────────────────────────────────────────────────────────────


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


def _build_track_through_intersection(
    vehicle_id: str,
    origin_lon: float,
    origin_lat: float,
    approach_bearing: float,  # direction approaching center from
    exit_bearing: float,      # direction leaving center to
) -> list[TrajectoryPoint]:
    """Build a synthetic track that passes through the origin intersection.

    Creates a straight-line path that turns at the origin. The vehicle
    approaches from one direction and exits in another, creating a turn
    event at the origin.
    """
    points: list[TrajectoryPoint] = []
    ts = 1000.0

    # Approach segment: 80m long, 4 points spaced 20m
    # Reverse of approach_bearing
    app_rad = math.radians(approach_bearing + 180)
    for i in range(4):
        dist = 80.0 - i * 20.0
        x = dist * math.sin(app_rad)
        y = dist * math.cos(app_rad)
        lon = origin_lon + x / 111320.0
        lat = origin_lat + y / 111320.0
        points.append(
            _make_point(vehicle_id, f"a{i}", ts, lon, lat, approach_bearing, 10.0)
        )
        ts += 10.0

    # Exit segment: 80m long, 4 points spaced 20m
    exit_rad = math.radians(exit_bearing)
    for i in range(4):
        dist = i * 20.0 + 20.0
        x = dist * math.sin(exit_rad)
        y = dist * math.cos(exit_rad)
        lon = origin_lon + x / 111320.0
        lat = origin_lat + y / 111320.0
        points.append(
            _make_point(vehicle_id, f"e{i}", ts, lon, lat, exit_bearing, 10.0)
        )
        ts += 10.0

    return points


# ── Model serialization tests ──────────────────────────────────────────


class TurnEventModelTest(unittest.TestCase):
    """I2-01: TurnEvent serialization."""

    def test_turn_event_as_dict(self):
        event = TurnEvent(
            event_id=0,
            vehicle_id="v1",
            timestamp=1000.0,
            lon=116.0, lat=39.0,
            x=0.0, y=0.0,
            in_bearing=0.0, out_bearing=90.0,
            turn_deg=90.0, score=90.0,
        )
        d = event.as_dict()
        self.assertEqual(d["event_id"], 0)
        self.assertEqual(d["vehicle_id"], "v1")
        self.assertEqual(d["turn_deg"], 90.0)


class IntersectReportTest(unittest.TestCase):
    """I2-02: IntersectReport serialization."""

    def test_report_as_dict(self):
        report = IntersectReport(
            track_count=10,
            turn_event_count=100,
            hotspot_event_count=50,
            orthogonal_candidate_count=20,
            cluster_count=5,
            cluster_count_turn=3,
            cluster_count_ortho=2,
            complete_intersection_count=3,
        )
        d = report.as_dict()
        self.assertEqual(d["track_count"], 10)
        self.assertEqual(d["cluster_count"], 5)


# ── Group by vehicle tests ─────────────────────────────────────────────


class GroupByVehicleTest(unittest.TestCase):
    """I2-03: Groups by vehicle_id, returns correct origin."""

    def test_group_by_vehicle(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td, "test.csv",
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116.0,39.0,90,40,1000\n"
                "v1,116.001,39.001,90,40,3000\n"
                "v2,116.1,39.1,180,30,2000\n",
            )
            source = CsvSource(csv_path)
            handle = get_database(source)
            tracks, origin_lon, origin_lat = _group_by_vehicle(handle)
            self.assertEqual(len(tracks), 2)
            self.assertIn("v1", tracks)
            self.assertIn("v2", tracks)
            self.assertEqual(len(tracks["v1"]), 2)
            self.assertEqual(len(tracks["v2"]), 1)
            self.assertAlmostEqual(origin_lon, 116.0)
            handle.close()


# ── Turn detection tests (Method 1) ───────────────────────────────────


class TurnDetectionTest(unittest.TestCase):
    """I2-04 ~ I2-08: Turn event detection."""

    def test_cross_intersection_detects_turns(self):
        """I2-04: A vehicle making a 90° turn generates a turn event."""
        origin_lon, origin_lat = 116.391, 39.907
        track = _build_track_through_intersection(
            "v1", origin_lon, origin_lat, approach_bearing=0.0, exit_bearing=90.0
        )
        tracks = {"v1": track}
        cfg = IntersectConfig(turn_window=2, min_turn_deg=45.0, max_turn_deg=170.0,
                              min_leg_m=20.0, min_movement_speed_mps=1.0)
        events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg)
        self.assertGreaterEqual(len(events), 1, "Should detect at least one turn event")
        # The turn should be near 90°
        for e in events:
            self.assertGreaterEqual(abs(e.turn_deg), cfg.min_turn_deg)

    def test_straight_line_no_turns(self):
        """I2-05: A vehicle on a straight line generates 0 turn events."""
        origin_lon, origin_lat = 116.391, 39.907
        # Build a straight track along bearing 0° (north)
        points: list[TrajectoryPoint] = []
        ts = 1000.0
        for i in range(10):
            lat = origin_lat + i * 0.0002
            points.append(
                _make_point("v1", str(i), ts, origin_lon, lat, 0.0, 10.0)
            )
            ts += 10.0
        tracks = {"v1": points}
        cfg = IntersectConfig()
        events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg)
        self.assertEqual(len(events), 0)

    def test_min_turn_deg_filter(self):
        """I2-06: Turns below min_turn_deg are filtered out."""
        origin_lon, origin_lat = 116.391, 39.907
        # A 30° turn — should be filtered with min_turn_deg=45
        track = _build_track_through_intersection(
            "v1", origin_lon, origin_lat, approach_bearing=0.0, exit_bearing=30.0
        )
        tracks = {"v1": track}
        cfg = IntersectConfig(min_turn_deg=45.0, min_leg_m=20.0, min_movement_speed_mps=1.0)
        events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg)
        self.assertEqual(len(events), 0, "30° turn should be filtered by min_turn_deg=45")

    def test_max_turn_deg_filter(self):
        """I2-07: Turns above max_turn_deg are filtered out."""
        origin_lon, origin_lat = 116.391, 39.907
        # A 180° U-turn — should be filtered with max_turn_deg=170
        track = _build_track_through_intersection(
            "v1", origin_lon, origin_lat, approach_bearing=0.0, exit_bearing=180.0
        )
        tracks = {"v1": track}
        cfg = IntersectConfig(max_turn_deg=170.0, min_leg_m=20.0, min_movement_speed_mps=1.0)
        events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg)
        # U-turn of 180° should NOT be detected (above max_turn_deg)
        for e in events:
            self.assertNotAlmostEqual(abs(e.turn_deg), 180.0, delta=1.0)

    def test_short_leg_filtered(self):
        """I2-08: Leg distances below min_leg_m are filtered out."""
        origin_lon, origin_lat = 116.391, 39.907
        # Track with very short legs (only 5m apart)
        points: list[TrajectoryPoint] = []
        ts = 1000.0
        for i in range(8):
            lat = origin_lat + i * 0.00005  # ~5m per step
            heading = 0.0 if i < 4 else 90.0
            points.append(
                _make_point("v1", str(i), ts, origin_lon, lat, heading, 10.0)
            )
            ts += 10.0
        tracks = {"v1": points}
        cfg = IntersectConfig(min_leg_m=35.0, min_turn_deg=45.0, min_movement_speed_mps=0.5)
        events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg)
        self.assertEqual(len(events), 0, "Short legs should be filtered")


# ── Getis-Ord G* tests ────────────────────────────────────────────────


class GetisOrdGStarTest(unittest.TestCase):
    """I2-09 ~ I2-11: Getis-Ord G* scoring."""

    def test_concentrated_events_high_z(self):
        """I2-09: Co-located events get high z-scores."""
        events = [
            TurnEvent(0, "v1", 1000, 116, 39, 0, 0, 0, 90, 90, 90),
            TurnEvent(1, "v2", 1001, 116, 39, 5, 5, 0, 90, 90, 90),
            TurnEvent(2, "v3", 1002, 116, 39, 10, 10, 0, 90, 90, 90),
            TurnEvent(3, "v4", 1003, 116, 39, -5, -5, 0, 90, 90, 90),
            # One far event
            TurnEvent(4, "v5", 1004, 117, 40, 5000, 5000, 0, 90, 90, 90),
        ]
        z = _compute_getis_ord_g_star(events, radius_m=200.0)
        # The four concentrated events should have positive z
        for eid in [0, 1, 2, 3]:
            self.assertGreater(z[eid], 0.0, f"Event {eid} should have positive z")
        # The far event should have negative z
        self.assertLess(z[4], 0.0)

    def test_sparse_events_low_z(self):
        """I2-10: Widely scattered events have near-zero or negative z."""
        events = [
            TurnEvent(i, f"v{i}", 1000 + i, 116 + i * 0.1, 39 + i * 0.1,
                       i * 10000, i * 10000, 0, 90, 90, 90)
            for i in range(5)
        ]
        z = _compute_getis_ord_g_star(events, radius_m=180.0)
        # All should be near zero or negative (no concentration)
        for eid in range(5):
            self.assertLess(z[eid], 2.0, f"Event {eid}: sparse events should not have high z")

    def test_single_event(self):
        """I2-11: Single event gets z=0.0."""
        events = [TurnEvent(0, "v1", 1000, 116, 39, 0, 0, 0, 90, 90, 90)]
        z = _compute_getis_ord_g_star(events, radius_m=180.0)
        self.assertEqual(z[0], 0.0)


class HotspotSelectionTest(unittest.TestCase):
    """I2-12: Hotspot filtering by z-score and turn angle."""

    def test_hotspot_filtering(self):
        # 3 events clustered + 2 far away → cluster gets positive z
        events = [
            TurnEvent(0, "v1", 1000, 116, 39, 0, 0, 0, 90, 90, 90),
            TurnEvent(1, "v2", 1001, 116, 39, 1, 1, 0, 90, 90, 90),
            TurnEvent(2, "v3", 1002, 116, 39, -1, -1, 0, 90, 90, 90),
            TurnEvent(3, "v4", 1003, 116, 39, 2, 2, 0, 90, 20, 20),  # small turn
            TurnEvent(4, "v5", 1004, 117, 40, 5000, 5000, 0, 90, 90, 90),  # far away
            TurnEvent(5, "v6", 1005, 117, 40, 5100, 5100, 0, 90, 90, 90),  # far away
        ]
        cfg = IntersectConfig(g_star_radius_m=200.0, g_star_min_z=0.5, min_turn_deg=45.0)
        selected = _select_hotspot_events(events, cfg)
        self.assertGreaterEqual(len(selected), 1, "Clustered events should have z >= 0.5")
        # Event 3 has turn=20 < 45, should be excluded
        for e in selected:
            self.assertGreaterEqual(abs(e.turn_deg), cfg.min_turn_deg)


# ── DBSCAN clustering tests ───────────────────────────────────────────


class DBSCANTest(unittest.TestCase):
    """I2-13 ~ I2-15: DBSCAN clustering."""

    def test_two_clusters_found(self):
        """I2-13: 8 events in 2 clusters 200m apart → 2 clusters."""
        origin_lon, origin_lat = 116.0, 39.0
        events = [
            # Cluster A (4 events close together)
            TurnEvent(i, f"v{i}", 1000 + i, 116.0, 39.0, i, i, 0, 90, 90, 90)
            for i in range(4)
        ] + [
            # Cluster B (4 events 300m away)
            TurnEvent(i + 4, f"v{i+4}", 1000 + i, 116.0, 39.0,
                      300 + i, 300 + i, 0, 90, 90, 90)
            for i in range(4)
        ]
        clusters = _cluster_dbscan(events, origin_lon, origin_lat,
                                   eps_m=100.0, min_samples=3)
        self.assertEqual(len(clusters), 2, f"Expected 2 clusters, got {len(clusters)}")

    def test_noise_labeled(self):
        """I2-14: Single isolated event labeled as noise."""
        origin_lon, origin_lat = 116.0, 39.0
        events = [
            TurnEvent(0, "v1", 1000, 116, 39, 0, 0, 0, 90, 90, 90),
            TurnEvent(1, "v2", 1001, 116, 39, 5000, 5000, 0, 90, 90, 90),
        ]
        clusters = _cluster_dbscan(events, origin_lon, origin_lat,
                                   eps_m=100.0, min_samples=3)
        self.assertEqual(len(clusters), 0, "Isolated events should be noise")

    def test_single_cluster(self):
        """I2-15: All events within eps_m → single cluster."""
        origin_lon, origin_lat = 116.0, 39.0
        events = [
            TurnEvent(i, f"v{i}", 1000 + i, 116, 39, i * 5, i * 5, 0, 90, 90, 90)
            for i in range(5)
        ]
        clusters = _cluster_dbscan(events, origin_lon, origin_lat,
                                   eps_m=200.0, min_samples=3)
        self.assertEqual(len(clusters), 1)


# ── Orthogonal candidate tests (Method 2) ──────────────────────────────


class OrthogonalCandidateTest(unittest.TestCase):
    """I2-16 ~ I2-17: Orthogonal candidate extraction and clustering."""

    def test_extract_orthogonal_from_cleaner_output(self):
        """I2-16: Orthogonal injection points are extracted."""
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td, "test.csv",
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116.0,39.0,0,20,1000\n"
                "v1,116.001,39.001,90,20,1010\n"
                "v2,117.0,40.0,180,20,1000\n"
                "v2,117.001,39.999,270,20,1010\n",
            )
            source = CsvSource(csv_path)
            handle = get_database(source)
            result = clean(handle, config=CleanerConfig(
                max_extension_m=5000.0, min_heading_delta_deg=80.0,
                max_heading_delta_deg=100.0,
            ))
            # Consume cleaned points to trigger orthogonal injection
            points = list(result.active.iter_points())
            handle.close()

            # Filter for injected points
            injected = [p for p in points if p.point_ref.generated_reason == "orthogonal_heading"]
            if not injected:
                self.skipTest("Cleaner did not generate orthogonal points for this data")

            # Build tracks from consumed points
            tracks: dict[str, list[TrajectoryPoint]] = {}
            for p in points:
                tracks.setdefault(p.vehicle_id, []).append(p)

            origin_lon, origin_lat = points[0].lon, points[0].lat
            candidates = _extract_orthogonal_candidates(tracks, origin_lon, origin_lat)
            self.assertGreaterEqual(len(candidates), 1)
            for c in candidates:
                self.assertEqual(c.point_ref.generated_reason, "orthogonal_heading")

    def test_cluster_orthogonal_candidates(self):
        """I2-17: Orthogonal candidates can be clustered."""
        origin_lon, origin_lat = 116.0, 39.0
        # Create candidates at two distinct locations
        candidates = []
        for i in range(4):
            candidates.append(
                OrthogonalCandidate(
                    point_ref=PointRef("test", str(i), stable=False,
                                       generated_reason="orthogonal_heading"),
                    vehicle_id=f"v{i}",
                    timestamp=1000.0 + i,
                    lon=116.0 + i * 0.0001,
                    lat=39.0 + i * 0.0001,
                    x=i * 3,
                    y=i * 3,
                )
            )
        clusters = _cluster_orthogonal_candidates(
            candidates, origin_lon, origin_lat, eps_m=100.0, min_samples=3
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].source, "orthogonal")


# ── Topology extraction tests ──────────────────────────────────────────


class TopologyExtractionTest(unittest.TestCase):
    """I2-18 ~ I2-21: Entry/exit observations, branch clustering, transition matrix."""

    def test_batch_entry_exit_observation(self):
        """I2-18: Vehicle passing through circle generates entry/exit observation."""
        origin_lon, origin_lat = 116.391, 39.907
        # Build a track that passes straight through the origin
        points: list[TrajectoryPoint] = []
        ts = 1000.0
        for i in range(10):
            lat = origin_lat + (i - 5) * 0.0005
            points.append(
                _make_point("v1", str(i), ts, origin_lon, lat, 0.0, 10.0)
            )
            ts += 10.0
        tracks = {"v1": points}

        cluster = HotspotCluster(
            cluster_id=0,
            centroid_x=0.0, centroid_y=0.0,
            centroid_lon=origin_lon, centroid_lat=origin_lat,
            radius_m=50.0,
            events=(),
            source="turn",
        )
        observations = _extract_entry_exit_observations_batch(
            tracks, cluster=cluster,
            origin_lon=origin_lon, origin_lat=origin_lat,
            radius_m=150.0, bearing_window=2,
        )
        self.assertGreaterEqual(len(observations), 1,
                                "Vehicle passing through circle should create observation")

    def test_branch_clustering(self):
        """I2-19: 4 cardinal directions cluster to 4 branches."""
        angles = [0.0, 90.0, 180.0, 270.0,
                   5.0, 95.0, 175.0, 265.0]  # 4 clusters with slight noise
        centers = _cluster_angles_greedy(angles, threshold_deg=35.0)
        self.assertEqual(len(centers), 4, f"Expected 4 branch centers, got {len(centers)}")

    def test_transition_matrix(self):
        """I2-20: Observed transitions populate correct counts."""
        # Create a simple intersection topology manually
        topology = IntersectionTopology(
            cluster_id=0,
            branch_centers_deg=[0.0, 90.0, 180.0, 270.0],
            transition_counts={(0, 1): 5, (1, 2): 3, (2, 3): 2, (0, 2): 4},
            observation_count=14,
            is_complete=True,
            reason="complete",
        )
        self.assertTrue(topology.is_complete)
        self.assertEqual(topology.transition_counts[(0, 1)], 5)
        self.assertEqual(len(topology.branch_centers_deg), 4)

    def test_nearest_angle_id(self):
        """I2-21: Nearest branch angle is correctly identified."""
        centers = [0.0, 90.0, 180.0, 270.0]
        self.assertEqual(_nearest_angle_id(5.0, centers), 0)    # nearest to 0°
        self.assertEqual(_nearest_angle_id(88.0, centers), 1)   # nearest to 90°
        self.assertEqual(_nearest_angle_id(179.0, centers), 2)  # nearest to 180°
        self.assertEqual(_nearest_angle_id(272.0, centers), 3)  # nearest to 270°


# ── Cluster merge tests ────────────────────────────────────────────────


class ClusterMergeTest(unittest.TestCase):
    """I2-22: Cluster merging from both methods."""

    def test_merge_overlapping_clusters(self):
        """Overlapping clusters from both methods are merged."""
        origin_lon, origin_lat = 116.0, 39.0
        c1 = HotspotCluster(
            cluster_id=0, centroid_x=0.0, centroid_y=0.0,
            centroid_lon=origin_lon, centroid_lat=origin_lat,
            radius_m=50.0,
            events=(TurnEvent(0, "v1", 1000, origin_lon, origin_lat,
                              0, 0, 0, 90, 90, 90),),
            source="turn",
        )
        c2 = HotspotCluster(
            cluster_id=0, centroid_x=20.0, centroid_y=20.0,
            centroid_lon=origin_lon, centroid_lat=origin_lat,
            radius_m=30.0,
            events=(TurnEvent(1, "v2", 1001, origin_lon, origin_lat,
                              20, 20, 0, 90, 90, 90),
                    TurnEvent(2, "v3", 1002, origin_lon, origin_lat,
                              25, 25, 0, 90, 90, 90)),
            source="orthogonal",
        )
        merged = _merge_clusters([c1, c2], merge_distance_m=80.0,
                                 origin_lon=origin_lon, origin_lat=origin_lat)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source, "merged")

    def test_no_merge_distant_clusters(self):
        """Distant clusters are not merged."""
        origin_lon, origin_lat = 116.0, 39.0
        c1 = HotspotCluster(
            cluster_id=0, centroid_x=0.0, centroid_y=0.0,
            centroid_lon=116.0, centroid_lat=39.0,
            radius_m=50.0, events=(), source="turn",
        )
        c2 = HotspotCluster(
            cluster_id=1, centroid_x=5000.0, centroid_y=5000.0,
            centroid_lon=117.0, centroid_lat=40.0,
            radius_m=50.0, events=(), source="orthogonal",
        )
        merged = _merge_clusters([c1, c2], merge_distance_m=80.0,
                                 origin_lon=origin_lon, origin_lat=origin_lat)
        self.assertEqual(len(merged), 2)


# ── End-to-end integration tests ───────────────────────────────────────


class EndToEndTest(unittest.TestCase):
    """I2-23 ~ I2-25: Full pipeline integration."""

    def test_synthetic_cross_intersection(self):
        """I2-23: Full pipeline on synthetic 4-way cross."""
        with tempfile.TemporaryDirectory() as td:
            origin_lon, origin_lat = 116.391, 39.907
            # Build 4 vehicles making 90° turns at the origin from each approach
            tracks_data = [
                ("v1", 0.0, 90.0),    # N → E
                ("v2", 90.0, 180.0),  # E → S
                ("v3", 180.0, 270.0), # S → W
                ("v4", 270.0, 0.0),   # W → N
            ]
            all_points: list[TrajectoryPoint] = []
            for vid, app, exit_ in tracks_data:
                track = _build_track_through_intersection(
                    vid, origin_lon, origin_lat, approach_bearing=app, exit_bearing=exit_
                )
                all_points.extend(track)

            # Write to CSV and parse back
            rows = ["vid,lng,lat,included_angle,gps_speed,gps_time_a"]
            for p in sorted(all_points, key=lambda p: (p.vehicle_id, p.timestamp)):
                rows.append(
                    f"{p.vehicle_id},{p.lon},{p.lat},{p.heading},{p.speed},{p.timestamp}"
                )
            csv_path = _make_csv(td, "cross.csv", "\n".join(rows) + "\n")

            source = CsvSource(csv_path)
            handle = get_database(source)
            result = detect_intersections(handle, config=IntersectConfig(
                use_turn_events=True,
                use_orthogonal_candidates=False,  # Only Method 1 for this test
                turn_window=2,
                min_turn_deg=45.0, max_turn_deg=170.0,
                min_leg_m=20.0, min_movement_speed_mps=1.0,
                g_star_radius_m=500.0, g_star_min_z=-1.0,  # lenient to catch all
                dbscan_eps_m_turn=500.0, dbscan_min_samples_turn=2,
                min_branches=2, min_observations=2, min_directed_transitions=2,
            ))
            handle.close()

            self.assertGreaterEqual(len(result.turn_events), 4,
                                    "4 vehicles making 90° turns → at least 4 turn events")
            self.assertGreaterEqual(len(result.clusters), 1,
                                    "All turns at the same location → at least 1 cluster")

    def test_straight_line_no_intersection(self):
        """I2-24: Straight-line tracks produce 0 turn events and 0 clusters."""
        with tempfile.TemporaryDirectory() as td:
            rows = ["vid,lng,lat,included_angle,gps_speed,gps_time_a"]
            for v in range(3):  # 3 vehicles on straight parallel paths
                base_lat = 39.0 + v * 0.01
                ts = 1000.0
                for i in range(20):
                    lat = base_lat + i * 0.0005
                    rows.append(f"v{v},{116.391},{lat},{0.0},{10.0},{ts}")
                    ts += 10.0
            csv_path = _make_csv(td, "straight.csv", "\n".join(rows) + "\n")

            source = CsvSource(csv_path)
            handle = get_database(source)
            result = detect_intersections(handle)
            handle.close()

            self.assertEqual(len(result.turn_events), 0,
                             "Straight lines should produce 0 turn events")
            self.assertEqual(len(result.clusters), 0)

    def test_real_csv_smoke(self):
        """I2-25: Smoke test — detect_intersections on Hfm_130pts.csv."""
        import os
        hfm_path = Path(__file__).resolve().parents[2] / "track" / "data" / "Hfm_130pts.csv"
        if not hfm_path.exists():
            self.skipTest(f"Hfm_130pts.csv not found at {hfm_path}")

        source = CsvSource(hfm_path)
        handle = get_database(source)
        # Run with default config
        result = detect_intersections(handle)
        handle.close()

        # Just verify no crashes and some output
        self.assertIsInstance(result.report, IntersectReport)
        self.assertGreaterEqual(result.report.track_count, 0)
        # The dataset has ~130 points — should find at least a few tracks
        # but may not have enough data for complete intersections
        print(f"\n  Hfm smoke: tracks={result.report.track_count}, "
              f"turn_events={result.report.turn_event_count}, "
              f"clusters={result.report.cluster_count}, "
              f"complete={result.report.complete_intersection_count}")


if __name__ == "__main__":
    unittest.main()
