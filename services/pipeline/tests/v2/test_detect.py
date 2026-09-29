"""Detect array geometry and public stage behavior."""
import math
import random
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from .config_factory import intersect_config,intersect_run_config,detect_context,runtime_config
from .detect_columns_helpers import CONTEXT as _TEST_SPATIAL_CONTEXT,TRANSFORMER as _TEST_TRANSFORMER,subtrack,sql_turns_from_subtrack
from track.v2._geometry import _batch_segment_intersections,_iter_bounded_segment_intersections,_paired_segment_intersections
from track.v2.candidates.build_patches import _iter_pair_batches as _iter_cross_track_pair_batches
from track.v2.candidates.collect_candidates import extract_cross_track_candidates
from track.v2.candidates.detect_turn_events import empty_turn_batch
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.intersections.performance_timing import PerfTracker
from track.v2.intersections.detect_intersections import (_build_dbscan_grid,_run_dbscan,_cluster_pipeline,_compute_getis_ord_g_star,detect_intersections,_detect_intersections_handle,_cluster_dbscan,_merge_clusters)
from track.v2.intersections.extract_topology import _cluster_angles_greedy,_nearest_angle_id,_build_topologies_from_tracks
from track.v2.exceptions import V2Error
from track.v2.models import IntersectReport
class PairedSegmentIntersectionTest(unittest.TestCase):

    def test_exact_intersection_uses_matching_pairs_without_cartesian_product(self):
        a1 = np.array([[0.0, -1.0], [10.0, -1.0]])
        a2 = np.array([[0.0, 1.0], [10.0, 1.0]])
        b1 = np.array([[-1.0, 0.0], [9.0, 2.0]])
        b2 = np.array([[1.0, 0.0], [11.0, 2.0]])
        ha = np.array([0.0, 0.0])
        hb = np.array([90.0, 90.0])
        (xs, ys, hit_indices) = _paired_segment_intersections(a1, a2, ha, b1, b2, hb, 80.0, 100.0)
        self.assertEqual(hit_indices.tolist(), [0])
        self.assertEqual(xs.tolist(), [0.0])
        self.assertEqual(ys.tolist(), [0.0])

    def test_pair_batches_never_exceed_the_configured_limit(self):
        """Dense cells remain memory bounded before exact geometry runs."""
        pair_batch_size = intersect_config().cross_track_pair_batch_size
        side = math.isqrt(pair_batch_size) + 1
        batches = list(_iter_cross_track_pair_batches(list(range(side)), list(range(side)), pair_batch_size))
        self.assertEqual(sum((len(a_ids) for (a_ids, _) in batches)), side * side)
        self.assertTrue(all((len(a_ids) <= pair_batch_size for (a_ids, _) in batches)))
        self.assertEqual(batches[0][0][0], 0)
        self.assertEqual(batches[-1][1][-1], side - 1)

class CompactAabbBroadPhaseTest(unittest.TestCase):

    def test_aabb_rejection_skips_exact_pairs_and_keeps_closed_endpoint(self):
        a1 = np.array([[0.0, 0.0], [10.0, 0.0]])
        a2 = np.array([[1.0, 0.0], [11.0, 0.0]])
        ha = np.array([90.0, 90.0])
        b1 = np.array([[10.0, -1.0], [30.0, -1.0]])
        b2 = np.array([[10.0, 1.0], [30.0, 1.0]])
        hb = np.array([0.0, 0.0])
        telemetry: dict[str, int] = {}
        blocks = list(_iter_bounded_segment_intersections(a1, a2, ha, b1, b2, hb, 80.0, 100.0, max_matrix_elements=2, telemetry=telemetry))
        self.assertEqual(telemetry['broad_pairs'], 1)
        self.assertEqual(telemetry['exact_pairs'], 1)
        self.assertEqual(telemetry['exact_skipped_pairs'], 3)
        self.assertEqual(telemetry['empty_broad_blocks'], 1)
        self.assertEqual(len(blocks), 1)
        (a_offset, cx, cy, flat_idx) = blocks[0]
        self.assertEqual(a_offset, 1)
        self.assertEqual(flat_idx.tolist(), [0])
        self.assertEqual(cx.tolist(), [10.0])
        self.assertEqual(cy.tolist(), [0.0])

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
        (dense_x, dense_y, dense_hits) = _batch_segment_intersections(a1, a2, ha, b1, b2, hb, 80.0, 100.0)
        bounded_hits: list[int] = []
        bounded_xy: list[tuple[float, float]] = []
        for (a_offset, cx, cy, flat_idx) in _iter_bounded_segment_intersections(a1, a2, ha, b1, b2, hb, 80.0, 100.0, max_matrix_elements=cap, telemetry=telemetry):
            bounded_hits.extend((a_offset * len(b1) + flat_idx).tolist())
            bounded_xy.extend(zip(cx.tolist(), cy.tolist()))
        self.assertEqual(bounded_hits, dense_hits.tolist())
        self.assertEqual(bounded_xy, [(float(dense_x.flat[i]), float(dense_y.flat[i])) for i in dense_hits])
        self.assertEqual(telemetry['matrix_element_cap'], cap)
        self.assertLessEqual(telemetry['max_broad_block_pairs'], cap)
        self.assertLessEqual(telemetry['max_exact_chunk_pairs'], cap)
        self.assertEqual(telemetry['broad_pairs'], len(a1) * len(b1))
        self.assertEqual(telemetry['exact_pairs'], len(a1) * len(b1))
        self.assertGreater(telemetry['broad_blocks'], len(a1))

    def test_dense_direct_never_exceeds_smaller_matrix_cap(self):
        a1 = np.array([[0.0, 0.0]])
        a2 = np.array([[2.0, 0.0]])
        ha = np.array([90.0])
        b1 = np.array([[1.0, -1.0]] * 64)
        b2 = np.array([[1.0, 1.0]] * 64)
        telemetry: dict[str, int] = {}
        list(_iter_bounded_segment_intersections(a1, a2, ha, b1, b2, np.zeros(64), 80.0, 100.0, max_matrix_elements=32, telemetry=telemetry))
        self.assertNotIn('dense_direct_blocks', telemetry)
        self.assertLessEqual(telemetry['max_broad_block_pairs'], 32)
        self.assertLessEqual(telemetry['max_exact_chunk_pairs'], 32)

