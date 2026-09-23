"""Detect turn/cross-track detection and topology verification tests."""

from __future__ import annotations

from .config_factory import (clip_config, detect_context, fitting_config, intersect_config,
                             intersect_run_config, network_config, pipeline_config,
                             postgres_config, projection_config, refine_config, runtime_config)

import math
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from track.v2._geometry import (
    _batch_segment_intersections,
    _iter_bounded_segment_intersections,
    _paired_segment_intersections,
    _to_lonlat,
    _to_xy,
)
from track.v2._perf import PerfTracker
from track.v2.cross_track import (
    extract_cross_track_candidates_legacy as _extract_orthogonal_cross_track_candidates_legacy,
    _prepare_segments as _prepare_cross_track_segments,
)
from track.v2.candidate_patches import (
    build_cross_track_index,
    _iter_cross_vehicle_pair_batches,
    _iter_pair_batches as _iter_cross_track_pair_batches,
)
from track.v2.detect_candidates import (
    extract_cross_track_candidates as _extract_orthogonal_cross_track_candidates,
)
from track.v2 import _cross_track_legacy as cross_track_legacy_module
from track.v2.exceptions import V2Error
from track.v2.detect import (
    IntersectConfig,
    IntersectResult,
    SpatialWindow,
    _cluster_angles_greedy,
    _cluster_dbscan,
    _cluster_pipeline,
    _cluster_orthogonal_cross_track_candidates,
    _annotate_source_ablation,
    _compute_getis_ord_g_star,
    _detect_turn_events,
    _detect_intersections_db,
    _extract_entry_exit_observations_batch,
    _extract_cross_track_db,
    _extract_cross_track_windows,
    _getis_local_counts_grid,
    _getis_local_counts_scan,
    _group_cluster_items,
    _group_by_vehicle,
    _build_dbscan_grid,
    _build_topology_point_index,
    _build_topologies_from_tracks,
    _merge_clusters,
    _nearest_angle_id,
    _project_tracks,
    _run_dbscan,
    _topology_point_windows,
    _query_active_points_in_window,
    _select_hotspot_events,
    detect_intersections,
)
from track.v2.models import (
    RefinedPointRef,
    CrossTrackCandidate,
    EntryExitObservation,
    GeneratedReason,
    HotspotCluster,
    IntersectReport,
    IntersectionTopology,
    PointRef,
    RuntimeConfig,
    SpatialContext,
    TrajectoryPoint,
    TurnEvent,
)
from track.v2.db.source_table import SourceTableContract
from track.v2.db.queries import _stream_vehicle_tracks_from_db
from track.v2.parser import CsvSource, get_database
from track.v2.projection import (
    CoordinateTransformer,
    ProjectionError,
    QueryWindowRequest,
    build_query_window,
)
from track.v2.refine_parquet import TopologyTrackSelection


# ── Helpers ────────────────────────────────────────────────────────────


_TEST_SPATIAL_CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=3857,
    projection_config={"coordinate_semantics_version": 2, "enforce_area_of_use": False, "max_scale_error": 1.0},
)
_TEST_TRANSFORMER = CoordinateTransformer(_TEST_SPATIAL_CONTEXT)


