"""Geometric-median centres and cumulative 95% candidate radii."""
import math

import numpy as np
import pytest

from track.v2.intersections.candidate_geometry import candidate_circle
from track.v2.intersections.detect_intersections import (
    _cluster_dbscan,
    _cluster_from_points,
)
from .detect_columns_helpers import TRANSFORMER
from .test_detect_algorithm_regressions import _events


@pytest.mark.parametrize("points, expected", [
    ([[4, 8]], [4, 8]),
    ([[4, 8]] * 6, [4, 8]),
    ([[0, 0], [10, 4]], [5, 2]),
    ([[0, 0], [1, 2], [9, 18]], [1, 2]),
    ([[0, 0]] * 3 + [[10, 0], [0, 10]], [0, 0]),
])
def test_degenerate_and_repeated_members(points, expected):
    center, radius = candidate_circle(np.asarray(points))
    np.testing.assert_allclose(center, expected, atol=1e-6)
    assert math.isfinite(radius) and radius >= 0


def test_iterate_at_nonoptimal_member_moves_to_geometric_median():
    # The coordinate-wise median is (0, 0), but the geometric median is inside.
    points = np.array([[0., 0.], [1., 0.], [0., 1.]])
    center, _ = candidate_circle(points)
    expected = (3 - math.sqrt(3)) / 6
    np.testing.assert_allclose(center, [expected, expected], atol=3e-6)


def test_geometric_median_is_rotation_translation_and_order_invariant():
    points = np.array([[0., 0.], [2., 0.], [1., math.sqrt(3)]])
    angle = .71
    rotation = np.array([[math.cos(angle), -math.sin(angle)],
                         [math.sin(angle), math.cos(angle)]])
    offset = np.array([335000., 3845000.])
    transformed = points @ rotation.T + offset
    expected = np.array([1., math.sqrt(3) / 3]) @ rotation.T + offset
    center, radius = candidate_circle(transformed[::-1])
    np.testing.assert_allclose(center, expected, rtol=0, atol=3e-6)
    assert radius == pytest.approx(2 / math.sqrt(3), abs=3e-6)


@pytest.mark.parametrize("size, outside, expected_radius", [
    (20, 1, 0.),       # Exactly 95% at the centre; interpolation would give 5m.
    (21, 1, 0.),
    (19, 1, 100.),     # 18/19 is below 95%, so the last point must be enclosed.
    (100, 6, 100.),
])
def test_radius_is_smallest_distance_enclosing_at_least_95_percent(size, outside, expected_radius):
    points = np.array([[0., 0.]] * (size-outside) + [[100., 0.]] * outside)
    center, radius = candidate_circle(points)
    np.testing.assert_array_equal(center, [0., 0.])
    assert radius == expected_radius
    distances = np.linalg.norm(points-center, axis=1)
    assert np.count_nonzero(distances <= radius) >= math.ceil(.95 * size)
    assert np.count_nonzero(distances < radius) < math.ceil(.95 * size)


@pytest.mark.parametrize("joint", [False, True])
def test_clustering_uses_robust_circle_without_dropping_members(joint):
    turns = _events([[0., 0.]] * 19)
    if joint:
        cross = _events([[100., 0.]], cross=True)
        clusters = _cluster_from_points(
            [("turn", turns), ("orthogonal_cross_track", cross)],
            TRANSFORMER, eps_m=150., min_samples=3,
        )
    else:
        clusters = _cluster_dbscan(
            _events([[0., 0.]] * 19 + [[100., 0.]]),
            TRANSFORMER, eps_m=150., min_samples=3,
        )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert (cluster.centroid_x, cluster.centroid_y, cluster.radius_m) == (0., 0., 0.)
    assert len(cluster.members) == 20
    assert sum(cluster.source_counts.values()) == 20
    if joint:
        assert cluster.source_counts == {"turn": 19, "orthogonal_cross_track": 1}
    np.testing.assert_allclose(
        [cluster.centroid_lon, cluster.centroid_lat], TRANSFORMER.inverse(0., 0.)
    )