class DBSCANTest(unittest.TestCase):

    def test_seed_neighbor_telemetry_is_block_level(self):
        tracker = PerfTracker('dbscan-block-test')
        count = 260
        labels = _run_dbscan([0.0] * count, [0.0] * count, eps_m=1.0, min_samples=2, perf=tracker)
        self.assertEqual(labels, [0] * count)
        node = tracker._root.children['dbscan_points'].children['dbscan_seed_neighbors']
        self.assertLessEqual(node.call_count, count)

    def test_many_disconnected_clusters_do_not_require_cluster_sized_resets(self):
        """Regression shape for per-cluster queue clearing: 400 singleton clusters."""
        xs = [float(index * 10) for index in range(400)]
        self.assertEqual(_run_dbscan(xs, [0.0] * len(xs), eps_m=0.1, min_samples=1), list(range(400)))

    def test_dense_grid_enqueues_each_point_at_most_once(self):
        """Dense clusters do not re-materialise N neighbours for every core."""
        from track.v2.intersections.detect_intersections import _take_grid_frontier_neighbors
        taken = 0

        def observe(*args):
            nonlocal taken
            result = _take_grid_frontier_neighbors(*args)
            taken += len(result)
            return result
        count = 2000
        with patch('track.v2.intersections.detect_intersections._take_grid_frontier_neighbors', side_effect=observe):
            labels = _run_dbscan([0.0] * count, [0.0] * count, eps_m=1.0, min_samples=3)
        self.assertEqual(labels, [0] * count)
        self.assertLessEqual(taken, count)

    def test_low_index_noise_is_later_absorbed_as_border(self):
        """A point marked noise before its adjacent core remains available."""
        xs = [0.0, 0.9, 1.7, 1.5, 1.5]
        ys = [0.0, 0.0, 0.0, 0.6, -0.6]
        xs.extend((100.0 + index * 10.0 for index in range(252)))
        ys.extend([100.0] * 252)
        expected = [0] * 5 + [-1] * 252
        actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=4)
        self.assertEqual(expected[0], 0)
        self.assertEqual(actual, expected)

    def test_shared_border_keeps_first_discovered_component(self):
        """A border shared by two core components keeps legacy first reach."""
        xs = [0.0, 0.9, 1.7, 1.5, 1.5, -0.9, -1.7, -1.5, -1.5]
        ys = [0.0, 0.0, 0.0, 0.6, -0.6, 0.0, 0.0, 0.6, -0.6]
        xs.extend((100.0 + index * 10.0 for index in range(248)))
        ys.extend([100.0] * 248)
        expected = [0] * 5 + [1] * 4 + [-1] * 248
        actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=4)
        self.assertEqual(expected[0], expected[1])
        self.assertNotEqual(expected[0], expected[5])
        self.assertEqual(actual, expected)

    def test_large_finite_coordinates_fall_back_before_unbounded_cell_range(self):
        xs = [1e+308, math.nextafter(1e+308, -math.inf)] + [float(i * 10) for i in range(255)]
        ys = [0.0] * len(xs)
        expected = list(range(len(xs)))
        build_results = []

        def observe_build(*args):
            value = _build_dbscan_grid(*args)
            build_results.append(value)
            return value
        with patch('track.v2.intersections.detect_intersections._build_dbscan_grid', side_effect=observe_build) as build_grid:
            actual = _run_dbscan(xs, ys, eps_m=1.0, min_samples=1)
        build_grid.assert_called_once_with(xs, ys, 1.0)
        self.assertEqual(build_results, [None])
        self.assertEqual(actual, expected)