def _with_processing_xy(
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    return _project_tracks(tracks, _TEST_TRANSFORMER)


def _candidate_keys(candidates):
    return [
        (
            candidate.vehicle_id,
            candidate.from_segment_start_point_ref,
            candidate.to_segment_start_point_ref,
        )
        for candidate in candidates
    ]


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


def _make_xy_refined_point(
    vehicle_id: str,
    point_id: int,
    timestamp: float,
    x: float,
    y: float,
    *,
    subtrack_index: int,
    sequence_in_subtrack: int,
) -> TrajectoryPoint:
    """Build one already-projected p/i point with a stable Refine identity."""
    lon, lat = _TEST_TRANSFORMER.inverse(x, y)
    return replace(
        _make_point(
            vehicle_id,
            str(point_id),
            timestamp,
            lon,
            lat,
            90.0,
            5.0,
        ),
        x=x,
        y=y,
        refined_point_ref=RefinedPointRef(
            point_kind="kept",
            point_id=point_id,
            subtrack_index=subtrack_index,
            refined_sequence_in_subtrack=sequence_in_subtrack,
        ),
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
            event_calculate_index=0,
            vehicle_id="v1",
            timestamp=1000.0,
            lon=116.0, lat=39.0,
            x=0.0, y=0.0,
            entry_bearing=0.0, exit_bearing=90.0,
            turn_deg=90.0, score=90.0,
        )
        d = event.as_dict()
        self.assertEqual(d["event_calculate_index"], 0)
        self.assertEqual(d["vehicle_id"], "v1")
        self.assertEqual(d["turn_deg"], 90.0)


class IntersectReportTest(unittest.TestCase):
    """I2-02: IntersectReport serialization."""

    def test_report_as_dict(self):
        report = IntersectReport(
            track_count=10,
            turn_event_count=100,
            hotspot_event_count=50,
            cross_track_candidate_count=5,
            cluster_count=5,
            cluster_count_turn=3,
            cluster_count_cross_track=1,
            complete_intersection_count=3,
        )
        d = report.as_dict()
        self.assertEqual(d["track_count"], 10)
        self.assertEqual(d["cluster_count"], 5)
        self.assertEqual(d["complete_intersection_source_counts"], {})


class IntersectConfigStageIdentityTest(unittest.TestCase):
    def test_config_uses_refine_id_without_run_id_alias(self):
        config = detect_context(refine_id="refine-9")

        self.assertEqual(config.refine_id, "refine-9")
        self.assertFalse(hasattr(config, "run_id"))


# ── Group by vehicle tests ─────────────────────────────────────────────


class PairedSegmentIntersectionTest(unittest.TestCase):
    """Exact cross-track geometry operates on explicit segment pairs only."""

    def test_exact_intersection_uses_matching_pairs_without_cartesian_product(self):
        a1 = np.array([[0.0, -1.0], [10.0, -1.0]])
        a2 = np.array([[0.0, 1.0], [10.0, 1.0]])
        b1 = np.array([[-1.0, 0.0], [9.0, 2.0]])
        b2 = np.array([[1.0, 0.0], [11.0, 2.0]])
        ha = np.array([0.0, 0.0])
        hb = np.array([90.0, 90.0])

        xs, ys, hit_indices = _paired_segment_intersections(
            a1, a2, ha, b1, b2, hb, 80.0, 100.0,
        )

        self.assertEqual(hit_indices.tolist(), [0])
        self.assertEqual(xs.tolist(), [0.0])
        self.assertEqual(ys.tolist(), [0.0])

    def test_pair_batches_never_exceed_the_configured_limit(self):
        """Dense cells remain memory bounded before exact geometry runs."""
        pair_batch_size = intersect_config().cross_track_pair_batch_size
        side = math.isqrt(pair_batch_size) + 1
        batches = list(_iter_cross_track_pair_batches(
            list(range(side)), list(range(side)), pair_batch_size,
        ))

        self.assertEqual(sum(len(a_ids) for a_ids, _ in batches), side * side)
        self.assertTrue(all(
            len(a_ids) <= pair_batch_size
            for a_ids, _ in batches
        ))
        self.assertEqual(batches[0][0][0], 0)
        self.assertEqual(batches[-1][1][-1], side - 1)

    def test_cross_vehicle_batches_never_materialize_same_vehicle_pairs(self):
        ranks = np.asarray([0, 0, 0, 1, 1], dtype=np.int64)
        batches = list(_iter_cross_vehicle_pair_batches(
            np.arange(5, dtype=np.int64), ranks, batch_size=4,
        ))
        pairs = [
            (int(left), int(right))
            for left_ids, right_ids in batches
            for left, right in zip(left_ids, right_ids)
        ]

        self.assertEqual(
            pairs,
            [(0, 3), (0, 4), (1, 3), (1, 4), (2, 3), (2, 4)],
        )
        self.assertTrue(all(len(left_ids) <= 4 for left_ids, _ in batches))

    def test_cross_track_preparation_projects_shared_endpoints_once(self):
        track = [
            _make_point("A", str(index), float(index), 116.0 + index * 0.001, 39.0, 0.0, 5.0)
            for index in range(4)
        ]

        with patch.object(
            _TEST_TRANSFORMER, "forward", wraps=_TEST_TRANSFORMER.forward
        ) as project:
            segments, vehicle_count = _prepare_cross_track_segments(
                {"A": track}, _TEST_TRANSFORMER, intersect_config(),
            )

        self.assertEqual(vehicle_count, 1)
        self.assertEqual(len(segments), 3)
        self.assertEqual(project.call_count, len(track))


class CompactAabbBroadPhaseTest(unittest.TestCase):
    """Regression coverage for the strict window broad/exact seam."""

    def test_aabb_rejection_skips_exact_pairs_and_keeps_closed_endpoint(self):
        # First A segment is disjoint from both B segments.  The second touches
        # B[0] at an endpoint; closed AABBs must retain that historical hit.
        a1 = np.array([[0.0, 0.0], [10.0, 0.0]])
        a2 = np.array([[1.0, 0.0], [11.0, 0.0]])
        ha = np.array([90.0, 90.0])
        b1 = np.array([[10.0, -1.0], [30.0, -1.0]])
        b2 = np.array([[10.0, 1.0], [30.0, 1.0]])
        hb = np.array([0.0, 0.0])
        telemetry: dict[str, int] = {}

        blocks = list(_iter_bounded_segment_intersections(
            a1, a2, ha, b1, b2, hb, 80.0, 100.0,
            max_matrix_elements=2, telemetry=telemetry,
        ))

        self.assertEqual(telemetry["broad_pairs"], 1)
        self.assertEqual(telemetry["exact_pairs"], 1)
        self.assertEqual(telemetry["exact_skipped_pairs"], 3)
        self.assertEqual(telemetry["empty_broad_blocks"], 1)
        self.assertEqual(len(blocks), 1)
        a_offset, cx, cy, flat_idx = blocks[0]
        self.assertEqual(a_offset, 1)
        self.assertEqual(flat_idx.tolist(), [0])
        self.assertEqual(cx.tolist(), [10.0])
        self.assertEqual(cy.tolist(), [0.0])

    def test_compact_exact_matches_dense_kernel_in_row_major_order(self):
        a1 = np.array([[0.0, 0.0], [0.0, 2.0], [0.0, 4.0]])
        a2 = np.array([[4.0, 0.0], [4.0, 2.0], [4.0, 4.0]])
        ha = np.array([90.0, 90.0, 90.0])
        b1 = np.array([[1.0, -1.0], [3.0, -1.0], [8.0, -1.0]])
        b2 = np.array([[1.0, 5.0], [3.0, 5.0], [8.0, 5.0]])
        hb = np.array([0.0, 0.0, 0.0])

        dense_x, dense_y, dense_hits = _batch_segment_intersections(
            a1, a2, ha, b1, b2, hb, 80.0, 100.0,
        )
        compact_hits: list[int] = []
        compact_xy: list[tuple[float, float]] = []
        for a_offset, cx, cy, flat_idx in _iter_bounded_segment_intersections(
            a1, a2, ha, b1, b2, hb, 80.0, 100.0,
            max_matrix_elements=3,
        ):
            compact_hits.extend((a_offset * len(b1) + flat_idx).tolist())
            compact_xy.extend(zip(cx.tolist(), cy.tolist()))

        self.assertEqual(compact_hits, dense_hits.tolist())
        self.assertEqual(
            compact_xy,
            [(float(dense_x.flat[i]), float(dense_y.flat[i])) for i in dense_hits],
        )

    def test_b_segments_over_cap_are_two_dimensionally_bounded(self):
        """B wider than the cap never creates an oversized broad/exact block."""
        a1 = np.array([[0.0, 0.0], [0.0, 2.0]])
        a2 = np.array([[7.0, 0.0], [7.0, 2.0]])
        ha = np.array([90.0, 90.0])
        b1 = np.array([[float(x), -1.0] for x in range(8)])
        b2 = np.array([[float(x), 3.0] for x in range(8)])
        hb = np.zeros(8)
        cap = 3
        telemetry: dict[str, int] = {}

        dense_x, dense_y, dense_hits = _batch_segment_intersections(
            a1, a2, ha, b1, b2, hb, 80.0, 100.0,
        )
        bounded_hits: list[int] = []
        bounded_xy: list[tuple[float, float]] = []
        for a_offset, cx, cy, flat_idx in _iter_bounded_segment_intersections(
            a1, a2, ha, b1, b2, hb, 80.0, 100.0,
            max_matrix_elements=cap,
            telemetry=telemetry,
        ):
            bounded_hits.extend((a_offset * len(b1) + flat_idx).tolist())
            bounded_xy.extend(zip(cx.tolist(), cy.tolist()))

        self.assertEqual(bounded_hits, dense_hits.tolist())
        self.assertEqual(
            bounded_xy,
            [(float(dense_x.flat[i]), float(dense_y.flat[i])) for i in dense_hits],
        )
        self.assertEqual(telemetry["matrix_element_cap"], cap)
        self.assertLessEqual(telemetry["max_broad_block_pairs"], cap)
        self.assertLessEqual(telemetry["max_exact_chunk_pairs"], cap)
        self.assertEqual(telemetry["broad_pairs"], len(a1) * len(b1))
        self.assertEqual(telemetry["exact_pairs"], len(a1) * len(b1))
        self.assertGreater(telemetry["broad_blocks"], len(a1))

    def test_hybrid_threshold_boundary_preserves_dense_hits_order_xy_and_cap(self):
        """1023/1024 use dense-direct; 1025 falls back to bounded broad."""
        for pairs, direct in ((1023, True), (1024, True), (1025, False)):
            a1 = np.array([[0.0, 0.0]])
            a2 = np.array([[2.0, 0.0]])
            ha = np.array([90.0])
            b1 = np.array([[1.0, -1.0]] * pairs)
            b2 = np.array([[1.0, 1.0]] * pairs)
            hb = np.zeros(pairs)
            dense_x, dense_y, dense_hits = _batch_segment_intersections(
                a1, a2, ha, b1, b2, hb, 80.0, 100.0,
            )
            telemetry: dict[str, int] = {}
            blocks = list(_iter_bounded_segment_intersections(
                a1, a2, ha, b1, b2, hb, 80.0, 100.0,
                max_matrix_elements=262_144, telemetry=telemetry,
            ))
            hits = [index for offset, _x, _y, flat in blocks for index in (offset + flat).tolist()]
            xy = [(float(x), float(y)) for _offset, cx, cy, _flat in blocks for x, y in zip(cx, cy)]
            self.assertEqual(hits, dense_hits.tolist())
            self.assertEqual(xy, [(float(dense_x.flat[i]), float(dense_y.flat[i])) for i in dense_hits])
            if direct:
                self.assertEqual(telemetry["dense_direct_blocks"], 1)
                self.assertEqual(telemetry["dense_direct_pairs"], pairs)
                self.assertEqual(telemetry["max_dense_direct_pairs"], pairs)
                self.assertNotIn("broad_blocks", telemetry)
            else:
                self.assertEqual(telemetry["broad_blocks"], 1)
                self.assertLessEqual(telemetry["max_broad_block_pairs"], 262_144)
                self.assertLessEqual(telemetry["max_exact_chunk_pairs"], 262_144)

    def test_dense_direct_never_exceeds_smaller_matrix_cap(self):
        a1 = np.array([[0.0, 0.0]])
        a2 = np.array([[2.0, 0.0]])
        ha = np.array([90.0])
        b1 = np.array([[1.0, -1.0]] * 64)
        b2 = np.array([[1.0, 1.0]] * 64)
        telemetry: dict[str, int] = {}
        list(_iter_bounded_segment_intersections(a1, a2, ha, b1, b2, np.zeros(64), 80.0, 100.0, max_matrix_elements=32, telemetry=telemetry))
        self.assertNotIn("dense_direct_blocks", telemetry)
        self.assertLessEqual(telemetry["max_broad_block_pairs"], 32)
        self.assertLessEqual(telemetry["max_exact_chunk_pairs"], 32)


class VehicleAabbPrefilterTest(unittest.TestCase):
    def _track(self, vehicle, points, heading):
        return [_make_point(vehicle, f"{vehicle}-{i}", float(i), lon, lat, heading, 1.0)
                for i, (lon, lat) in enumerate(points)]

    @staticmethod
    def _payload(candidate):
        return (
            candidate.point_ref,
            candidate.vehicle_id,
            candidate.timestamp,
            candidate.lon,
            candidate.lat,
            candidate.x,
            candidate.y,
            candidate.heading,
            candidate.speed,
            candidate.from_segment_start_point_ref,
            candidate.to_segment_start_point_ref,
        )

    def _historical_oracle(self, tracks, origin_lon=116.0, origin_lat=39.0):
        return _extract_orthogonal_cross_track_candidates_legacy(
            tracks, origin_lon, origin_lat, intersect_config(),
            vehicle_aabb_prefilter=False,
        )

    def test_rejected_pairs_skip_kernel_but_keep_pair_count(self):
        tracks = {"A": self._track("A", [(116, 39), (116.001, 39)], 90),
                  "B": self._track("B", [(117, 40), (117.001, 40)], 90)}
        telemetry = {}
        with patch("track.v2._cross_track_legacy._iter_bounded_segment_intersections") as kernel:
            candidates, pairs, total = _extract_orthogonal_cross_track_candidates_legacy(
                tracks, 116, 39, intersect_config(), telemetry=telemetry,
            )
        self.assertEqual((candidates, pairs, total), ([], 1, 1))
        kernel.assert_not_called()
        self.assertEqual(
            telemetry["vehicle_aabb_pairs"],
            telemetry["vehicle_aabb_rejected"] + telemetry.get("vehicle_kernel_pairs", 0),
        )

    def test_closed_endpoint_calls_kernel_and_preserves_candidate_order(self):
        tracks = {"A": self._track("A", [(116, 38.999), (116, 39)], 0),
                  "B": self._track("B", [(115.999, 39), (116, 39)], 90)}
        telemetry = {}
        with patch(
            "track.v2._cross_track_legacy._iter_bounded_segment_intersections",
            wraps=_iter_bounded_segment_intersections,
        ) as kernel:
            candidates, pairs, total = _extract_orthogonal_cross_track_candidates_legacy(
                tracks, 116, 39, intersect_config(), telemetry=telemetry,
            )
        self.assertEqual((pairs, total), (1, 1))
        self.assertTrue(kernel.called)
        self.assertEqual(telemetry["vehicle_aabb_rejected"], 0)
        self.assertEqual(
            [item.vehicle_id for item in candidates],
            sorted(item.vehicle_id for item in candidates),
        )

    def test_closed_endpoint_returns_known_intersection_and_full_payload_matches_oracle(self):
        tracks = {
            "A": [
                _make_point("A", "A-start", 10.0, 116.0, 38.999, 0.0, 12.0, "source-a"),
                _make_point("A", "A-end", 20.0, 116.0, 39.0, 0.0, 13.0, "source-a"),
            ],
            "B": [
                _make_point("B", "B-start", 30.0, 115.999, 39.0, 90.0, 8.0, "source-b"),
                _make_point("B", "B-end", 40.0, 116.0, 39.0, 90.0, 9.0, "source-b"),
            ],
        }
        oracle, oracle_pairs, oracle_total = self._historical_oracle(tracks)
        telemetry: dict[str, int] = {}
        with patch(
            "track.v2._cross_track_legacy._iter_bounded_segment_intersections",
            wraps=_iter_bounded_segment_intersections,
        ) as kernel:
            actual, pairs, total = _extract_orthogonal_cross_track_candidates_legacy(
                tracks, 116.0, 39.0, intersect_config(), telemetry=telemetry,
            )

        self.assertEqual((pairs, total), (oracle_pairs, oracle_total))
        self.assertEqual(len(actual), 1)
        self.assertEqual(kernel.call_count, 1)
        candidate = actual[0]
        self.assertEqual(candidate.vehicle_id, "A|B")
        self.assertAlmostEqual(candidate.x, 0.0)
        self.assertAlmostEqual(candidate.y, 0.0)
        self.assertAlmostEqual(candidate.lon, 116.0)
        self.assertAlmostEqual(candidate.lat, 39.0)
        self.assertEqual(candidate.point_ref, PointRef("source-a", "A-start", stable=True))
        self.assertEqual(
            candidate.from_segment_start_point_ref,
            PointRef("source-a", "A-start", stable=True),
        )
        self.assertEqual(
            candidate.to_segment_start_point_ref,
            PointRef("source-b", "B-start", stable=True),
        )
        self.assertEqual([self._payload(item) for item in actual], [self._payload(item) for item in oracle])
        self.assertEqual(telemetry["vehicle_aabb_pairs"], 1)
        self.assertEqual(telemetry["vehicle_aabb_rejected"], 0)

    def test_mixed_vehicle_pairs_account_kernel_and_rejection_and_match_oracle(self):
        tracks = {
            "A": self._track("A", [(116.0, 38.999), (116.0, 39.001)], 0.0),
            "B": self._track("B", [(115.999, 39.0), (116.001, 39.0)], 90.0),
            "C": self._track("C", [(117.0, 40.0), (117.001, 40.0)], 90.0),
        }
        oracle, oracle_pairs, oracle_total = self._historical_oracle(tracks)
        telemetry: dict[str, int] = {}
        with patch(
            "track.v2._cross_track_legacy._iter_bounded_segment_intersections",
            wraps=_iter_bounded_segment_intersections,
        ) as kernel:
            actual, pairs, total = _extract_orthogonal_cross_track_candidates_legacy(
                tracks, 116.0, 39.0, intersect_config(), telemetry=telemetry,
            )

        self.assertEqual((pairs, total), (3, 3))
        self.assertEqual((pairs, total), (oracle_pairs, oracle_total))
        self.assertEqual(kernel.call_count, 1)
        self.assertEqual(telemetry["vehicle_aabb_pairs"], 3)
        self.assertEqual(telemetry["vehicle_aabb_rejected"], 2)
        self.assertEqual(telemetry["vehicle_kernel_pairs"], 1)
        self.assertEqual(
            telemetry["vehicle_aabb_pairs"],
            telemetry["vehicle_aabb_rejected"] + telemetry["vehicle_kernel_pairs"],
        )
        self.assertEqual([self._payload(item) for item in actual], [self._payload(item) for item in oracle])

    def test_all_rejected_pairs_emit_intermediate_and_final_progress(self):
        tracks = {
            f"V{index:02d}": self._track(
                f"V{index:02d}",
                [(116.0 + index, 39.0), (116.001 + index, 39.0)],
                90.0,
            )
            for index in range(15)
        }
        telemetry: dict[str, int] = {}
        with patch(
            "track.v2._cross_track_legacy._emit_progress",
            wraps=cross_track_legacy_module._emit_progress,
        ) as progress, patch(
            "track.v2._cross_track_legacy._iter_bounded_segment_intersections",
        ) as kernel:
            candidates, pairs, total = _extract_orthogonal_cross_track_candidates_legacy(
                tracks, 116.0, 39.0, intersect_config(), telemetry=telemetry,
            )

        self.assertEqual((candidates, pairs, total), ([], 105, 105))
        kernel.assert_not_called()
        self.assertEqual(telemetry["vehicle_aabb_pairs"], 105)
        self.assertEqual(telemetry["vehicle_aabb_rejected"], 105)
        self.assertNotIn("vehicle_kernel_pairs", telemetry)
        observed = [
            (call.args[1], call.args[2], call.kwargs.get("final", False))
            for call in progress.call_args_list
        ]
        self.assertIn((100, 105, False), observed)
        self.assertIn((105, 105, True), observed)


class ActivePointsWindowQueryTelemetryTest(unittest.TestCase):
    """Window queries preserve normal behavior when telemetry is disabled."""

    _ROWS = [(
        "vehicle-1", 123.5, 116.1, 39.2, 90.0, 8.0,
        "source-A", "row-7", {"tag": "kept"}, "",
        12_934_999.25, 4_748_000.5, 0, 7, 1, 0,
    )]

    def setUp(self) -> None:
        self.window = build_query_window(QueryWindowRequest(116.1, 39.2, 4.0, 6.0))
        source_contract = SourceTableContract(
            source_table="source-A",
            quoted_table='"custom_points"',
            key_sql_type="bigint",
            has_extra=True,
        )
        patcher = patch(
            "track.v2.db.queries.validate_source_table",
            return_value=source_contract,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    class _Cursor:
        def __init__(self, rows):
            self.rows = rows
            self.query = None
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query, params):
            self.query = query
            self.params = params

        def __iter__(self):
            return iter(self.rows)

    class _Connection:
        def __init__(self, rows):
            self.cursor_instance = ActivePointsWindowQueryTelemetryTest._Cursor(rows)

        def cursor(self, **_kwargs):
            return self.cursor_instance

    def test_query_without_telemetry_returns_points_and_preserves_query_contract(self):
        conn = self._Connection(self._ROWS)
        window = self.window

        points = _query_active_points_in_window(
            conn,
            window,
            table="custom_points",
            source_table="source-A",
            spatial_context=_TEST_SPATIAL_CONTEXT,
            telemetry=None,
        )

        self.assertIn('FROM "custom_points"', conn.cursor_instance.query)
        self.assertNotIn("injected_points", conn.cursor_instance.query)
        self.assertIn(
            "geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
            conn.cursor_instance.query,
        )
        self.assertIn(
            f"ST_X(ST_Transform(geom, {window.query_srid})) >= %s",
            conn.cursor_instance.query,
        )
        self.assertEqual(
            conn.cursor_instance.params,
            [
                "source-A", *window.prefilter_bounds,
                window.x_min, window.y_min, window.x_max, window.y_max,
            ],
        )
        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point.vehicle_id, "vehicle-1")
        self.assertEqual(point.timestamp, 123.5)
        self.assertEqual((point.lon, point.lat), (116.1, 39.2))
        self.assertEqual((point.x, point.y), (12_934_999.25, 4_748_000.5))
        self.assertEqual(point.point_ref, PointRef("source-A", "row-7", stable=True))
        self.assertEqual(point.extra, {"tag": "kept"})

    def test_query_kept_mode_joins_frozen_refine_members_without_active_filter(self):
        conn = self._Connection(self._ROWS)
        window = self.window

        points = _query_active_points_in_window(
            conn,
            window,
            table="tp",
            kept_table="ktp",
            source_table="source-A",
            refine_id="refine-9",
            spatial_context=_TEST_SPATIAL_CONTEXT,
            telemetry=None,
        )

        self.assertIn("FROM ktp AS ktp", conn.cursor_instance.query)
        self.assertIn('JOIN "custom_points" AS tp', conn.cursor_instance.query)
        self.assertIn("ON tp.id = ktp.source_row_key::bigint", conn.cursor_instance.query)
        self.assertIn("ktp.refine_id = %s", conn.cursor_instance.query)
        self.assertNotIn("is_active", conn.cursor_instance.query)
        self.assertIn(
            "FROM injected_points   WHERE geom && "
            "ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
            conn.cursor_instance.query,
        )
        self.assertEqual(
            conn.cursor_instance.params,
            [
                "source-A",
                "refine-9",
                *window.prefilter_bounds,
                window.x_min, window.y_min, window.x_max, window.y_max,
                "public.injected_points",
                *window.prefilter_bounds,
                window.x_min, window.y_min, window.x_max, window.y_max,
                "refine-9",
            ],
        )
        self.assertEqual(len(points), 1)

    def test_query_kept_mode_requires_refine_id(self):
        conn = self._Connection(self._ROWS)

        with self.assertRaises(ValueError):
            _query_active_points_in_window(
                conn,
                SpatialWindow(-1.0, -2.0, 3.0, 4.0),
                kept_table="ktp",
                refine_id=None,
                spatial_context=_TEST_SPATIAL_CONTEXT,
            )

    def test_window_preserves_injected_database_identity_provenance_and_xy(self):
        rows = [(
            "vehicle-1", 123.5, 116.1, 39.2, 90.0, 0.0,
            "public.injected_points", "282", {}, "orthogonal_heading",
            12_946_000.25, 4_762_000.5, 1, 282, 1, 0,
        )]
        conn = self._Connection(rows)

        points = _query_active_points_in_window(
            conn,
            self.window,
            table="tp",
            kept_table="ktp",
            source_table="source-A",
            refine_id="refine-9",
            spatial_context=_TEST_SPATIAL_CONTEXT,
        )

        outer_select, _separator, _branches = conn.cursor_instance.query.partition("FROM (")
        self.assertNotIn("from_source_row_key", outer_select)
        self.assertNotIn("to_source_row_key", outer_select)
        self.assertIn("generated_reason", outer_select)
        self.assertIn("x, y", outer_select)
        self.assertEqual(
            points[0].point_ref,
            PointRef(
                "public.injected_points",
                "282",
                stable=False,
                generated_reason="orthogonal_heading",
            ),
        )
        self.assertEqual((points[0].x, points[0].y), (12_946_000.25, 4_762_000.5))

    def test_stream_uses_injected_table_and_id_as_point_identity(self):
        rows = [(
            "vehicle-1", 123.5, 116.1, 39.2, 90.0, 0.0,
            "public.injected_points", "282", {}, "heading_orthogonal",
            12_946_000.25, 4_762_000.5, 1, 282, 1, 0,
        )]
        conn = self._Connection(rows)

        tracks = list(_stream_vehicle_tracks_from_db(
            conn,
            table="tp",
            kept_table="ktp",
            refine_id="refine-9",
            spatial_context=_TEST_SPATIAL_CONTEXT,
        ))

        self.assertEqual(
            conn.cursor_instance.params,
            ["tp", "refine-9", "public.injected_points", "refine-9"],
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(tracks[0]), 1)
        self.assertEqual(
            tracks[0][0].point_ref,
            PointRef(
                "public.injected_points",
                "282",
                stable=False,
                generated_reason="heading_orthogonal",
            ),
        )
        self.assertEqual((tracks[0][0].x, tracks[0][0].y), (12_946_000.25, 4_762_000.5))

    def test_query_with_telemetry_records_disjoint_query_and_materialization_phases(self):
        conn = self._Connection(self._ROWS)
        telemetry: dict[str, int | float] = {}

        with patch(
            "track.v2.db.queries.time.perf_counter",
            side_effect=(10.0, 12.5, 20.0, 20.75),
        ):
            points = _query_active_points_in_window(
                conn,
                self.window,
                spatial_context=_TEST_SPATIAL_CONTEXT,
                telemetry=telemetry,
            )

        self.assertEqual(len(points), 1)
        self.assertEqual(telemetry["queried_rows"], 1)
        self.assertEqual(telemetry["query_seconds"], 2.5)
        self.assertEqual(telemetry["materialization_seconds"], 0.75)


