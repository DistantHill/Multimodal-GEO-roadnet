"""Behavioral coverage for native geometry, lineage, and bounded work."""
import gc

import numpy as np
import pytest

from pipeline import _native


def refine(points, times=None, headings=None, speeds=None, **config):
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = len(points)
    return _native.refine(
        points,
        np.asarray(times if times is not None else np.arange(n), dtype=float),
        np.asarray(headings if headings is not None else np.full(n, np.nan), dtype=float),
        np.asarray(speeds if speeds is not None else np.full(n, 10.0), dtype=float),
        config,
    )


def test_refine_splits_time_gaps_and_keeps_source_indices():
    result = refine([[0, 0], [1, 0], [3, 0], [4, 0]], [0, 1, 200, 201])
    assert result['subtrack_index'].tolist() == [1, 1, 2, 2]
    assert result['sequence'].tolist() == [0, 1, 0, 1]
    assert result['source_index'].tolist() == [0, 1, 2, 3]
    assert result['excluded_index'].tolist() == []


def test_refine_stationary_endpoints_and_spatial_guard():
    result = refine([[0, 0], [1, 0], [-1, 0], [0, 0]], [0, 30, 60, 90], speeds=[0] * 4)
    assert result['source_index'].tolist() == [0, 3]
    assert result['excluded_index'].tolist() == [1, 2]
    assert result['excluded_reason'] == ['stationary_interior'] * 2
    moving = refine([[0, 0], [50, 0], [100, 0], [150, 0]], [0, 30, 60, 90], speeds=[0] * 4)
    assert moving['source_index'].tolist() == [0, 1, 2, 3]


def test_refine_isolated_spike_removal_preserves_feasible_track():
    result = refine([[0, 0], [1000, 1000], [2, 0], [3, 0]])
    assert result['source_index'].tolist() == [0, 2, 3]
    assert result['excluded_index'].tolist() == [1]
    assert result['excluded_reason'] == ['isolated_speed_spike']
    assert result['subtrack_index'].tolist() == [1, 1, 1]
    alternating = refine([[0, 0], [1000, 0], [2, 0], [1000, 1], [4, 0]])
    assert alternating['source_index'].tolist() == [0, 2, 4]


def test_refine_duplicate_time_and_nonfinite_positions_have_reasons():
    result = refine([[0, 0], [0, 0], [float('nan'), 2], [3, 0]], [0, 0, 1, 2])
    assert result['source_index'].tolist() == [0, 3]
    assert result['excluded_reason'] == ['duplicate_timestamp', 'nonfinite_position_or_time']
    with pytest.raises(ValueError, match='sorted'):
        refine([[0, 0], [1, 0]], [1, 0])


def test_refine_orthogonal_injection_uses_early_forward_late_backward_rays():
    # Early eastbound at (0,0), later northbound at (10,10): turn is (10,0).
    result = refine([[0, 0], [10, 10]], [0, 10], [90, 0])
    np.testing.assert_allclose(result['xy'], [[0, 0], [10, 0], [10, 10]], atol=1e-12)
    assert result['kind'].tolist() == [0, 1, 0]
    assert result['source_index'].tolist() == [0, -1, 1]
    assert result['from_index'].tolist() == [0, 0, 1]
    assert result['to_index'].tolist() == [0, 1, 1]
    np.testing.assert_allclose(result['ratio_in_gap'], [0, 0.5, 0])
    np.testing.assert_allclose(result['timestamp'], [0, 5, 10])
    np.testing.assert_allclose(result['heading_cal'], [90, 0, 0], atol=1e-12)
    # This intersection requires extending the earlier point backward; it is invalid.
    rejected = refine([[10, 0], [0, 10]], [0, 10], [90, 0])
    assert rejected['kind'].tolist() == [0, 0]


def test_refine_empty_and_output_ownership():
    result = refine([])
    assert result['xy'].shape == (0, 2)
    positions = np.array([[0., 0.], [10., 0.]])
    snapshot = positions.copy()
    result = _native.refine(positions, np.array([0., 1.]), np.array([90., 90.]), np.ones(2), {})
    del positions
    gc.collect()
    np.testing.assert_array_equal(result['xy'], snapshot)


def turn_fixture():
    xy = np.array([[-20., 0.], [-10., 0.], [0., 0.], [0., 10.], [0., 20.]])
    return xy, np.arange(5, dtype=float), np.array([90., 90., 0., 0., 0.]), np.ones(5, np.int64), np.arange(5, dtype=np.int64)


def test_turn_uses_five_points_and_circular_heading_means():
    result = _native.turns(*turn_fixture(), {'min_turn_deg': 80., 'max_turn_deg': 100.})
    assert result['index'].tolist() == [2]
    np.testing.assert_allclose(result['turn_deg'], [-90])
    assert _native.turns(*turn_fixture(), {'min_turn_deg': 91.})['index'].size == 0
    xy, times, heading, track, seq = turn_fixture()
    heading[:] = [359, 1, 89, 91, 91]
    np.testing.assert_allclose(_native.turns(xy, times, heading, track, seq, {})['turn_deg'], [90], atol=1e-10)


