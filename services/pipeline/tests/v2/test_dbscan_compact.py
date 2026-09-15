"""Exact-label and storage behavior checks; deliberately small bounded inputs."""
from __future__ import annotations

from array import array
import math
import random
from unittest.mock import patch

import pytest

from track.v2 import detect
from track.v2.dbscan_storage import ActiveGridLinks, index_typecode


def invoke(function, xs, ys, eps, minimum):
    return function(xs, ys, eps_m=eps, min_samples=minimum)


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("eps,minimum", [(0.25, 1), (0.5, 3), (0.9, 5), (0.5, 0)])
def test_labels_match_historical_scan(seed, eps, minimum):
    rng = random.Random(seed)
    xs = array("d", (rng.randrange(-12, 13) * .25 for _ in range(300)))
    ys = array("d", (rng.randrange(-12, 13) * .25 for _ in range(300)))
    expected = invoke(detect._run_dbscan_scan, xs, ys, eps, minimum)
    assert invoke(detect._run_dbscan, xs, ys, eps, minimum) == expected


@pytest.mark.parametrize("shape", ["ambiguous_border", "initial_noise", "signed_seams", "duplicates"])
def test_boundary_assignment_noise_and_lifo_order(shape):
    if shape == "ambiguous_border":
        xs = [0., .9, 1.7, 1.5, 1.5, -.9, -1.7, -1.5, -1.5]
        ys = [0., 0., 0., .6, -.6, 0., 0., .6, -.6]
        eps, minimum = 1., 4
    elif shape == "initial_noise":
        xs, ys = [0., .9, 1.7, 1.5, 1.5], [0., 0., 0., .6, -.6]
        eps, minimum = 1., 4
    elif shape == "signed_seams":
        xs = [-0., 0., -1e-300, 1., math.nextafter(1., -math.inf), math.nextafter(1., math.inf)]
        ys = [0.] * len(xs)
        eps, minimum = 1., 2
    else:
        xs, ys = [0.] * 40 + [5.] * 40, [0.] * 80
        eps, minimum = .5, 3
    missing = 300 - len(xs)
    xs += [100. + 10. * i for i in range(missing)]
    ys += [100.] * missing
    prefixes = {
        "ambiguous_border": [0] * 5 + [1] * 4,
        "initial_noise": [0] * 5,
        "signed_seams": [0] * 6,
        "duplicates": [0] * 40 + [1] * 40,
    }
    expected = prefixes[shape] + [-1] * missing
    assert invoke(detect._run_dbscan_scan, xs, ys, eps, minimum) == expected
    assert invoke(detect._run_dbscan, xs, ys, eps, minimum) == expected


@pytest.mark.parametrize("eps,first", [
    (0., 0.), (-1., 0.), (math.nan, 0.), (math.inf, 0.),
    (1e308, 0.), (1e-320, 1.), (1., 1e308), (1., math.nan), (1., math.inf),
])
def test_invalid_or_nonfinite_grid_inputs_keep_original_scan_fallback(eps, first):
    xs, ys = [first] + [float(i * 10) for i in range(299)], [0.] * 300
    expected = invoke(detect._run_dbscan_scan, xs, ys, eps, 2)
    with patch.object(detect, "_run_dbscan_scan", wraps=detect._run_dbscan_scan) as scan:
        actual = invoke(detect._run_dbscan, xs, ys, eps, 2)
    assert scan.call_count == 1
    assert actual == expected


def test_unequal_vectors_preserve_fallback_exception_or_labels():
    xs, ys = [0.] * 300, [0.] * 299
    try:
        expected = invoke(detect._run_dbscan_scan, xs, ys, 1., 2)
    except Exception as error:
        with pytest.raises(type(error)):
            invoke(detect._run_dbscan, xs, ys, 1., 2)
    else:
        assert invoke(detect._run_dbscan, xs, ys, 1., 2) == expected