class PerfWindowsGateContractTest(unittest.TestCase):
    """The frozen performance gate is immutable and formal-real checks fail closed."""

    def test_manifest_writer_is_write_once(self):
        from track.v2.manual_thin_benchmark.perf_windows_gate import _write_manifest_once

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gate_manifest.json"
            output.write_text("frozen\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                _write_manifest_once(output, {"replacement": True})

            self.assertEqual(output.read_text(encoding="utf-8"), "frozen\n")

    def test_real_gate_rejects_drifted_incomplete_database(self):
        from track.v2.manual_thin_benchmark.manual_bench_windows import _validate_real_gate

        frozen = {
            "available": True,
            "database": "track_test",
            "table": "trajectory_points",
            "row_count": 1_000_000,
            "extent": [108.8, 34.6, 110.9, 35.899982],
            "stable_id_checksum": "frozen-checksum",
        }
        observed = {
            "available": True,
            "read_only": True,
            "database": "track_test",
            "table": "trajectory_points",
            "row_count": 200_000,
            "extent": [108.8, 34.600008, 110.899942, 35.899963],
            "columns": ["id", "vehicle_id", "timestamp", "lon", "lat"],
            "source_row_key_checksum": None,
            "full_table_sha256": None,
        }

        validation = _validate_real_gate(frozen, observed)

        self.assertFalse(validation["formal_valid"])
        self.assertEqual(
            set(validation["error_codes"]),
            {
                "frozen_schema_sha256_missing",
                "frozen_full_table_sha256_missing",
                "row_count_mismatch",
                "extent_mismatch",
                "source_row_key_missing",
                "source_row_key_checksum_mismatch",
            },
        )


class CrossTrackWindowProgressTest(unittest.TestCase):
    """Method 3 reports window-level progress even for small windows."""

    def test_cross_track_windows_prints_current_and_total_window(self):
        cfg = intersect_run_config(
            spatial_window_size=(1_000.0, 1_000.0),
            spatial_window_overlap=0.0,
        )
        with patch("track.v2.detect._query_active_points_in_window", return_value=[]), \
             patch("builtins.print") as print_mock:
            _extract_cross_track_windows(
                object(), cfg,
                extent=(0.0, 0.0, 2_500.0, 2_500.0),
                spatial_context=_TEST_SPATIAL_CONTEXT,
                transformer=_TEST_TRANSFORMER,
            )

        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("当前窗口/Current window:\t1/9", output)

    def test_cross_track_windows_deduplicate_overlapping_segment_pairs(self):
        origin_lon, origin_lat = 116.0, 39.0
        points = [
            _make_point("A", "102", 10.0, origin_lon, origin_lat - 0.001, 0.0, 12.0, "table_a"),
            _make_point("A", "105", 20.0, origin_lon, origin_lat + 0.001, 0.0, 12.0, "table_a"),
            _make_point("B", "77", 10.0, origin_lon - 0.001, origin_lat, 90.0, 8.0, "table_b"),
            _make_point("B", "78", 20.0, origin_lon + 0.001, origin_lat, 90.0, 8.0, "table_b"),
        ]
        cfg = intersect_run_config(
            spatial_window_size=(1_000.0, 1_000.0),
            spatial_window_overlap=0.15,
            cross_track_dbscan_min_samples=1,
        )
        with patch("track.v2.detect._query_active_points_in_window", return_value=points), \
              patch("builtins.print"):
            centre_x, centre_y = _TEST_TRANSFORMER.forward(origin_lon, origin_lat)
            candidates = _extract_cross_track_windows(
                object(), cfg,
                extent=(centre_x - 1_000.0, centre_y - 1_000.0,
                        centre_x + 1_000.0, centre_y + 1_000.0),
                spatial_context=_TEST_SPATIAL_CONTEXT,
                transformer=_TEST_TRANSFORMER,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].vehicle_id, "A|B")


class GroupByVehicleTest(unittest.TestCase):
    """I2-03: Grouping is independent of source row order and CRS choice."""

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
            handle = get_database(
                source, field_config=pipeline_config().parser.fields,
                runtime=runtime_config(),
            )
            tracks = _group_by_vehicle(handle)
            self.assertEqual(len(tracks), 2)
            self.assertIn("v1", tracks)
            self.assertIn("v2", tracks)
            self.assertEqual(len(tracks["v1"]), 2)
            self.assertEqual(len(tracks["v2"]), 1)
            handle.close()