@pytest.mark.parametrize("headings,expected",[([0,90,90,90],1),([0,0,0,0],0),([0,170,170,170],0)])
def test_turn_filters_from_heading_window(headings,expected):
    track=subtrack([[0,0],[0,20],[20,20],[40,20]],headings=headings)
    result=sql_turns_from_subtrack(track,intersect_config(turn_window=1,min_leg_m=1,max_turn_deg=150))
    assert len(result)==expected

@pytest.mark.parametrize("minimum,maximum,expected",[(90,90,1),(91,150,0),(1,89,0)])
def test_turn_angle_endpoints_are_inclusive(minimum,maximum,expected):
    track=subtrack([[0,0],[0,20],[20,20]],headings=[0,90,90])
    assert len(sql_turns_from_subtrack(track,intersect_config(turn_window=1,min_leg_m=1,min_turn_deg=minimum,max_turn_deg=maximum)))==expected

@pytest.mark.parametrize("elapsed,leg,expected",[(1,20,1),(0,20,0),(1,.1,0)])
def test_turn_movement_filters(elapsed,leg,expected):
    track=subtrack([[0,0],[0,leg],[leg,leg]],headings=[0,90,90])
    track=replace(track,timestamp=np.asarray([0,elapsed,2*elapsed],dtype=np.float64))
    assert len(sql_turns_from_subtrack(track,intersect_config(turn_window=1,min_leg_m=1)))==expected

def test_getis_star_keeps_noncontiguous_calculation_identity():
    batch=sql_turns_from_subtrack(subtrack([[0,0],[0,20],[20,20]],headings=[0,90,90]),intersect_config(turn_window=1,min_leg_m=1),first_index=9001)
    assert _compute_getis_ord_g_star(batch,5)=={9001:0.0}

@pytest.mark.parametrize("count",[3,300])
def test_dbscan_disconnected_points_are_noise(count):
    assert _run_dbscan(np.arange(count,dtype=np.float64)*10,np.zeros(count),eps_m=1,min_samples=2)==[-1]*count

@pytest.mark.parametrize("count",[3,300])
def test_dbscan_coincident_points_are_one_cluster(count):
    assert _run_dbscan(np.zeros(count),np.zeros(count),eps_m=1,min_samples=2)==[0]*count

def test_branch_angles_wrap_and_nearest():
    assert len(_cluster_angles_greedy([359,1,90,180,270],5))==4
    assert _nearest_angle_id(359,[0,90,180])==0

def test_public_detect_eager_preserves_report_and_identity():
    a=subtrack([[-20,0],[20,0]],subtrack_index=123,headings=[90,90])
    b=subtrack([[0,-20],[0,20]],subtrack_index=999,headings=[0,0])
    cfg=intersect_run_config(use_turn_events=False,min_intersection_radius_m=10,topology_radius_padding_m=0,candidate_dbscan_min_samples=1,min_observations=1,min_branches=2,min_directed_transitions=1)
    result=_detect_intersections_handle({123:a,999:b},cfg,_TEST_SPATIAL_CONTEXT)
    assert result.report.track_count==2
    assert result.report.cross_track_candidate_count==1
    assert result.cross_track_candidates.from_subtrack_index.tolist()==[123]
    assert result.cross_track_candidates.to_subtrack_index.tolist()==[999]
    assert result.clusters[0].members.kind.tolist()==["orthogonal_cross_track"]
    assert result.clusters[0].crossing_points.calculation_index.tolist()==[0,1,2,3]
    assert result.report.complete_intersection_count==1

def test_public_handle_requires_spatial_context():
    with pytest.raises(V2Error,match="SpatialContext"):
        detect_intersections({},config=intersect_config(),context=detect_context(query_active_from_db=False),runtime=runtime_config())


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


@pytest.mark.parametrize("pairs,direct",[(1023,True),(1024,True),(1025,False)])
def test_hybrid_threshold_boundary_preserves_known_hits_order_xy_and_cap(pairs,direct):
    telemetry={}
    blocks=list(_iter_bounded_segment_intersections(np.array([[0.,0.]]),np.array([[2.,0.]]),np.array([90.]),np.tile([1.,-1.],(pairs,1)),np.tile([1.,1.],(pairs,1)),np.zeros(pairs),80.,100.,max_matrix_elements=262144,telemetry=telemetry))
    assert [int(index) for offset,x,y,indices in blocks for index in offset+indices]==list(range(pairs))
    assert all(np.array_equal(x,np.ones(len(x))) and np.array_equal(y,np.zeros(len(y))) for offset,x,y,indices in blocks)
    if direct:
        assert telemetry['dense_direct_blocks']==1
        assert telemetry['max_dense_direct_pairs']==pairs
        assert 'broad_blocks' not in telemetry
    else:
        assert telemetry['broad_blocks']==1
        assert telemetry['max_broad_block_pairs']<=262144
        assert telemetry['max_exact_chunk_pairs']<=262144