def test_signed_index_capacity_and_sentinels_without_large_allocations():
    code = index_typecode(0)
    maximum = (1 << (8 * array(code).itemsize - 1)) - 1
    assert index_typecode(maximum + 1) == code
    if code == "i" and array("i").itemsize < array("q").itemsize:
        assert index_typecode(maximum + 2) == "q"
    assert list(array(code, [-2, -1, 0, maximum])) == [-2, -1, 0, maximum]
    with pytest.raises(ValueError):
        index_typecode(-1)
    with pytest.raises(OverflowError):
        index_typecode(1 << (8 * array("q").itemsize - 1) | 1)


def test_active_chain_head_middle_tail_repeated_delete_and_unlink_during_iteration():
    grid = {(0, 0): array("i", [0, 2, 4, 6]), (1, 0): array("i", [1, 3, 5, 7])}
    links = ActiveGridLinks(grid, 8)
    links.remove((0, 0), 0)
    links.remove((0, 0), 4)
    links.remove((0, 0), 6)
    assert list(links.iter_cell((0, 0))) == [2]
    links.remove((0, 0), 2)
    assert list(links.iter_cell((0, 0))) == []
    with pytest.raises(KeyError):
        links.remove((0, 0), 4)
    encountered = []
    for index in links.iter_cell((1, 0)):
        encountered.append(index)
        links.remove((1, 0), index)
    assert encountered == [1, 3, 5, 7]
    assert list(links.iter_cell((1, 0))) == []
    assert set(links.previous) == {-2} and set(links.following) == {-2}


def test_heap_frontier_exact_global_order_and_remaining_members():
    xs = [0., -.25, .75, -.5, 1., 2., -.75, 4.]
    ys = [0.] * len(xs)
    expected = [1, 2, 3, 4, 6]
    grid = detect._build_dbscan_grid(xs, ys, 1.)
    links = ActiveGridLinks(grid, len(xs))
    links.remove((0, 0), 0)
    actual = detect._take_grid_frontier_neighbors(xs, ys, 0, 1., grid, links)
    assert isinstance(actual, array)
    assert list(actual) == expected == sorted(expected)
    assert sorted(index for cell in grid for index in links.iter_cell(cell)) == [5, 7]


def test_dense_cluster_visits_each_active_node_once_without_rescanning_removed_points():
    class Coordinates:
        def __init__(self, count):
            self.data = array("d", [0.]) * count
            self.reads = 0
        def __len__(self):
            return len(self.data)
        def __getitem__(self, index):
            self.reads += 1
            return self.data[index]
    class CountingLinks(ActiveGridLinks):
        def __init__(self, grid, count):
            super().__init__(grid, count)
            self.visited = 0
        def iter_cell(self, cell):
            for index in super().iter_cell(cell):
                self.visited += 1
                yield index
    count = 2000
    xs, ys = Coordinates(count), Coordinates(count)
    created = []
    def make_links(grid, n):
        assert all(isinstance(indices, array) for indices in grid.values())
        links = CountingLinks(grid, n)
        created.append(links)
        return links
    with patch.object(detect, "ActiveGridLinks", side_effect=make_links):
        labels = invoke(detect._run_dbscan, xs, ys, 1., 3)
    assert labels == [0] * count
    assert created[0].visited == count - 1
    assert xs.reads + ys.reads < 25 * count
    assert all(head == -1 for head in created[0].heads.values())


def test_public_labels_remain_list_with_one_shared_integer_per_large_cluster_label():
    xs = array("d", (float(group * 10) for group in range(270) for _ in range(3)))
    ys = array("d", [0.]) * len(xs)
    labels = invoke(detect._run_dbscan, xs, ys, 1., 2)
    assert isinstance(labels, list)
    assert labels == [group for group in range(270) for _ in range(3)]
    assert all(labels[i] is labels[i+1] is labels[i+2] for i in range(0, len(labels), 3))