class ProjectionContextContractTest(unittest.TestCase):
    """Detect uses one explicit Run projection regardless of input ordering."""

    class _Handle:
        def __init__(self, points=(), spatial_context=None):
            self._points = tuple(points)
            self.spatial_context = spatial_context

        def iter_points(self):
            yield from self._points

    def test_detect_requires_explicit_run_spatial_context(self):
        with self.assertRaisesRegex(V2Error, "requires.*SpatialContext"):
            detect_intersections(
                self._Handle(), config=intersect_config(), context=detect_context(),
                runtime=runtime_config(),
            )

    def test_empty_result_carries_parent_run_spatial_context(self):
        result = detect_intersections(
            self._Handle(), config=intersect_config(), context=detect_context(),
            runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
        )

        self.assertEqual(result.spatial_context, _TEST_SPATIAL_CONTEXT)

    def test_refine_handle_context_must_match_detect_context(self):
        with self.assertRaisesRegex(V2Error, "does not match"):
            detect_intersections(
                self._Handle(spatial_context=object()),
                config=intersect_config(), context=detect_context(),
                runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
            )

    def test_projection_is_independent_of_which_source_point_arrives_first(self):
        points = [
            _make_point("v1", "west", 1.0, 115.9, 38.9, 90.0, 8.0),
            _make_point("v1", "east", 2.0, 116.1, 39.1, 90.0, 8.0),
        ]

        forward = _project_tracks({"v1": points}, _TEST_TRANSFORMER)
        reversed_input = _project_tracks(
            {"v1": list(reversed(points))}, _TEST_TRANSFORMER
        )
        forward_by_ref = {
            point.point_ref.source_row_key: (point.x, point.y)
            for point in forward["v1"]
        }
        reversed_by_ref = {
            point.point_ref.source_row_key: (point.x, point.y)
            for point in reversed_input["v1"]
        }
        self.assertEqual(forward_by_ref, reversed_by_ref)
        for point in points:
            expected = _TEST_TRANSFORMER.forward(point.lon, point.lat)
            actual = forward_by_ref[point.point_ref.source_row_key]
            self.assertAlmostEqual(actual[0], expected[0], places=7)
            self.assertAlmostEqual(actual[1], expected[1], places=7)

    def test_formal_db_detect_requires_refine_before_any_data_query(self):
        with self.assertRaisesRegex(V2Error, "requires refine_id"):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True),
                runtime=runtime_config(),
            )

    def test_formal_db_detect_requires_runtime_and_data_root(self):
        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        reader = SimpleNamespace(descriptor=SimpleNamespace(
            refine_id="refine-1", source_fingerprint_json='{"version":1}',
        ))
        with (
            patch("track.v2.detect.fetch_refine_run_row", return_value=refine),
            self.assertRaisesRegex(V2Error, "runtime and data_root"),
        ):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                runtime=runtime_config(), refine_reader=reader,
            )

    def test_formal_db_detect_requires_columnar_reader(self):
        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        with (
            patch("track.v2.detect.fetch_refine_run_row", return_value=refine),
            patch("track.v2.db.source_table.verify_refine_source_fingerprint") as fingerprint,
            self.assertRaisesRegex(V2Error, "RefineDatasetReader"),
        ):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                runtime=runtime_config(), data_root=Path(tempfile.mkdtemp()),
            )
        fingerprint.assert_not_called()

    def test_formal_parquet_detect_rejects_fingerprint_mismatch_without_pg_scan(self):
        refine = SimpleNamespace(
            refine_id="11111111-1111-1111-1111-111111111111",
            source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT,
            source_fingerprint={"version": 1},
        )
        reader = SimpleNamespace(descriptor=SimpleNamespace(
            refine_id=refine.refine_id,
            source_fingerprint_json='{"version":2}',
        ))
        with (
            patch("track.v2.detect.fetch_refine_run_row", return_value=refine),
            patch("track.v2.detect._detect_intersections_db") as compute,
            self.assertRaisesRegex(V2Error, "fingerprint"),
        ):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id=refine.refine_id),
                runtime=runtime_config(), refine_reader=reader,
            )
        compute.assert_not_called()

    def test_formal_parquet_empty_extent_avoids_pg_point_queries(self):
        reader = MagicMock()
        reader.extent.return_value = None
        result = _detect_intersections_db(
            object(), intersect_config(), _TEST_SPATIAL_CONTEXT,
            runtime=runtime_config(), data_root=Path(tempfile.mkdtemp()),
            refine_reader=reader,
        )
        reader.iter_tracks.assert_not_called()
        self.assertEqual(result.report.track_count, 0)

    def test_formal_parquet_cross_track_uses_columnar_pairs_not_disk_index(self):
        reader = MagicMock()
        expected = []
        with (
            patch("track.v2.detect.extract_columnar_cross_track_candidates",
                  return_value=expected) as columnar,
            patch("track.v2.detect_candidates._prepare_segments") as prepare,
            patch("track.v2.detect_candidates.build_cross_track_index") as build_index,
        ):
            actual = _extract_cross_track_db(
                reader, intersect_run_config(source_table="source"), _TEST_TRANSFORMER,
            )
        self.assertIs(actual, expected)
        columnar.assert_called_once()
        prepare.assert_not_called()
        build_index.assert_not_called()

    def test_disabled_formal_parquet_cross_track_does_not_read_pair_planner(self):
        reader = MagicMock()
        actual = _extract_cross_track_db(
            reader, intersect_run_config(use_cross_track_candidates=False), _TEST_TRANSFORMER,
        )
        self.assertEqual(actual, [])
        reader.iter_cross_track_pair_batches.assert_not_called()

    def test_formal_db_detect_rejects_assertion_and_source_mismatch(self):
        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        different = SpatialContext(
            4326, 32649,
            {"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": .01},
        )
        with patch("track.v2.detect.fetch_refine_run_row", return_value=refine):
            with self.assertRaisesRegex(V2Error, "does not match"):
                detect_intersections(
                    conn=object(), config=intersect_config(),
                    context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                    runtime=runtime_config(), spatial_context=different,
                )
            with self.assertRaisesRegex(V2Error, "source_table"):
                detect_intersections(
                    conn=object(),
                    config=intersect_config(),
                    context=detect_context(
                        query_active_from_db=True, refine_id="refine-1", source_table="wrong",
                    ),
                    runtime=runtime_config(),
                )

    def test_formal_db_detect_rejects_unready_refine_and_table_overrides_before_reads(self):
        for status, fingerprint in (("running", {"version": 1}), ("completed", None)):
            refine = SimpleNamespace(
                refine_id="refine-1", source_table="raw_points", status=status,
                spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint=fingerprint,
            )
            with (
                self.subTest(status=status, fingerprint=fingerprint),
                patch("track.v2.detect.fetch_refine_run_row", return_value=refine),
            ):
                with self.assertRaisesRegex(V2Error, "completed immutable-source"):
                    detect_intersections(
                        conn=object(),
                        config=intersect_config(),
                        context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                        runtime=runtime_config(),
                    )

        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        for override in (
            {"db_table": "other"},
            {"kept_table": "other_members"},
        ):
            with (
                self.subTest(override=override),
                patch("track.v2.detect.fetch_refine_run_row", return_value=refine),
            ):
                with self.assertRaisesRegex(V2Error, "does not belong|must use"):
                    detect_intersections(
                        conn=object(),
                        config=intersect_config(),
                        context=detect_context(
                            query_active_from_db=True, refine_id="refine-1", **override,
                        ),
                        runtime=runtime_config(),
                    )
    def test_in_memory_handle_does_not_trust_unscoped_existing_xy(self):
        point = replace(
            _make_point("v1", "legacy", 1.0, 116.0, 39.0, 90.0, 8.0),
            x=12.0,
            y=34.0,
        )

        projected = _project_tracks({"v1": [point]}, _TEST_TRANSFORMER)["v1"][0]

        self.assertEqual(
            (projected.x, projected.y),
            _TEST_TRANSFORMER.forward(point.lon, point.lat),
        )

    def test_db_projected_xy_is_preserved_without_reprojection(self):
        point = replace(
            _make_point("v1", "db-row", 1.0, 116.0, 39.0, 0.0, 8.0),
            x=12_345.25,
            y=-98_765.5,
        )

        with patch.object(_TEST_TRANSFORMER, "forward_many") as transform:
            projected = _project_tracks(
                {"v1": [point]}, _TEST_TRANSFORMER, trust_existing=True
            )

        transform.assert_not_called()
        self.assertIs(projected["v1"][0], point)
        self.assertEqual(
            (projected["v1"][0].x, projected["v1"][0].y),
            (12_345.25, -98_765.5),
        )

    def test_half_populated_projected_coordinate_is_rejected(self):
        point = replace(
            _make_point("v1", "bad-row", 1.0, 116.0, 39.0, 0.0, 8.0),
            x=12_345.25,
        )

        with self.assertRaisesRegex(V2Error, "both processing x and y"):
            _project_tracks({"v1": [point]}, _TEST_TRANSFORMER)


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
        cfg = intersect_config(turn_window=2, min_turn_deg=45.0, max_turn_deg=170.0,
                              min_leg_m=20.0, min_movement_speed_mps=1.0)
        events = _detect_turn_events(_with_processing_xy(tracks), cfg)
        self.assertGreaterEqual(len(events), 1, "Should detect at least one turn event")
        # The turn should be near 90°
        for e in events:
            self.assertGreaterEqual(abs(e.turn_deg), cfg.min_turn_deg)

    def test_injected_point_participates_in_method_1_with_provenance(self):
        origin_lon, origin_lat = 116.391, 39.907
        track = _build_track_through_intersection(
            "v1", origin_lon, origin_lat,
            approach_bearing=0.0, exit_bearing=90.0,
        )
        track[4] = replace(
            track[4],
            point_ref=PointRef(
                "public.injected_points",
                "282",
                stable=False,
                from_ref="21022680",
                to_ref="21022681",
                generated_reason=GeneratedReason.HEADING_ORTHOGONAL,
            ),
        )
        cfg = intersect_config(
            use_cross_track_candidates=False,
            turn_window=2,
            min_turn_deg=45.0,
            max_turn_deg=170.0,
            min_leg_m=20.0,
            min_movement_speed_mps=1.0,
            g_star_min_z=-10.0,
            dbscan_min_samples_turn=1,
        )

        projected = _with_processing_xy({"v1": track})
        events = _detect_turn_events(projected, cfg)
        injected_events = [
            event for event in events
            if event.point_ref == track[4].point_ref
        ]
        self.assertEqual(len(injected_events), 1)
        self.assertEqual(injected_events[0].source, "turn")
        self.assertEqual(injected_events[0].point_ref.source_id, "public.injected_points")
        self.assertEqual(injected_events[0].point_ref.source_row_key, "282")

        hotspots, clusters = _cluster_pipeline(
            events, [], _TEST_TRANSFORMER, cfg,
        )
        self.assertTrue(any(
            event.point_ref == injected_events[0].point_ref for event in hotspots
        ))
        self.assertTrue(clusters)
        self.assertEqual(
            {source for cluster in clusters for source in cluster.algorithm_sources},
            {"turn"},
        )

    def test_injected_point_is_not_promoted_when_turn_checks_fail(self):
        origin_lon, origin_lat = 116.391, 39.907
        track = [
            _make_point(
                "v1", str(index), float(index), origin_lon,
                origin_lat + index * 0.0002, 0.0, 10.0,
            )
            for index in range(8)
        ]
        track[4] = replace(
            track[4],
            point_ref=PointRef(
                "public.injected_points", "282", stable=False,
                from_ref="3", to_ref="4",
                generated_reason=GeneratedReason.HEADING_ORTHOGONAL,
            ),
        )
        cfg = intersect_config(
            use_cross_track_candidates=False,
            min_leg_m=20.0,
            min_movement_speed_mps=1.0,
            g_star_min_z=-10.0,
            dbscan_min_samples_turn=1,
        )

        events = _detect_turn_events(_with_processing_xy({"v1": track}), cfg)
        hotspots, clusters = _cluster_pipeline(
            events, [], _TEST_TRANSFORMER, cfg,
        )

        self.assertEqual(events, [])
        self.assertEqual(hotspots, [])
        self.assertEqual(clusters, [])

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
        tracks = _with_processing_xy({"v1": points})
        cfg = intersect_config()
        events = _detect_turn_events(tracks, cfg)
        self.assertEqual(len(events), 0)

    def test_min_turn_deg_filter(self):
        """I2-06: Turns below min_turn_deg are filtered out."""
        origin_lon, origin_lat = 116.391, 39.907
        # A 30° turn — should be filtered with min_turn_deg=45
        track = _build_track_through_intersection(
            "v1", origin_lon, origin_lat, approach_bearing=0.0, exit_bearing=30.0
        )
        tracks = {"v1": track}
        cfg = intersect_config(min_turn_deg=45.0, min_leg_m=20.0, min_movement_speed_mps=1.0)
        events = _detect_turn_events(_with_processing_xy(tracks), cfg)
        self.assertEqual(len(events), 0, "30° turn should be filtered by min_turn_deg=45")

    def test_max_turn_deg_filter(self):
        """I2-07: Turns above max_turn_deg are filtered out."""
        origin_lon, origin_lat = 116.391, 39.907
        # A 180° U-turn — should be filtered with max_turn_deg=170
        track = _build_track_through_intersection(
            "v1", origin_lon, origin_lat, approach_bearing=0.0, exit_bearing=180.0
        )
        tracks = {"v1": track}
        cfg = intersect_config(max_turn_deg=170.0, min_leg_m=20.0, min_movement_speed_mps=1.0)
        events = _detect_turn_events(_with_processing_xy(tracks), cfg)
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
        tracks = _with_processing_xy({"v1": points})
        cfg = intersect_config(min_leg_m=35.0, min_turn_deg=45.0, min_movement_speed_mps=0.5)
        events = _detect_turn_events(tracks, cfg)
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

    def test_radius_boundary_and_non_contiguous_ids_match_exact_scan(self):
        """G* keeps inclusive-radius semantics without relying on sequential IDs."""
        radius = 180.0
        below = math.nextafter(radius, 0.0)
        above = math.nextafter(radius, math.inf)
        events = [
            TurnEvent(10, "v1", 1, 116, 39, 0.0, 0.0, 0, 90, 90, 90),
            TurnEvent(27, "v2", 2, 116, 39, radius, 0.0, 0, 90, 90, 90),
            TurnEvent(51, "v3", 3, 116, 39, below, 0.0, 0, 90, 90, 90),
            TurnEvent(88, "v4", 4, 116, 39, above, 0.0, 0, 90, 90, 90),
            TurnEvent(103, "v5", 5, 116, 39, -radius, 0.0, 0, 90, 90, 90),
        ]
        radius_sq = radius * radius
        counts = [
            sum((other.x - event.x) ** 2 + (other.y - event.y) ** 2 <= radius_sq for other in events)
            for event in events
        ]
        mean = sum(counts) / len(counts)
        variance = sum(value * value for value in counts) / len(counts) - mean * mean
        expected = {
            event.event_calculate_index: (count - mean) / math.sqrt(variance)
            for event, count in zip(events, counts)
        }
        self.assertEqual(_compute_getis_ord_g_star(events, radius), expected)

    @staticmethod
    def _large_events(points: list[tuple[float, float]]) -> list[TurnEvent]:
        """Build >256 non-contiguous IDs so the production grid is mandatory."""
        return [
            TurnEvent(10_000 + index * 13, f"v{index}", 1_700_000_000 + index,
                      116.0, 39.0, x, y, 0.0, 90.0, 90.0,
                      20.0 if index % 17 == 0 else 90.0)
            for index, (x, y) in enumerate(points)
        ]

    def _assert_large_grid_matches_scan(self, label: str, points: list[tuple[float, float]]) -> None:
        events = self._large_events(points)
        self.assertGreater(len(events), 256, label)
        radius = 180.0
        expected_counts = _getis_local_counts_scan(events, radius)
        mean = sum(expected_counts) / len(expected_counts)
        variance = sum(count * count for count in expected_counts) / len(expected_counts) - mean * mean
        std = math.sqrt(max(variance, 0.0))
        expected_z = {
            event.event_calculate_index: 0.0 if std == 0.0 else (count - mean) / std
            for event, count in zip(events, expected_counts)
        }
        cfg = intersect_config(g_star_radius_m=radius, g_star_min_z=0.5, min_turn_deg=45.0)
        expected_selected = [
            event.event_calculate_index for event in events
            if expected_z[event.event_calculate_index] >= cfg.g_star_min_z
            and abs(event.turn_deg) >= cfg.min_turn_deg
        ]
        # If the threshold/fallback regresses, this mock makes the test fail:
        # successful computation proves the actual grid path was exercised.
        with patch("track.v2.detect._getis_local_counts_scan", side_effect=AssertionError("scan fallback used")):
            actual_counts = _getis_local_counts_grid(events, radius)
            actual_z = _compute_getis_ord_g_star(events, radius)
            actual_selected = [
                event.event_calculate_index
                for event in _select_hotspot_events(events, cfg)
            ]
        self.assertEqual(actual_counts, expected_counts, label)
        self.assertEqual(actual_z, expected_z, label)
        self.assertEqual(actual_selected, expected_selected, label)

    def test_large_grid_seams_duplicates_negative_and_all_coincident(self):
        """Grid is used and exact at radius seams, negative cells and duplicates."""
        radius = 180.0
        below = math.nextafter(radius, 0.0)
        above = math.nextafter(radius, math.inf)
        cases = {
            "radius_nextafter": [(0.0, 0.0), (radius, 0.0), (below, 0.0), (above, 0.0), (-radius, 0.0)] * 52,
            "cross_zero_cell_seam": [(-1e-300, 0.0), (radius, 0.0), (below, 0.0), (above, 0.0), (-radius, 0.0)] * 52,
            "negative_duplicates": [(-180.0, -180.0), (-180.0, -180.0), (-0.1, -0.1), (-360.0, -0.1), (-540.0, -180.0)] * 52,
            "all_coincident": [(3.0, -4.0)] * 260,
        }
        for label, points in cases.items():
            with self.subTest(label=label):
                self._assert_large_grid_matches_scan(label, points)

    def test_large_grid_perf_live_and_repeated_calls_keep_contract(self):
        """Grid keeps G* telemetry names, 500-event live progress and no state leakage."""
        events = self._large_events([(index * 400.0, -index * 400.0) for index in range(600)])

        class Live:
            def __init__(self):
                self.calls: list[dict[str, object]] = []

            def update(self, **kwargs):
                self.calls.append(kwargs)

        live = Live()
        tracker = PerfTracker("gstar-test")
        first = _compute_getis_ord_g_star(events, 180.0, live_status=live, perf=tracker)
        second = _compute_getis_ord_g_star(events, 180.0, live_status=live, perf=tracker)
        self.assertEqual(first, second)
        self.assertTrue(live.calls, "G* must report at least every 500 events")
        report = tracker.report()
        self.assertIn("gstar_events", report)
        self.assertIn("gstar_neighbors", report)
        neighbor_node = tracker._root.children["gstar_events"].children["gstar_neighbors"]
        self.assertEqual(neighbor_node.call_count, len(events) * 2)

    def test_negative_and_overflow_radius_preserve_historical_scan(self):
        """Negative radii use abs cells; overflowed squared radii explicitly scan."""
        events = self._large_events([(index * 400.0, -index * 400.0) for index in range(260)])
        for radius in (-180.0, 1e308):
            with self.subTest(radius=radius):
                self.assertEqual(
                    _getis_local_counts_grid(events, radius),
                    _getis_local_counts_scan(events, radius),
                )

    def test_huge_finite_coordinate_span_falls_back_without_enumeration(self):
        """Huge finite quotient ULPs cannot create an unbounded cell range."""
        events = self._large_events([(1e308, float(index * 3)) for index in range(257)])
        expected_counts = _getis_local_counts_scan(events, 1.0)
        with patch("track.v2.detect._getis_local_counts_scan", wraps=_getis_local_counts_scan) as fallback:
            actual_counts = _getis_local_counts_grid(events, 1.0)
        self.assertEqual(actual_counts, expected_counts)
        fallback.assert_called_once()
        mean = sum(expected_counts) / len(expected_counts)
        variance = sum(count * count for count in expected_counts) / len(expected_counts) - mean * mean
        std = math.sqrt(max(variance, 0.0))
        expected_z = {
            event.event_calculate_index: 0.0 if std == 0.0 else (count - mean) / std
            for event, count in zip(events, expected_counts)
        }
        cfg = intersect_config(g_star_radius_m=1.0, g_star_min_z=0.5, min_turn_deg=45.0)
        expected_selected = [
            event.event_calculate_index for event in events
            if expected_z[event.event_calculate_index] >= cfg.g_star_min_z
            and abs(event.turn_deg) >= cfg.min_turn_deg
        ]
        self.assertEqual(_compute_getis_ord_g_star(events, 1.0), expected_z)
        self.assertEqual(
            [
                event.event_calculate_index
                for event in _select_hotspot_events(events, cfg)
            ],
            expected_selected,
        )


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
        cfg = intersect_config(g_star_radius_m=200.0, g_star_min_z=0.5, min_turn_deg=45.0)
        selected = _select_hotspot_events(events, cfg)
        self.assertGreaterEqual(len(selected), 1, "Clustered events should have z >= 0.5")
        # Event 3 has turn=20 < 45, should be excluded
        for e in selected:
            self.assertGreaterEqual(abs(e.turn_deg), cfg.min_turn_deg)


