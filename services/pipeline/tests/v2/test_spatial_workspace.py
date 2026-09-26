"""Array workspace capacity and spatial-window boundary regressions."""
import math
from unittest.mock import patch
import numpy as np
import pytest
from .config_factory import intersect_config
from .detect_columns_helpers import TRANSFORMER, subtrack
from track.v2.candidates.build_patches import CrossTrackCapacityError
from track.v2.candidates.collect_candidates import extract_cross_track_candidates
from track.v2.candidates.query_source_windows import SpatialWindowIterator


def _crossing_tracks():
    return {1: subtrack([[0, -300], [0, 300]], subtrack_index=1, vehicle_id="A", headings=[0, 0]),
            2: subtrack([[-300, 0], [300, 0]], subtrack_index=2, vehicle_id="B", headings=[90, 90])}


def test_capacity_guard_runs_before_grid_allocation():
    with patch("track.v2.candidates.build_patches._build_grid", side_effect=AssertionError("grid must not be allocated")):
        with pytest.raises(CrossTrackCapacityError, match="overflow fallback"):
            extract_cross_track_candidates(_crossing_tracks(), TRANSFORMER,
                intersect_config(cross_track_max_grid_cells_per_segment=1), max_overflow_pair_space=1)


def test_memory_workspace_emits_one_crossing_with_run_partition_identity():
    candidates, eligible, scheduled = extract_cross_track_candidates(_crossing_tracks(), TRANSFORMER, intersect_config())
    assert eligible == scheduled == 1
    assert len(candidates) == 1
    assert candidates.from_subtrack_index.tolist() == [1]
    assert candidates.to_subtrack_index.tolist() == [2]
    assert candidates.from_vehicle_id.tolist() == ["A"]
    assert candidates.to_vehicle_id.tolist() == ["B"]
    np.testing.assert_array_equal(candidates.xy, [[0, 0]])
    assert candidates.calculation_index.tolist() == [0]


def test_window_shape_exact_multiple_has_no_phantom_column() -> None:
    step = 1_000.0
    windows = SpatialWindowIterator(
        (0.0, 0.0, 7.0 * step, 3.0 * step),
        size=(1_000.0, 1_000.0),
        overlap=0.15,
    )

    assert windows.window_shape() == (7, 3)
    assert windows.count_windows() == 21
    assert len(list(windows.iter_windows())) == 21


def test_window_iterator_count_always_matches_materialized_windows() -> None:
    step = 2_000.0
    for ulps, expected_lon_count in ((-1, 7), (0, 7), (1, 8)):
        edge = 7.0 * step
        if ulps < 0:
            edge = math.nextafter(edge, -math.inf)
        elif ulps > 0:
            edge = math.nextafter(edge, math.inf)
        windows = SpatialWindowIterator(
            (0.0, 0.0, edge, 2.3 * step),
            size=(2_000.0, 2_000.0),
            overlap=0.0,
        )
        materialized = list(windows.iter_windows())
        assert windows.window_shape()[0] == expected_lon_count
        assert windows.count_windows() == len(list(windows.iter_windows()))
        assert materialized[-1].x_max == edge


def test_tiny_positive_extent_still_has_one_window_per_axis() -> None:
    tiny = math.nextafter(0.0, math.inf)
    windows = SpatialWindowIterator((0.0, 0.0, tiny, tiny))

    assert windows.window_shape() == (1, 1)
    assert windows.count_windows() == 1
