import numpy as np
import pytest

from track.v2.grid_cross_track_candidates import GridSegmentBudgetError, build_grid_segments


def _point(point_id, sequence, x, *, kind=0):
    return {
        "point_id": point_id, "point_kind": kind,
        "refined_sequence_in_subtrack": sequence,
        "source_id": "public.injected_points" if kind else "source_points",
        "source_row_key": None if kind else str(point_id),
        "generated_reason": "test" if kind else None,
        "timestamp": float(sequence), "lon": 110.0, "lat": 20.0,
        "heading": 90.0, "speed": 5.0, "x": x, "y": 0.0,
    }


def _row(vehicle, point, prev=None, next_=None):
    result = {"vehicle_id": vehicle, "subtrack_index": 1, **point}
    for prefix, neighbor in (("prev_", prev), ("next_", next_)):
        for key in point:
            result[prefix + key] = None if neighbor is None else neighbor[key]
    return result


def _batch(*rows):
    return {name: tuple(row[name] for row in rows) for name in rows[0]}


def test_build_grid_segments_deduplicates_both_hit_endpoints_and_sorts():
    first = _point(10, 0, 0.0)
    injected = _point(20, 1, 1.0, kind=1)
    third = _point(11, 2, 2.0)
    other_first = _point(30, 0, 0.0)
    other_next = _point(31, 1, 1.0)
    batches = [
        _batch(_row("z", other_first, next_=other_next),
               _row("a", first, next_=injected)),
        _batch(_row("a", injected, prev=first, next_=third)),
    ]

    result = build_grid_segments(batches, max_segments=3, max_bytes=100_000)

    assert result["vehicle_id"] == ("a", "a", "z")
    assert result["segment_identity"] == (
        ("a", 1, 0, 10, 1, 20),
        ("a", 1, 1, 20, 0, 11),
        ("z", 1, 0, 30, 0, 31),
    )
    np.testing.assert_array_equal(result["x_start"], [0.0, 1.0, 0.0])
    np.testing.assert_array_equal(result["x_end"], [1.0, 2.0, 1.0])
    assert result["start_point_kind"] == ("kept", "injected", "kept")
    assert result["start_source_id"][1] == "public.injected_points"
    assert result["start_generated_reason"][1] == "test"
    assert result["start_source_row_key"] == ("10", None, "30")


def test_build_grid_segments_budget_fails_without_retry():
    first = _point(10, 0, 0.0)
    second = _point(11, 1, 1.0)
    third = _point(12, 2, 2.0)
    batch = _batch(_row("a", second, prev=first, next_=third))
    with pytest.raises(GridSegmentBudgetError, match="max_segments"):
        build_grid_segments([batch], max_segments=1, max_bytes=100_000)
    with pytest.raises(GridSegmentBudgetError, match="max_bytes"):
        build_grid_segments([batch], max_segments=2, max_bytes=100)


def test_build_grid_segments_rejects_nonadjacent_neighbours():
    first = _point(10, 0, 0.0)
    third = _point(12, 2, 2.0)
    with pytest.raises(ValueError, match="not adjacent"):
        build_grid_segments(
            [_batch(_row("a", third, prev=first))],
            max_segments=2, max_bytes=100_000,
        )