# ── DBSCAN clustering tests ───────────────────────────────────────────


class DBSCANTest(unittest.TestCase):
    def test_group_cluster_items_single_pass_preserves_order(self):
        grouped = _group_cluster_items(
            ["two-a", "noise", "zero-a", "two-b", "zero-b"],
            [2, -1, 0, 2, 0],
        )
        self.assertEqual(
            grouped,
            [(0, ["zero-a", "zero-b"]), (2, ["two-a", "two-b"])],
        )

    def test_seed_neighbor_telemetry_is_block_level(self):
        tracker = PerfTracker("dbscan-block-test")
        count = 260

        labels = _run_dbscan(
            [0.0] * count,
            [0.0] * count,
            eps_m=1.0,
            min_samples=2,
            perf=tracker,
        )

        self.assertEqual(labels, [0] * count)
        node = tracker._root.children["dbscan_points"].children["dbscan_seed_neighbors"]
        self.assertLessEqual(node.call_count, count)

    @staticmethod
    def _historical_scan_oracle(xs, ys, eps_m, min_samples):
        unvisited, noise = -99, -1
        labels = [unvisited] * len(xs)
        cluster_label = 0
        def neighbors(index):
            result = []
            eps_sq = eps_m * eps_m
            for candidate in range(len(xs)):
                # Match the c12 baseline's multiplication semantics exactly:
                # huge finite deltas become ``inf`` instead of raising as
                # Python's ``value ** 2`` does.
                dx = xs[candidate] - xs[index]
                dy = ys[candidate] - ys[index]
                if dx * dx + dy * dy <= eps_sq:
                    result.append(candidate)
            return result
        for index in range(len(xs)):
            if labels[index] != unvisited:
                continue
            near = neighbors(index)
            if len(near) < min_samples:
                labels[index] = noise
                continue
            labels[index] = cluster_label
            seeds = [candidate for candidate in near if candidate != index]
            while seeds:
                current = seeds.pop()
                if labels[current] == noise:
                    labels[current] = cluster_label
                if labels[current] != unvisited:
                    continue
                labels[current] = cluster_label
                current_near = neighbors(current)
                if len(current_near) >= min_samples:
                    for candidate in current_near:
                        if labels[candidate] in (unvisited, noise) and candidate not in seeds:
                            seeds.append(candidate)
            cluster_label += 1
        return labels
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
        clusters = _cluster_dbscan(events, _TEST_TRANSFORMER,
                                   eps_m=100.0, min_samples=3)
        self.assertEqual(len(clusters), 2, f"Expected 2 clusters, got {len(clusters)}")
        self.assertEqual(clusters[0].algorithm_sources, ("turn",))
        self.assertEqual(clusters[0].source_counts["turn"], 4)
        self.assertEqual(clusters[0].source_ratios["turn"], 1.0)

    def test_noise_labeled(self):
        """I2-14: Single isolated event labeled as noise."""
        origin_lon, origin_lat = 116.0, 39.0
        events = [
            TurnEvent(0, "v1", 1000, 116, 39, 0, 0, 0, 90, 90, 90),
            TurnEvent(1, "v2", 1001, 116, 39, 5000, 5000, 0, 90, 90, 90),
        ]
        clusters = _cluster_dbscan(events, _TEST_TRANSFORMER,
                                   eps_m=100.0, min_samples=3)
        self.assertEqual(len(clusters), 0, "Isolated events should be noise")

    def test_single_cluster(self):
        """I2-15: All events within eps_m → single cluster."""
        origin_lon, origin_lat = 116.0, 39.0
        events = [
            TurnEvent(i, f"v{i}", 1000 + i, 116, 39, i * 5, i * 5, 0, 90, 90, 90)
            for i in range(5)
        ]
        clusters = _cluster_dbscan(events, _TEST_TRANSFORMER,
                                   eps_m=200.0, min_samples=3)
        self.assertEqual(len(clusters), 1)

    def test_labels_match_historical_boundary_contract(self):
        """The spatial index preserves labels, IDs, order, and <= eps semantics."""
        cases = (
            ([], [], 1.0, 1, []),
            ([0.0], [0.0], 1.0, 1, [0]),
            ([0.0, 3.0, 6.0], [0.0, 0.0, 0.0], 1.0, 2, [-1, -1, -1]),
            ([0.0, 0.1, 0.2, 10.0, 10.1, 10.2], [0.0] * 6, 0.5, 2, [0, 0, 0, 1, 1, 1]),
            ([0.0, 0.9, 1.8, 2.7, 3.6], [0.0] * 5, 1.0, 2, [0, 0, 0, 0, 0]),
            ([-1.0, -1.0, -1.0, -4.0, -4.0], [-1.0, -1.0, -1.0, 3.0, 3.0], 0.1, 2, [0, 0, 0, 1, 1]),
            ([0.0, 1.0, math.nextafter(1.0, math.inf)], [0.0] * 3, 1.0, 1, [0, 0, 0]),
            ([0.0, 0.0], [0.0, 0.0], 0.0, 2, [0, 0]),
            ([0.0, 0.5], [0.0, 0.0], -1.0, 2, [0, 0]),
            ([math.nan], [0.0], 1.0, 1, [-1]),
            ([0.0, 1.0], [0.0, 0.0], math.nan, 1, [-1, -1]),
            ([0.0, 1.0], [0.0, 0.0], math.inf, 1, [0, 0]),
            ([0.0, math.inf], [0.0, 0.0], 1.0, 1, [0, -1]),
            ([0.0], [0.0], 1.0, 0, [0]),
            ([0.0], [0.0], 1.0, -1, [0]),
        )
        for xs, ys, eps_m, min_samples, expected in cases:
            with self.subTest(xs=xs, eps_m=eps_m, min_samples=min_samples):
                self.assertEqual(
                    _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples),
                    expected,
                )
        with self.assertRaises(IndexError):
            _run_dbscan([0.0, 1.0], [0.0], eps_m=1.0, min_samples=1)

    def test_grid_matches_historical_scan_across_zero_exact_boundary(self):
        """A one-ULP/cross-zero cell boundary cannot hide an exact neighbour."""
        xs = [-1e-300, 1.0, math.nextafter(1.0, math.inf), math.nextafter(-1e-300, -math.inf)]
        ys = [0.0] * len(xs)
        expected = [0, 0, 0, 0]
        self.assertEqual(_run_dbscan(xs, ys, eps_m=1.0, min_samples=1), expected)

    def test_many_disconnected_clusters_do_not_require_cluster_sized_resets(self):
        """Regression shape for per-cluster queue clearing: 400 singleton clusters."""
        xs = [float(index * 10) for index in range(400)]
        self.assertEqual(_run_dbscan(xs, [0.0] * len(xs), eps_m=0.1, min_samples=1), list(range(400)))

    def test_large_grid_cross_zero_and_nextafter_matches_scan_oracle(self):
        xs = [-1e-300, 1.0, math.nextafter(1.0, math.inf)] + [1000.0 + i * 10.0 for i in range(254)]
        ys = [0.0] * len(xs)
        expected = self._historical_scan_oracle(xs, ys, 1.0, 1)
        with patch("track.v2.detect._neighbors_indices", side_effect=AssertionError("full scan fallback forbidden")):
            actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=1)
        self.assertEqual(actual, expected)

    def test_large_frontier_matches_historical_labels_on_seeded_shapes(self):
        """Active-frontier expansion retains legacy border and cluster order."""
        for seed in range(6):
            rng = random.Random(seed)
            xs = [rng.randrange(-12, 13) * 0.25 for _ in range(300)]
            ys = [rng.randrange(-12, 13) * 0.25 for _ in range(300)]
            for eps_m, min_samples in ((0.25, 1), (0.5, 3), (0.9, 5)):
                with self.subTest(seed=seed, eps_m=eps_m, min_samples=min_samples):
                    expected = self._historical_scan_oracle(
                        xs, ys, eps_m, min_samples,
                    )
                    self.assertEqual(
                        _run_dbscan(
                            xs, ys, eps_m=eps_m, min_samples=min_samples,
                        ),
                        expected,
                    )

    def test_dense_grid_enqueues_each_point_at_most_once(self):
        """Dense clusters do not re-materialise N neighbours for every core."""
        from track.v2.detect import _take_grid_frontier_neighbors

        taken = 0

        def observe(*args):
            nonlocal taken
            result = _take_grid_frontier_neighbors(*args)
            taken += len(result)
            return result

        count = 2_000
        with patch(
            "track.v2.detect._take_grid_frontier_neighbors",
            side_effect=observe,
        ):
            labels = _run_dbscan(
                [0.0] * count,
                [0.0] * count,
                eps_m=1.0,
                min_samples=3,
            )

        self.assertEqual(labels, [0] * count)
        self.assertLessEqual(taken, count)

    def test_low_index_noise_is_later_absorbed_as_border(self):
        """A point marked noise before its adjacent core remains available."""
        xs = [0.0, 0.9, 1.7, 1.5, 1.5]
        ys = [0.0, 0.0, 0.0, 0.6, -0.6]
        xs.extend(100.0 + index * 10.0 for index in range(252))
        ys.extend([100.0] * 252)

        expected = self._historical_scan_oracle(xs, ys, 1.0, 4)
        actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=4)

        self.assertEqual(expected[0], 0)
        self.assertEqual(actual, expected)

    def test_shared_border_keeps_first_discovered_component(self):
        """A border shared by two core components keeps legacy first reach."""
        xs = [
            0.0,
            0.9, 1.7, 1.5, 1.5,
            -0.9, -1.7, -1.5, -1.5,
        ]
        ys = [
            0.0,
            0.0, 0.0, 0.6, -0.6,
            0.0, 0.0, 0.6, -0.6,
        ]
        xs.extend(100.0 + index * 10.0 for index in range(248))
        ys.extend([100.0] * 248)

        expected = self._historical_scan_oracle(xs, ys, 1.0, 4)
        actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=4)

        self.assertEqual(expected[0], expected[1])
        self.assertNotEqual(expected[0], expected[5])
        self.assertEqual(actual, expected)

    def test_large_finite_coordinates_fall_back_before_unbounded_cell_range(self):
        xs = [1e308, math.nextafter(1e308, -math.inf)] + [float(i * 10) for i in range(255)]
        ys = [0.0] * len(xs)
        expected = self._historical_scan_oracle(xs, ys, 1.0, 1)
        build_results = []
        def observe_build(*args):
            value = _build_dbscan_grid(*args)
            build_results.append(value)
            return value
        with patch("track.v2.detect._build_dbscan_grid", side_effect=observe_build) as build_grid:
            actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=1)
        build_grid.assert_called_once_with(xs, ys, 1.0)
        self.assertEqual(build_results, [None])
        self.assertEqual(actual, expected)


