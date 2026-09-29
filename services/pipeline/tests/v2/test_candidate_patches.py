"""Grid index capacity and finite-coordinate regressions."""
import numpy as np

from track.v2.candidates.build_patches import build_cross_track_index


def test_index_holds_numeric_spans_and_membership_fallback_avoids_grid():
    starts = np.asarray([[-20., 0.], [0., -20.]])
    ends = -starts
    index = build_cross_track_index(starts, ends, np.asarray([1, 2], dtype=np.int64),
                                    cell_m=10, max_cells=100)
    assert index.spans.shape == (2, 4) and index.spans.dtype == np.int64
    assert index.grid
    fallback = build_cross_track_index(starts, ends, np.asarray([1, 2], dtype=np.int64),
                                       cell_m=10, max_cells=100, max_grid_memberships=0)
    assert fallback.grid == {} and fallback.overflow_segment_ids == [0, 1]


def test_nonfinite_segment_is_excluded_from_index():
    starts = np.asarray([[np.nan, 0.], [0., -20.]])
    index = build_cross_track_index(starts, np.asarray([[20., 0.], [0., 20.]]),
                                    np.asarray([1, 2], dtype=np.int64), cell_m=10, max_cells=1)
    assert index.finite.tolist() == [False, True]
    assert index.overflow_segment_ids == [1]


def test_overflow_work_limit_keeps_finite_segments():
    starts = np.asarray([[0., 0.], [0., 1.], [0., 2.], [0., 3.]])
    ends = starts + np.asarray([1000., 1000.])
    telemetry = {}
    index = build_cross_track_index(starts, ends, np.asarray([1, 1, 2, 3], dtype=np.int64),
                                    cell_m=1, max_cells=1, max_grid_memberships=0,
                                    max_overflow_pair_space=1, telemetry=telemetry)
    assert index.overflow_segment_ids == [0, 1, 2, 3]
    assert telemetry['overflow_pair_space_estimate'] == 16
    assert telemetry['overflow_pair_budget_exceeded'] == 1