def test_turn_does_not_bridge_subtracks_or_missing_sequence():
    xy, times, heading, track, seq = turn_fixture()
    track[-1] = 2
    assert _native.turns(xy, times, heading, track, seq, {})['index'].size == 0
    track[-1] = 1
    seq[-1] = 6
    assert _native.turns(xy, times, heading, track, seq, {})['index'].size == 0


def test_crossings_are_exact_deduplicated_and_cross_vehicle():
    segments = np.array([[-100, 0, 100, 0], [0, -100, 0, 100], [-100, 5, 100, 5]], dtype=float)
    result = _native.crossings(segments, np.array([10, 20, 10]), {'cross_grid_size_m': 10.})
    assert sorted(zip(result['segment_a'], result['segment_b'])) == [(0, 1), (1, 2)]
    np.testing.assert_allclose(result['xy'], [[0, 0], [0, 5]])
    np.testing.assert_allclose(result['angle_deg'], [90, 90])
    assert _native.crossings(segments, np.zeros(3, dtype=np.int64), {})['xy'].shape == (0, 2)


def test_crossings_include_shared_endpoint_but_not_parallel_overlap():
    segments = np.array([[0, 0, 10, 0], [10, 0, 10, 10], [5, 0, 9, 0]], dtype=float)
    result = _native.crossings(segments, np.arange(3, dtype=np.int64), {'cross_grid_size_m': 10.})
    np.testing.assert_allclose(result['xy'], [[10, 0]])


def test_crossing_filters_and_resource_limits():
    segments = np.array([[-100, 0, 100, 0], [0, -100, 0, 100]], dtype=float)
    tracks = np.array([1, 2])
    result = _native.crossings(segments, tracks, {}, headings=np.array([[90., 180.], [0., 0.]]))
    assert result['eligible_segments'] == 1
    assert len(result['xy']) == 0
    result = _native.crossings(segments, tracks, {'cross_min_speed_mps': 1.}, speeds=np.zeros((2, 2)))
    assert result['eligible_segments'] == 0
    with pytest.raises(RuntimeError, match='max_grid_memberships'):
        _native.crossings(segments, tracks, {'cross_grid_size_m': 1., 'max_grid_memberships': 2})
    with pytest.raises(ValueError, match='finite'):
        _native.crossings(np.array([[np.nan, 0, 1, 1]]), np.array([1]), {})


def reference_dbscan(points, eps, minimum):
    """Small independent brute-force oracle; production does not allocate this matrix."""
    distances = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    neighbors = [np.flatnonzero(row <= eps).tolist() for row in distances]
    labels = np.full(len(points), -1, dtype=int)
    core = np.array([len(ids) >= minimum for ids in neighbors])
    label = 0
    for i in range(len(points)):
        if labels[i] >= 0 or not core[i]:
            continue
        pending = [i]
        labels[i] = label
        while pending:
            current = pending.pop()
            for other in neighbors[current]:
                if labels[other] >= 0:
                    continue
                labels[other] = label
                if core[other]:
                    pending.append(other)
        label += 1
    return labels


def test_cluster_matches_independent_density_oracle():
    rng = np.random.default_rng(42)
    points = np.concatenate([rng.normal([0, 0], 0.2, (25, 2)), rng.normal([3, 3], 0.2, (25, 2)), [[10, 10], [20, 20]]])
    rng.shuffle(points)
    result = _native.cluster(points, 0.35, 5)
    np.testing.assert_array_equal(result['labels'], reference_dbscan(points, 0.35, 5))
    assert sum(result['counts']) + np.count_nonzero(result['labels'] == -1) == len(points)
    for label, center in enumerate(result['centers']):
        np.testing.assert_allclose(center, points[result['labels'] == label].mean(axis=0))


def test_cluster_dense_duplicate_events_do_not_enumerate_all_pairs():
    result = _native.cluster(np.zeros((10000, 2)), 10., 3, max_pair_checks=1)
    assert result['counts'].tolist() == [10000]
    assert result['pair_checks'] == 0
    assert _native.cluster(np.empty((0, 2)), 1., 2)['centers'].shape == (0, 2)


def test_topology_requires_arms_and_multiple_vehicles():
    centers = np.array([[0., 0.]])
    cross = np.array([[-100, 0, 100, 0], [0, -100, 0, 100]], dtype=float)
    result = _native.topology(centers, cross, np.array([1, 2]), {})
    assert result['arm_count'].tolist() == [4]
    assert result['vehicle_count'].tolist() == [2]
    assert result['valid'].tolist() == [1]
    np.testing.assert_allclose(result['arm_bearings'], [0, 90, 180, 270])
    assert _native.topology(centers, cross, np.array([1, 1]), {})['valid'].tolist() == [0]
    straight = np.array([[-100, -1, 100, -1], [-100, 1, 100, 1]], dtype=float)
    assert _native.topology(centers, straight, np.array([1, 2]), {})['arm_count'].tolist() == [2]
    assert _native.topology(centers, straight, np.array([1, 2]), {})['valid'].tolist() == [0]


