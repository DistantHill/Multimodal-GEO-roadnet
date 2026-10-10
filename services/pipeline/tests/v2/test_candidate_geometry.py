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
