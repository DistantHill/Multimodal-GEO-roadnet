"""Spatial-window boundary regressions."""
import math

from track.v2.candidates.query_source_windows import SpatialWindowIterator

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