@pytest.mark.parametrize("points", [[], [[1]], [[float('nan'), 0]], [[0, float('inf')]]])
def test_invalid_geometry_is_rejected(points):
    with pytest.raises(ValueError):
        candidate_circle(np.asarray(points))


def test_slow_almost_collinear_cluster_has_verified_mean_distance_gap(monkeypatch):
    from track.v2.intersections import candidate_geometry as geometry

    # Along a million-metre span, a nearly flat objective need not produce
    # micrometre centre steps. The projected 1-D optimum is a global lower
    # bound on the 2-D mean-distance objective.
    points = np.random.default_rng(2).normal(size=(1000, 2)) * [1e6, 1.]
    points += [335000., 3845000.]
    monkeypatch.setattr(geometry, "_FAST_ITERATIONS", 16)
    monkeypatch.setattr(geometry, "_MAX_ITERATIONS", 64)
    center, radius = candidate_circle(points)
    distances = np.hypot(*(points - center).T)
    lower_bound = np.mean(np.abs(points[:, 0] - np.median(points[:, 0])))
    assert np.mean(distances) - lower_bound <= 1.0
    assert radius == np.sort(distances)[949]


def test_objective_certificate_bounds_known_triangle_optimum():
    from track.v2.intersections.candidate_geometry import _optimality_gap

    points = np.array([[0., 0.], [1000., 0.], [0., 1000.]])
    optimum_coordinate = 1000 * (3 - math.sqrt(3)) / 6
    optimum = np.array([optimum_coordinate, optimum_coordinate])
    optimum_objective = np.mean(np.hypot(*(points - optimum).T))
    for center in (np.zeros(2), optimum, np.array([700., -200.])):
        bound, objective = _optimality_gap(points, center)
        assert 0 <= objective - optimum_objective <= bound + 1e-10


def test_near_coincident_certificate_charges_approximation_error():
    from track.v2.intersections.candidate_geometry import _optimality_gap

    points = np.array([[0., 0.], [0., 1e-9], [1e6, 0.]])
    bound, objective = _optimality_gap(points, np.zeros(2))
    # The near member is not exactly coincident: its allowance is nonzero.
    assert bound > 0
    assert objective == pytest.approx(1e6 / 3)
    assert bound < 1.0


def test_nonunique_median_is_accepted_by_objective_without_position_claim():
    points = np.array([[0., 0.], [1e6, 0.]])
    center, _ = candidate_circle(points)
    # Every centre on this segment has exactly the same optimal objective.
    assert np.mean(np.hypot(*(points - center).T)) == 500000.
    assert 0 <= center[0] <= 1e6
    assert center[1] == 0


def test_iteration_limit_without_acceptance_is_explicit_numerical_failure(monkeypatch):
    from track.v2.intersections import candidate_geometry as geometry

    monkeypatch.setattr(geometry, "_MAX_ITERATIONS", 0)
    monkeypatch.setattr(geometry, "_FAST_ITERATIONS", 0)
    with pytest.raises(geometry.CandidateGeometryNumericalError) as captured:
        candidate_circle(np.array([[0., 0.], [1000., 0.], [0., 1000.]]))
    diagnostics = captured.value.diagnostics
    assert diagnostics["point_count"] == 3
    assert diagnostics["iterations"] == 0
    assert diagnostics["objective_gap_bound_m"] > diagnostics["tolerance_m"] == 1.0
    assert "points" not in diagnostics


def test_unrepresentable_span_is_explicit_numerical_failure():
    from track.v2.intersections.candidate_geometry import CandidateGeometryNumericalError

    largest = np.finfo(np.float64).max
    with pytest.raises(CandidateGeometryNumericalError):
        candidate_circle(np.array([[-largest, 0.], [largest, 0.], [largest, 0.]]))


def test_repeated_large_finite_coordinates_do_not_overflow_median_average():
    largest = np.finfo(np.float64).max
    center, radius = candidate_circle(np.array([[largest, largest]] * 2))
    np.testing.assert_array_equal(center, [largest, largest])
    assert radius == 0