# ── Cross-track candidate tests (Method 3) ───────────────────────────────


class CrossTrackCandidateTest(unittest.TestCase):
    """Method 3 cross-track candidate extraction and clustering."""

    def test_cross_track_candidate_keeps_canonical_pair_provenance(self):
        """Cross-track candidates retain both source segments in A/B order."""
        origin_lon, origin_lat = 116.0, 39.0
        tracks = {
            "B": [
                _make_point("B", "77", 10.0, origin_lon - 0.001, origin_lat, 90.0, 8.0, "table_b"),
                _make_point("B", "78", 20.0, origin_lon + 0.001, origin_lat, 90.0, 8.0, "table_b"),
            ],
            "A": [
                _make_point("A", "102", 30.0, origin_lon, origin_lat - 0.001, 0.0, 12.0, "table_a"),
                _make_point("A", "105", 40.0, origin_lon, origin_lat + 0.001, 0.0, 12.0, "table_a"),
            ],
        }

        candidates, pair_count, total_pairs = _extract_orthogonal_cross_track_candidates(
            tracks, _TEST_TRANSFORMER, intersect_config(),
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(pair_count, 1)
        self.assertEqual(total_pairs, 1)
        candidate = candidates[0]
        self.assertEqual(candidate.vehicle_id, "A|B")
        self.assertEqual(
            candidate.from_segment_start_point_ref,
            PointRef("table_a", "102", stable=True),
        )
        self.assertEqual(
            candidate.to_segment_start_point_ref,
            PointRef("table_b", "77", stable=True),
        )
        self.assertEqual(candidate.heading, 0.0)
        self.assertEqual(candidate.speed, 12.0)

    def test_cross_track_grid_deduplicates_multicell_hits_in_legacy_order(self):
        """Long segments cross many cells but emit each ordered pair once."""
        origin_lon, origin_lat = 116.0, 39.0
        tracks = {
            "A": [
                _make_point("A", "a0", 10.0, origin_lon, origin_lat - 0.003, 0.0, 12.0),
                _make_point("A", "a1", 20.0, origin_lon, origin_lat, 0.0, 12.0),
                _make_point("A", "a2", 30.0, origin_lon, origin_lat + 0.003, 0.0, 12.0),
            ],
            "B": [
                _make_point("B", "b0", 10.0, origin_lon - 0.003, origin_lat - 0.0015, 90.0, 8.0),
                _make_point("B", "b1", 20.0, origin_lon + 0.003, origin_lat - 0.0015, 90.0, 8.0),
            ],
            "C": [
                _make_point("C", "c0", 10.0, origin_lon - 0.003, origin_lat + 0.0015, 90.0, 8.0),
                _make_point("C", "c1", 20.0, origin_lon + 0.003, origin_lat + 0.0015, 90.0, 8.0),
            ],
        }

        candidates, pair_count, total_pairs = _extract_orthogonal_cross_track_candidates(
            tracks, _TEST_TRANSFORMER, intersect_config(),
        )

        self.assertEqual((pair_count, total_pairs), (3, 3))
        self.assertEqual(
            [
                (
                    candidate.vehicle_id,
                    candidate.from_segment_start_point_ref.source_row_key,
                    candidate.to_segment_start_point_ref.source_row_key,
                )
                for candidate in candidates
            ],
            [("A|B", "a0", "b0"), ("A|C", "a1", "c0")],
        )

    def test_cross_track_grid_keeps_intersection_on_cell_boundary(self):
        """An intersection at the projected origin is not lost to cell ownership."""
        origin_lon, origin_lat = 116.0, 39.0
        tracks = {
            "A": [
                _make_point("A", "a0", 10.0, origin_lon, origin_lat - 0.001, 0.0, 12.0),
                _make_point("A", "a1", 20.0, origin_lon, origin_lat + 0.001, 0.0, 12.0),
            ],
            "B": [
                _make_point("B", "b0", 10.0, origin_lon - 0.001, origin_lat, 90.0, 8.0),
                _make_point("B", "b1", 20.0, origin_lon + 0.001, origin_lat, 90.0, 8.0),
            ],
        }

        candidates, _, _ = _extract_orthogonal_cross_track_candidates(
            tracks, _TEST_TRANSFORMER, intersect_config(),
        )

        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0].lon, origin_lon, places=9)
        self.assertAlmostEqual(candidates[0].lat, origin_lat, places=9)

    def test_nonfinite_coordinates_fail_projection_before_geometry(self):
        """A fixed Run projection rejects invalid 4326 coordinates explicitly."""
        origin_lon, origin_lat = 116.0, 39.0
        for nonfinite in (float("nan"), float("inf")):
            tracks = {
                "A": [
                    _make_point("A", "a0", 10.0, origin_lon, origin_lat - 0.001, 0.0, 12.0),
                    _make_point("A", "a1", 20.0, origin_lon, origin_lat + 0.001, 0.0, 12.0),
                ],
                "B": [
                    _make_point("B", "b0", 10.0, nonfinite, origin_lat - 0.001, 90.0, 8.0),
                    _make_point("B", "b1", 20.0, nonfinite, origin_lat + 0.001, 90.0, 8.0),
                ],
                "C": [
                    _make_point("C", "c0", 10.0, origin_lon - 0.001, origin_lat, 90.0, 8.0),
                    _make_point("C", "c1", 20.0, origin_lon + 0.001, origin_lat, 90.0, 8.0),
                ],
            }
            with self.assertRaises(ProjectionError):
                _extract_orthogonal_cross_track_candidates(
                    tracks, _TEST_TRANSFORMER, intersect_config(),
                )

    def test_oversized_aabb_uses_bounded_fallback_without_losing_hit(self):
        """A long segment is not expanded into more than the private cell cap."""
        origin_lon, origin_lat = 116.0, 39.0
        tracks = {
            "A": [
                _make_point("A", "a0", 10.0, origin_lon, origin_lat - 2.0, 0.0, 12.0),
                _make_point("A", "a1", 20.0, origin_lon, origin_lat + 2.0, 0.0, 12.0),
            ],
            "B": [
                _make_point("B", "b0", 10.0, origin_lon - 3.0, origin_lat, 90.0, 8.0),
                _make_point("B", "b1", 20.0, origin_lon + 3.0, origin_lat, 90.0, 8.0),
            ],
        }
        segments, _ = _prepare_cross_track_segments(
            tracks, _TEST_TRANSFORMER, intersect_config(),
        )
        max_grid_cells = intersect_config().cross_track_max_grid_cells_per_segment
        index = build_cross_track_index(
            segments, cell_m=intersect_config().cross_track_grid_cell_m, max_cells=max_grid_cells,
        )
        grid, overflow_ids = index.grid, index.overflow_segment_ids
        self.assertEqual(overflow_ids, [0, 1])
        self.assertLessEqual(
            sum(len(ids) for ids in grid.values()),
            max_grid_cells,
        )

        legacy = _extract_orthogonal_cross_track_candidates_legacy(
            tracks, origin_lon, origin_lat, intersect_config(),
        )
        bounded = _extract_orthogonal_cross_track_candidates(
            tracks, _TEST_TRANSFORMER, intersect_config(),
        )
        self.assertEqual(legacy[1:], bounded[1:])
        self.assertEqual(_candidate_keys(legacy[0]), _candidate_keys(bounded[0]))

    def test_seeded_cross_track_differential_preserves_candidate_provenance(self):
        """Fixed-CRS broad phase preserves legacy segment-pair ownership."""
        origin_lon, origin_lat = 116.0, 39.0
        cfg = intersect_config(cross_track_dbscan_min_samples=1)

        for seed in range(20):
            rng = random.Random(seed)
            headings = {
                "A": 0.0,
                "B": 90.0,
                "C": 80.0,
                "D": 100.0 if seed % 2 else float("nan"),
            }
            tracks: dict[str, list[TrajectoryPoint]] = {}
            for vehicle_id, heading in headings.items():
                track: list[TrajectoryPoint] = []
                for point_index in range(4):
                    if vehicle_id == "B" and point_index == 1 and seed % 3 == 0:
                        x, y = _to_xy(track[-1].lon, track[-1].lat, origin_lon, origin_lat)
                    else:
                        x = rng.choice((-300.0, -100.0, 0.0, 100.0, 300.0)) + rng.uniform(-30.0, 30.0)
                        y = rng.choice((-300.0, -100.0, 0.0, 100.0, 300.0)) + rng.uniform(-30.0, 30.0)
                    lon, lat = _to_lonlat(x, y, origin_lon, origin_lat)
                    track.append(_make_point(
                        vehicle_id, f"{vehicle_id}{seed}-{point_index}", float(point_index),
                        lon, lat, heading, 8.0,
                    ))
                tracks[vehicle_id] = track

            with np.errstate(all="ignore"), patch("builtins.print"):
                legacy = _extract_orthogonal_cross_track_candidates_legacy(
                    tracks, origin_lon, origin_lat, cfg,
                )
                bounded = _extract_orthogonal_cross_track_candidates(
                    tracks, _TEST_TRANSFORMER, cfg,
                )
            self.assertEqual(legacy[1:], bounded[1:], f"seed={seed}")
            self.assertEqual(_candidate_keys(legacy[0]), _candidate_keys(bounded[0]), f"seed={seed}")
            for candidate in bounded[0]:
                expected_x, expected_y = _TEST_TRANSFORMER.forward(
                    candidate.lon, candidate.lat
                )
                self.assertAlmostEqual(candidate.x, expected_x, places=6)
                self.assertAlmostEqual(candidate.y, expected_y, places=6)

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
        tracks = _with_processing_xy({"v1": points})
        center_x, center_y = _TEST_TRANSFORMER.forward(origin_lon, origin_lat)

        cluster = HotspotCluster(
            cluster_calculate_index=0,
            centroid_x=center_x, centroid_y=center_y,
            centroid_lon=origin_lon, centroid_lat=origin_lat,
            radius_m=50.0,
            events=(),
            source="turn",
        )
        observations, crossing_points = _extract_entry_exit_observations_batch(
            tracks, cluster=cluster,
            transformer=_TEST_TRANSFORMER,
            radius_m=150.0, bearing_window=2,
        )
        self.assertGreaterEqual(len(observations), 1,
                                "Vehicle passing through circle should create observation")
        self.assertTrue(crossing_points)
        for point in crossing_points:
            roundtrip_x, roundtrip_y = _TEST_TRANSFORMER.forward(
                point.crossing_lon, point.crossing_lat
            )
            self.assertAlmostEqual(point.crossing_x, roundtrip_x, places=6)
            self.assertAlmostEqual(point.crossing_y, roundtrip_y, places=6)

    def test_global_segments_route_complete_subtracks_to_topology_query_boxes(self):
        """A segment hit routes its complete, originally ordered subtrack."""
        cfg = intersect_config(
            min_intersection_radius_m=100.0,
            topology_radius_padding_m=0.0,
        )
        clusters = [
            HotspotCluster(
                cluster_calculate_index=index,
                centroid_x=_TEST_TRANSFORMER.forward(lon, 0.0)[0],
                centroid_y=_TEST_TRANSFORMER.forward(lon, 0.0)[1],
                centroid_lon=lon,
                centroid_lat=0.0,
                radius_m=1.0,
                events=(),
                source="turn",
            )
            for index, lon in enumerate((0.0, 0.01))
        ]
        lons = (-0.002, -0.001, 0.0, 0.001, 0.0085, 0.01, 0.012)
        track = _with_processing_xy({"v1": [
            _make_point("v1", str(index), float(index), lon, 0.0, 0.0, 5.0)
            for index, lon in enumerate(lons)
        ]})["v1"]

        windows = _topology_point_windows(clusters, cfg)
        point_index = _build_topology_point_index({"v1": track})
        routed = [point_index.tracks_in(window) for window in windows]

        self.assertEqual(len(point_index.segments), len(track) - 1)
        self.assertEqual(len(point_index.lower_bounds), len(track) - 1)
        self.assertEqual(len(point_index.prefix_upper_bounds), len(track) - 1)
        self.assertEqual(
            [point.point_ref.source_row_key for point in routed[0]["v1"]],
            [str(index) for index in range(len(track))],
        )
        self.assertEqual(
            [point.point_ref.source_row_key for point in routed[1]["v1"]],
            [str(index) for index in range(len(track))],
        )

        tie_track = _with_processing_xy({"tie": [
            _make_point("tie", "stream-first", 1.0, 0.001, 0.0, 0.0, 5.0),
            _make_point("tie", "stream-second", 1.0, -0.001, 0.0, 0.0, 5.0),
        ]})["tie"]
        tie_index = _build_topology_point_index({"tie": tie_track})
        self.assertEqual(
            [
                point.point_ref.source_row_key
                for point in tie_index.tracks_in(windows[0])["tie"]
            ],
            ["stream-first", "stream-second"],
        )

    def test_topology_from_tracks_extracts_complete_routed_subtrack(self):
        cfg = intersect_config(
            min_intersection_radius_m=100.0,
            topology_radius_padding_m=0.0,
        )
        cluster = HotspotCluster(
            cluster_calculate_index=0,
            centroid_x=0.0,
            centroid_y=0.0,
            centroid_lon=0.0,
            centroid_lat=0.0,
            radius_m=1.0,
            events=(),
            source="turn",
        )
        track = _with_processing_xy({"v1": [
            _make_point("v1", "outside", 0.0, -0.01, 0.0, 0.0, 5.0),
            _make_point("v1", "inside-a", 1.0, -0.001, 0.0, 0.0, 5.0),
            _make_point("v1", "inside-b", 2.0, 0.001, 0.0, 0.0, 5.0),
        ]})["v1"]

        with patch(
            "track.v2.detect._extract_entry_exit_observations_batch",
            return_value=([], []),
        ) as extract:
            topologies, annotated, observations = _build_topologies_from_tracks(
                [cluster], {"v1": track}, cfg, _TEST_TRANSFORMER,
            )

        routed_track = extract.call_args.args[0]["v1"]
        self.assertEqual(
            [point.point_ref.source_row_key for point in routed_track],
            ["outside", "inside-a", "inside-b"],
        )
        self.assertEqual(len(topologies), 1)
        self.assertEqual(len(annotated), 1)
        self.assertEqual(observations, [])

    def test_reader_topology_matches_index_for_duplicate_windows_and_track_order(self):
        cfg = intersect_run_config(
            source_table="source", min_intersection_radius_m=50.0,
            topology_radius_padding_m=0.0, bearing_window=1,
        )
        cluster = HotspotCluster(
            cluster_calculate_index=0, centroid_x=0.0, centroid_y=0.0,
            centroid_lon=0.0, centroid_lat=0.0, radius_m=1.0,
            events=(), source="turn",
        )
        clusters = [cluster, replace(cluster, cluster_calculate_index=1)]
        tracks = {
            key: [
                _make_xy_refined_point(vehicle, point_id, float(point_id), x, 0.0,
                                       subtrack_index=subtrack,
                                       sequence_in_subtrack=sequence)
                for sequence, (point_id, x) in enumerate(((base, -250.0), (base + 1, 250.0)))
            ]
            for key, vehicle, subtrack, base in (
                ("v\x0010", "v", 10, 10), ("v\x002", "v", 2, 20),
            )
        }
        expected = _build_topologies_from_tracks(
            clusters, tracks, cfg, _TEST_TRANSFORMER,
        )
        reader = MagicMock()
        reader.iter_topology_tracks.return_value = iter(
            TopologyTrackSelection((0, 1), key, tracks[key]) for key in sorted(tracks)
        )

        actual = _build_topologies_from_tracks(
            clusters, {}, cfg, _TEST_TRANSFORMER, refine_reader=reader,
        )

        self.assertEqual(actual, expected)
        self.assertEqual(
            [[point.crossing_calculate_index for point in item.crossing_points]
             for item in actual[1]],
            [[0, 1, 2, 3], [0, 1, 2, 3]],
        )
        reader.iter_topology_tracks.assert_called_once_with(
            "source", _topology_point_windows(clusters, cfg),
            batch_size=cfg.stream_batch_size,
        )

    def test_formal_reader_owner_never_constructs_or_populates_workspace(self):
        reader = MagicMock()
        reader.extent.return_value = (0.0, 0.0, 1.0, 1.0)
        reader.iter_turn_candidates.return_value = iter(())
        reader.iter_topology_tracks.return_value = (_ for _ in ())
        reader.track_count.return_value = 7
        cfg = intersect_run_config(
            source_table="source", use_turn_events=False,
            use_cross_track_candidates=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = _detect_intersections_db(
                object(), cfg, _TEST_SPATIAL_CONTEXT,
                runtime=runtime_config(), data_root=directory, refine_reader=reader,
            )
        try:
            self.assertEqual(result.report.track_count, 7)
            reader.iter_tracks.assert_not_called()
        finally:
            result.close()

    def test_reader_topology_closes_selection_iterator_on_geometry_failure(self):
        cfg = intersect_run_config(min_intersection_radius_m=50.0)
        cluster = HotspotCluster(
            cluster_calculate_index=0, centroid_x=0.0, centroid_y=0.0,
            centroid_lon=0.0, centroid_lat=0.0, radius_m=1.0,
            events=(), source="turn",
        )
        track = [_make_xy_refined_point(
            "v", 1, 0.0, -1.0, 0.0, subtrack_index=1,
            sequence_in_subtrack=0,
        )]
        closed = False

        def selections():
            nonlocal closed
            try:
                yield TopologyTrackSelection((0,), "v\x001", track)
            finally:
                closed = True

        reader = MagicMock()
        reader.iter_topology_tracks.return_value = selections()
        with patch(
            "track.v2.detect._extract_entry_exit_observations_batch",
            side_effect=KeyboardInterrupt,
        ), self.assertRaises(KeyboardInterrupt):
            _build_topologies_from_tracks(
                [cluster], {}, cfg, _TEST_TRANSFORMER, refine_reader=reader,
            )
        self.assertTrue(closed)

    def test_segments_with_outer_endpoints_still_produce_entry_and_exit(self):
        """Each boundary segment remains real when its outer point misses the window."""
        cfg = intersect_config(
            min_intersection_radius_m=50.0,
            topology_radius_padding_m=0.0,
            bearing_window=1,
        )
        cluster = HotspotCluster(
            cluster_calculate_index=0,
            centroid_x=0.0,
            centroid_y=0.0,
            centroid_lon=0.0,
            centroid_lat=0.0,
            radius_m=1.0,
            events=(),
            source="turn",
        )
        track = [
            _make_xy_refined_point(
                "v1", 1, 0.0, -250.0, 0.0,
                subtrack_index=1, sequence_in_subtrack=0,
            ),
            _make_xy_refined_point(
                "v1", 2, 1.0, 0.0, 0.0,
                subtrack_index=1, sequence_in_subtrack=1,
            ),
            _make_xy_refined_point(
                "v1", 3, 2.0, 250.0, 0.0,
                subtrack_index=1, sequence_in_subtrack=2,
            ),
        ]

        _topologies, annotated, observations = _build_topologies_from_tracks(
            [cluster], {"v1\x001": track}, cfg, _TEST_TRANSFORMER,
        )

        crossings = annotated[0].crossing_points
        self.assertEqual([crossing.is_entry for crossing in crossings], [True, False])
        self.assertEqual(len(observations), 1)
        self.assertEqual(
            [crossing.segment_start.point_id for crossing in crossings],
            [1, 2],
        )

    def test_same_vehicle_subtracks_do_not_form_a_synthetic_segment(self):
        """Window routing retains track keys instead of regrouping by vehicle."""
        cfg = intersect_config(
            min_intersection_radius_m=50.0,
            topology_radius_padding_m=0.0,
            bearing_window=1,
        )
        cluster = HotspotCluster(
            cluster_calculate_index=0,
            centroid_x=0.0,
            centroid_y=0.0,
            centroid_lon=0.0,
            centroid_lat=0.0,
            radius_m=1.0,
            events=(),
            source="turn",
        )
        tracks = {
            "v1\x001": [
                _make_xy_refined_point(
                    "v1", 1, 0.0, -90.0, 0.0,
                    subtrack_index=1, sequence_in_subtrack=0,
                ),
                _make_xy_refined_point(
                    "v1", 2, 1.0, -80.0, 0.0,
                    subtrack_index=1, sequence_in_subtrack=1,
                ),
            ],
            "v1\x002": [
                _make_xy_refined_point(
                    "v1", 3, 2.0, 80.0, 0.0,
                    subtrack_index=2, sequence_in_subtrack=0,
                ),
                _make_xy_refined_point(
                    "v1", 4, 3.0, 90.0, 0.0,
                    subtrack_index=2, sequence_in_subtrack=1,
                ),
            ],
        }

        point_index = _build_topology_point_index(tracks)
        routed = point_index.tracks_in(_topology_point_windows([cluster], cfg)[0])
        self.assertEqual(list(routed), ["v1\x001", "v1\x002"])

        _topologies, annotated, observations = _build_topologies_from_tracks(
            [cluster], tracks, cfg, _TEST_TRANSFORMER,
        )
        self.assertEqual(annotated[0].crossing_points, ())
        self.assertEqual(observations, [])

    def test_long_segment_crosses_circle_when_both_endpoints_miss_window(self):
        """AABB routing retains a two-root segment with no endpoint in the box."""
        cfg = intersect_config(
            min_intersection_radius_m=50.0,
            topology_radius_padding_m=0.0,
            bearing_window=1,
        )
        cluster = HotspotCluster(
            cluster_calculate_index=0,
            centroid_x=0.0,
            centroid_y=0.0,
            centroid_lon=0.0,
            centroid_lat=0.0,
            radius_m=1.0,
            events=(),
            source="turn",
        )
        track = [
            _make_xy_refined_point(
                "v1", 1, 0.0, -250.0, 0.0,
                subtrack_index=1, sequence_in_subtrack=0,
            ),
            _make_xy_refined_point(
                "v1", 2, 1.0, 250.0, 0.0,
                subtrack_index=1, sequence_in_subtrack=1,
            ),
        ]

        _topologies, annotated, observations = _build_topologies_from_tracks(
            [cluster], {"v1\x001": track}, cfg, _TEST_TRANSFORMER,
        )

        crossings = annotated[0].crossing_points
        self.assertEqual([crossing.is_entry for crossing in crossings], [True, False])
        self.assertEqual(len(observations), 1)
        self.assertEqual(
            [crossing.segment_start.point_id for crossing in crossings],
            [1, 1],
        )

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
            cluster_calculate_index=0,
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

    def test_db_mode_routes_topology_through_columnar_reader(self):
        point = _with_processing_xy({
            "v1": [_make_point("v1", "1", 1.0, 116.0, 39.0, 0.0, 5.0)]
        })["v1"][0]
        cluster = HotspotCluster(
            cluster_calculate_index=0,
            centroid_x=point.x,
            centroid_y=point.y,
            centroid_lon=116.0,
            centroid_lat=39.0,
            radius_m=1.0,
            events=(),
            source="turn",
        )
        topology = IntersectionTopology(
            cluster_calculate_index=0,
            branch_centers_deg=[],
            transition_counts={},
            observation_count=0,
            is_complete=False,
            reason="test",
        )

        reader = MagicMock()
        reader.descriptor = SimpleNamespace(
            refine_id="refine-1", source_fingerprint_json='{"version":1}',
        )
        reader.extent.return_value = (115.0, 38.0, 117.0, 40.0)
        reader.iter_turn_candidates.return_value = iter(())
        reader.track_count.return_value = 1
        with (
            patch(
                "track.v2.detect.fetch_refine_run_row",
                return_value=SimpleNamespace(
                    refine_id="refine-1",
                    source_table="trajectory_points",
                    status="completed",
                    spatial_context=_TEST_SPATIAL_CONTEXT,
                    source_fingerprint={"version": 1},
                ),
            ),
            patch(
                "track.v2.detect._extract_cross_track_db",
                return_value=[],
            ),
            patch(
                "track.v2.detect._cluster_pipeline",
                return_value=([], [cluster]),
            ),
            patch(
                "track.v2.detect._build_topologies_from_tracks",
                return_value=([topology], [cluster], []),
            ) as build_topologies,
        ):
            result = detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                runtime=runtime_config(), data_root=Path(tempfile.mkdtemp()),
                refine_reader=reader,
            )

        self.assertEqual(build_topologies.call_args.args[1], {})
        self.assertIs(build_topologies.call_args.kwargs["refine_reader"], reader)
        self.assertEqual(result.topologies, [topology])