def test_fit_orients_tracks_and_uses_median_then_fixed_spacing():
    points = np.array([[0, -1], [10, -1], [20, -1], [20, 1], [0, 1]], dtype=float)
    result = _native.fit(points, np.array([0, 3, 5]), np.array([0, 1], np.uint8), {'point_spacing_m': 7., 'fit_sample_spacing_m': 3.})
    assert result['status'] == 'ok'
    np.testing.assert_allclose(result['xy'], [[0, 0], [7, 0], [14, 0], [20, 0]], atol=1e-12)
    assert result['length_m'] == pytest.approx(20)
    assert result['forward_clip_count'] == result['reverse_clip_count'] == 1
    np.testing.assert_array_equal(result['used_clip_index'], [0, 1])


def test_fit_rejects_degenerate_support_and_limits_and_keeps_curvature():
    points = np.array([[0, 0], [0, 0], [0, 0], [10, 10], [20, 0]], dtype=float)
    result = _native.fit(points, np.array([0, 2, 5]), np.zeros(2, np.uint8), {})
    assert result['used_clip_index'].tolist() == [1]
    assert result['xy'][:, 1].max() > 5
    insufficient = _native.fit(points, np.array([0, 2, 5]), np.zeros(2, np.uint8), {'min_clips_per_pair': 2})
    assert insufficient['status'] == 'insufficient_clips'
    assert insufficient['xy'].shape == (0, 2)
    with pytest.raises(RuntimeError, match='max_fit_points'):
        _native.fit(points, np.array([0, 5]), np.zeros(1, np.uint8), {'point_spacing_m': 0.001, 'max_fit_points': 100})
    with pytest.raises(ValueError, match='offsets'):
        _native.fit(points, np.array([0, 6]), np.zeros(1, np.uint8), {})


def test_clip_interpolates_sparse_crossings_and_preserves_boundary_provenance():
    xy = np.array([[-100., 0.], [200., 0.]])
    centers = np.array([[0., 0.], [100., 0.]])
    clips = _native.clip_track(xy, centers, np.array([10., 10.]), {'min_length_m': 0.})
    assert [row['clip_type'] for row in clips] == ['boundary_start', 'between_intersection', 'boundary_end']
    middle = clips[1]
    np.testing.assert_allclose(middle['xy'], [[10., 0.], [90., 0.]])
    assert middle['length_m'] == pytest.approx(80)
    assert middle['source_index'].tolist() == [-1, -1]
    assert middle['from_index'].tolist() == [0, 0]
    assert middle['to_index'].tolist() == [1, 1]
    np.testing.assert_allclose(middle['ratio_in_gap'], [110 / 300, 190 / 300])
    assert middle['start_intersection_index'] == 0
    assert middle['end_intersection_index'] == 1
    connected = _native.clip_track(xy, centers, np.array([10., 10.]), {'include_boundary_clips': False})
    assert len(connected) == 1


def test_clip_exact_sampled_boundaries_do_not_lose_connections():
    x = [-100, -10, 0, 10, 50, 90, 100, 110, 200]
    xy = np.column_stack((x, np.zeros(len(x))))
    clips = _native.clip_track(xy, np.array([[0., 0.], [100., 0.]]), np.array([10., 10.]), {})
    assert [row['clip_type'] for row in clips] == ['boundary_start', 'between_intersection', 'boundary_end']
    np.testing.assert_allclose(clips[1]['xy'], [[10, 0], [50, 0], [90, 0]])
    assert clips[1]['source_index'].tolist() == [3, 4, 5]


def test_clip_tangent_overlapping_discs_and_self_return():
    tangent = _native.clip_track(np.array([[-20., 10.], [20., 10.]]), np.array([[0., 0.]]), np.array([10.]), {})
    assert [row['clip_type'] for row in tangent] == ['full_track']
    overlap = _native.clip_track(np.array([[-30., 0.], [40., 0.]]), np.array([[0., 0.], [10., 0.]]), np.array([10., 10.]), {})
    assert [row['clip_type'] for row in overlap] == ['boundary_start', 'boundary_end']
    np.testing.assert_allclose(overlap[0]['xy'], [[-30, 0], [-10, 0]])
    np.testing.assert_allclose(overlap[1]['xy'], [[20, 0], [40, 0]])
    assert overlap[1]['start_intersection_index'] == 1
    repeated = _native.clip_track(np.array([[-30., 0.], [0., 0.], [30., 0.], [0., 0.], [-30., 0.]]), np.array([[0., 0.]]), np.array([10.]), {})
    assert [row['clip_type'] for row in repeated] == ['boundary_start', 'same_intersection', 'boundary_end']
    np.testing.assert_allclose(repeated[1]['xy'], [[10, 0], [30, 0], [10, 0]])
    assert _native.clip_track(np.empty((0, 2)), np.empty((0, 2)), np.empty(0), {}) == []
