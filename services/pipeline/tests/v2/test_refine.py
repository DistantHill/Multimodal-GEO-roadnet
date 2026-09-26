"""Refine rules validated directly on complete source columns."""
import unittest
import numpy as np
from .config_factory import refine_config
from track.v2.models import SourceBatch, SpatialContext
from track.v2.trajectories.refine_trajectories import refine_vehicle, refine, ray_intersection, heading_vector
from track.v2.validation.resolve_point_references import replay_refine
from track.v2.projection import CoordinateTransformer

CONTEXT = SpatialContext(4326, 32650, {"coordinate_semantics_version": 2,
                                     "enforce_area_of_use": True, "max_scale_error": 0.01})


def batch(times, speeds=None, headings=None, lonlat=None):
    n = len(times)
    return SourceBatch("fixture", "original", np.asarray(lonlat if lonlat is not None else
                       np.column_stack((np.linspace(116, 116.001, n), np.full(n, 39.))), dtype=np.float64),
                       np.asarray(times, dtype=np.float64), np.asarray(headings or [0] * n, dtype=np.float64),
                       np.asarray(speeds if speeds is not None else [10] * n, dtype=np.float64),
                       np.arange(n, dtype=np.int64), np.ones(n, dtype=bool), np.full(n, ""))


class RefineArrayRulesTest(unittest.TestCase):
    def test_gap_splits_and_preserves_vehicle(self):
        result = refine_vehicle(batch([0, 1, 200, 201]), spatial_context=CONTEXT,
                                config=refine_config(max_gap_s=120))
        self.assertEqual([len(t) for t in result.subtracks], [2, 2])
        self.assertEqual([t.vehicle_id for t in result.subtracks], ["original", "original"])
        self.assertEqual([t.subtrack_index for t in result.subtracks], [1, 2])

    def test_no_gap_is_single_track(self):
        result = refine_vehicle(batch([0, 1, 2]), spatial_context=CONTEXT, config=refine_config())
        self.assertEqual(len(result.subtracks), 1)
        self.assertEqual(len(result.subtracks[0]), 3)

    def test_stationary_keeps_endpoints_and_excludes_only_interior(self):
        result = refine_vehicle(batch([0, 30, 60, 90], [0] * 4), spatial_context=CONTEXT,
                                config=refine_config(max_stationary_duration_s=20))
        self.assertEqual(result.excluded.source_row_key.tolist(), [1, 2])
        self.assertEqual(result.subtracks[0].source_row_key.tolist(), [0, 3])
        self.assertEqual(result.excluded.reason_counts(), {'stationary': 2})

    def test_short_stationary_run_retained(self):
        result = refine_vehicle(batch([0, 1, 2], [0] * 3), spatial_context=CONTEXT,
                                config=refine_config(max_stationary_duration_s=20))
        self.assertEqual(len(result.excluded), 0)
        self.assertEqual(len(result.subtracks[0]), 3)

    def test_stationary_exclusion_is_partition_local(self):
        result = refine_vehicle(batch([0, 30, 60, 90, 300, 330, 360, 390], [0] * 8),
                                spatial_context=CONTEXT,
                                config=refine_config(max_gap_s=120, max_stationary_duration_s=20))
        self.assertEqual(result.excluded.source_row_key.tolist(), [1, 2, 5, 6])
        self.assertEqual([len(t) for t in result.subtracks], [2, 2])

    def test_refine_does_not_mutate_source_and_projects_authoritative_xy(self):
        source = batch([0, 1])
        snapshot = source.lonlat.copy()
        result = refine_vehicle(source, spatial_context=CONTEXT, config=refine_config())
        np.testing.assert_array_equal(source.lonlat, snapshot)
        self.assertFalse(result.subtracks[0].xy.flags.writeable)
        self.assertEqual(result.spatial_context, CONTEXT)
        expected = CoordinateTransformer(CONTEXT).forward_many(source.lonlat)
        np.testing.assert_array_equal(result.subtracks[0].xy, expected)

    def test_eager_report_and_replay_groups(self):
        source = batch([0, 30, 60, 90], [0] * 4)
        class Handle:
            def iter_vehicle_batches(self):
                yield source
        result = refine(Handle(), spatial_context=CONTEXT,
                        config=refine_config(max_stationary_duration_s=20))
        self.assertEqual(result.report.as_dict()['active_count'], 2)
        self.assertEqual(result.report.excluded_count, 2)
        replay = replay_refine('original', {'original': source}, refine_result=result).as_dict()
        self.assertEqual(len(replay['original']), 4)
        self.assertEqual(len(replay['excluded']), 2)
        self.assertEqual(len(replay['active']), 2)
        self.assertEqual(replay['active'][0]['point_ref']['source_row_key'], '0')


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




def test_injected_identity_anchor_and_replay_are_independent_columns():
    source = batch([0, 10], headings=[90, 0], lonlat=[[116, 39], [116, 39]])
    class Handle:
        def iter_vehicle_batches(self):
            yield source
    result = refine(Handle(), spatial_context=CONTEXT,
                    config=refine_config(min_heading_delta_deg=45, max_heading_delta_deg=135))
    track = result.subtracks[0]
    assert track.kind.tolist() == ['p', 'i', 'p']
    assert track.source_row_key_valid.tolist() == [True, False, True]
    assert track.refine_point_id_valid.tolist() == [False, False, False]
    assert track.after_sequence.tolist() == [-1, 0, -1]
    assert track.timestamp.tolist() == [0, 5, 10]
    assert result.report.injected_count == 1
    replay = replay_refine('original', {'original': source}, refine_result=result).as_dict()
    assert len(replay['injected']) == 1
    point, left, right = replay['injected'][0]
    assert point['point_ref']['from_ref'] == '0'
    assert point['point_ref']['to_ref'] == '1'
    assert left['point_ref']['source_row_key'] == '0'
    assert right['point_ref']['source_row_key'] == '1'


def test_kept_replay_preserves_source_stability_hash_and_extra():
    source = SourceBatch('fixture', 'original', np.array([[116., 39.], [116.001, 39.]]),
                         np.array([0., 1.]), np.zeros(2), np.ones(2), np.array([0, -1]),
                         np.array([False, True]), np.array(['a' * 64, 'b' * 64]),
                         {'altitude': np.array([10., 20.])})
    class Handle:
        def iter_vehicle_batches(self):
            yield source
    result = refine(Handle(), spatial_context=CONTEXT, config=refine_config())
    output = replay_refine('original', {'original': source}, refine_result=result).as_dict()
    assert output['active'][0]['point_ref']['stable'] is False
    assert output['active'][0]['point_ref']['source_row_hash'] == 'a' * 64
    assert output['active'][1]['extra']['altitude'] == 20.