# ── Cluster merge tests ────────────────────────────────────────────────


class ClusterMergeTest(unittest.TestCase):
    """I2-22: Cluster merging from both methods."""

    def test_merge_overlapping_clusters(self):
        """Overlapping clusters from both methods are merged."""
        origin_lon, origin_lat = 116.0, 39.0
        c1 = HotspotCluster(
            cluster_calculate_index=0, centroid_x=0.0, centroid_y=0.0,
            centroid_lon=origin_lon, centroid_lat=origin_lat,
            radius_m=50.0,
            events=(TurnEvent(0, "v1", 1000, origin_lon, origin_lat,
                              0, 0, 0, 90, 90, 90),),
            source="turn",
            source_point_refs={"turn": (PointRef("raw", "1", stable=True),)},
        )
        c2 = HotspotCluster(
            cluster_calculate_index=0, centroid_x=20.0, centroid_y=20.0,
            centroid_lon=origin_lon, centroid_lat=origin_lat,
            radius_m=30.0,
            events=(TurnEvent(1, "v2", 1001, origin_lon, origin_lat,
                              20, 20, 0, 90, 90, 90),
                    TurnEvent(2, "v3", 1002, origin_lon, origin_lat,
                              25, 25, 0, 90, 90, 90)),
            source="orthogonal_cross_track",
            source_point_refs={
                "orthogonal_cross_track": (
                    PointRef("raw", "2", stable=True),
                    PointRef("raw", "3", stable=True),
                ),
            },
        )
        merged = _merge_clusters(
            [c1, c2], merge_distance_m=80.0, transformer=_TEST_TRANSFORMER
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source, "merged")
        self.assertEqual(
            merged[0].algorithm_sources,
            ("orthogonal_cross_track", "turn"),
        )
        self.assertEqual(merged[0].source_counts["turn"], 1)
        self.assertEqual(merged[0].source_counts["orthogonal_cross_track"], 2)
        self.assertAlmostEqual(merged[0].source_ratios["orthogonal_cross_track"], 2 / 3)
        self.assertEqual(
            [ref.source_row_key for ref in merged[0].source_point_refs["orthogonal_cross_track"]],
            ["2", "3"],
        )

    def test_no_merge_distant_clusters(self):
        """Distant clusters are not merged."""
        origin_lon, origin_lat = 116.0, 39.0
        c1 = HotspotCluster(
            cluster_calculate_index=0, centroid_x=0.0, centroid_y=0.0,
            centroid_lon=116.0, centroid_lat=39.0,
            radius_m=50.0, events=(), source="turn",
        )
        c2 = HotspotCluster(
            cluster_calculate_index=1, centroid_x=5000.0, centroid_y=5000.0,
            centroid_lon=117.0, centroid_lat=40.0,
            radius_m=50.0, events=(), source="orthogonal_cross_track",
        )
        merged = _merge_clusters(
            [c1, c2], merge_distance_m=80.0, transformer=_TEST_TRANSFORMER
        )
        self.assertEqual(len(merged), 2)


# ── End-to-end integration tests ───────────────────────────────────────

class SourceAblationTest(unittest.TestCase):
    """Source contribution and leave-one-source-out validation."""

    def test_single_source_cluster_marks_source_as_critical(self):
        origin_lon, origin_lat = 116.0, 39.0
        events = [
            TurnEvent(i, f"v{i}", 1000 + i, origin_lon, origin_lat,
                      i * 2.0, i * 2.0, 0, 90, 90, 90)
            for i in range(4)
        ]
        cfg = intersect_config(dbscan_eps_m_turn=100.0, dbscan_min_samples_turn=3)
        clusters = _cluster_dbscan(
            events,
            _TEST_TRANSFORMER,
            eps_m=cfg.dbscan_eps_m_turn,
            min_samples=cfg.dbscan_min_samples_turn,
        )

        annotated = _annotate_source_ablation(
            clusters,
            events,
            [],
            cfg,
            _TEST_TRANSFORMER,
        )

        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0].algorithm_sources, ("turn",))
        self.assertEqual(annotated[0].critical_sources, ("turn",))

    def test_cross_track_source_is_reclustered_for_ablation(self):
        origin_lon, origin_lat = 116.0, 39.0
        base_x, base_y = _TEST_TRANSFORMER.forward(origin_lon, origin_lat)
        candidates: list[CrossTrackCandidate] = []
        for index in range(3):
            x = base_x + index
            y = base_y + index
            lon, lat = _TEST_TRANSFORMER.inverse(x, y)
            candidates.append(CrossTrackCandidate(
                point_ref=PointRef("public.day01", str(index), stable=True),
                from_segment_start_point_ref=PointRef(
                    "public.day01", f"a{index}", stable=True,
                ),
                to_segment_start_point_ref=PointRef(
                    "public.day02", f"b{index}", stable=True,
                ),
                vehicle_id=f"A|B-{index}",
                timestamp=float(index),
                lon=lon,
                lat=lat,
                x=x,
                y=y,
                heading=0.0,
                speed=10.0,
            ))
        cfg = intersect_config(
            cross_track_dbscan_eps_m=100.0,
            cross_track_dbscan_min_samples=2,
        )
        clusters = _cluster_orthogonal_cross_track_candidates(
            candidates,
            _TEST_TRANSFORMER,
            eps_m=cfg.cross_track_dbscan_eps_m,
            min_samples=cfg.cross_track_dbscan_min_samples,
        )

        annotated = _annotate_source_ablation(
            clusters, [], candidates, cfg, _TEST_TRANSFORMER,
        )

        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0].algorithm_sources, ("orthogonal_cross_track",))
        self.assertEqual(annotated[0].critical_sources, ("orthogonal_cross_track",))
        self.assertEqual(set(annotated[0].source_ratios), {"orthogonal_cross_track"})


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
            handle = get_database(
                source, field_config=pipeline_config().parser.fields,
                runtime=runtime_config(),
            )
            result = detect_intersections(handle, config=intersect_config(
                use_turn_events=True,
                use_cross_track_candidates=False,  # Only Method 1 for this test
                turn_window=2,
                min_turn_deg=45.0, max_turn_deg=170.0,
                min_leg_m=20.0, min_movement_speed_mps=1.0,
                g_star_radius_m=500.0, g_star_min_z=-1.0,  # lenient to catch all
                dbscan_eps_m_turn=500.0, dbscan_min_samples_turn=2,
                min_branches=2, min_observations=2, min_directed_transitions=2,
            ), context=detect_context(), runtime=runtime_config(),
                spatial_context=_TEST_SPATIAL_CONTEXT)
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
            handle = get_database(
                source, field_config=pipeline_config().parser.fields,
                runtime=runtime_config(),
            )
            result = detect_intersections(
                handle, config=intersect_config(), context=detect_context(),
                runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
            )
            handle.close()

            self.assertEqual(len(result.turn_events), 0,
                             "Straight lines should produce 0 turn events")
            self.assertEqual(len(result.clusters), 0)

    def test_handle_mode_exposes_cross_track_candidates(self):
        """Public result retains deduplicated cross-track candidates."""
        with tempfile.TemporaryDirectory() as td:
            rows = [
                "vid,lng,lat,included_angle,gps_speed,gps_time_a",
                "A,116.0,38.999,0,12,10",
                "A,116.0,39.001,0,12,20",
                "B,115.999,39.0,90,8,10",
                "B,116.001,39.0,90,8,20",
            ]
            csv_path = _make_csv(td, "cross_track.csv", "\n".join(rows) + "\n")
            handle = get_database(
                CsvSource(csv_path), field_config=pipeline_config().parser.fields,
                runtime=runtime_config(),
            )
            result = detect_intersections(
                handle,
                config=intersect_config(
                    use_turn_events=False,
                    cross_track_dbscan_min_samples=1,
                ),
                context=detect_context(),
                runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
            )
            handle.close()

        self.assertEqual(len(result.cross_track_candidates), 1)
        self.assertEqual(result.cross_track_candidates[0].vehicle_id, "A|B")

    def test_real_csv_smoke(self):
        """I2-25: Smoke test — detect_intersections on Hfm_130pts.csv."""
        import os
        hfm_path = Path(__file__).resolve().parents[2] / "track" / "data" / "Hfm_130pts.csv"
        if not hfm_path.exists():
            self.skipTest(f"Hfm_130pts.csv not found at {hfm_path}")

        source = CsvSource(hfm_path)
        handle = get_database(
            source, field_config=pipeline_config().parser.fields,
            runtime=runtime_config(),
        )
        # Run with default config
        result = detect_intersections(
            handle, config=intersect_config(), context=detect_context(),
            runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
        )
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
