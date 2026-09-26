from types import SimpleNamespace

import numpy as np

from track.v2.candidates.detect_grid_crossings import iter_grid_candidates
from track.v2.models import PointRef, RefinedPointRef


def _segments():
    return {
        "segment_identity": (
            ("a", 1, 0, 10, 1, 20),
            ("b", 2, 1, 21, 0, 11),
            ("c", 3, 0, 12, 0, 13),
        ),
        "vehicle_id": ("a", "b", "c"),
        "start_point_kind": ("kept", "injected", "kept"),
        "start_point_id": np.array([10, 21, 12], dtype=np.int64),
        "subtrack_index": np.array([1, 2, 3], dtype=np.int64),
        "start_refined_sequence_in_subtrack": np.array([0, 3, 4], dtype=np.int64),
        "start_source_id": ("public.raw", "public.injected_points", "public.raw"),
        "start_source_row_key": ("raw-10", None, "raw-12"),
        "start_generated_reason": (None, "orthogonal_heading", None),
        "timestamp": np.array([10.0, 12.0, 14.0]),
        "heading": np.array([90.0, 0.0, 180.0]),
        "speed": np.array([5.0, 6.0, 7.0]),
    }


def test_iter_grid_candidates_kept_and_injected_provenance():
    segments = _segments()
    identities = segments["segment_identity"]
    batches = [
        {
            "x": np.array([1.0]), "y": np.array([2.0]),
            "segment_index_a": np.array([0]), "segment_index_b": np.array([1]),
            "segment_identity_a": (identities[0],),
            "segment_identity_b": (identities[1],),
        },
        {
            "x": np.array([3.0]), "y": np.array([4.0]),
            "segment_index_a": np.array([1]), "segment_index_b": np.array([2]),
            "segment_identity_a": (identities[1],),
            "segment_identity_b": (identities[2],),
        },
    ]
    transformer = SimpleNamespace(inverse=lambda x, y: (x + 110.0, y + 20.0))

    result = list(iter_grid_candidates(segments, batches, transformer))

    assert [key for key, _candidate in result] == [
        (identities[0], identities[1]), (identities[1], identities[2]),
    ]
    first = result[0][1]
    assert first.point_ref == PointRef("public.raw", "raw-10", True)
    assert first.from_segment_start_point_ref == first.point_ref
    assert first.to_segment_start_point_ref == PointRef(
        "public.injected_points", "21", False,
        generated_reason="orthogonal_heading",
    )
    assert first.from_segment_start == RefinedPointRef("kept", 10, 1, 0)
    assert first.to_segment_start == RefinedPointRef("injected", 21, 2, 3)
    assert (first.vehicle_id, first.timestamp, first.lon, first.lat,
            first.x, first.y, first.heading, first.speed,
            first.cross_track_calculate_index) == (
        "a|b", 11.0, 111.0, 22.0, 1.0, 2.0, 90.0, 5.0, -1,
    )
    second = result[1][1]
    assert second.point_ref == PointRef(
        "public.injected_points", "21", False,
        generated_reason="orthogonal_heading",
    )
    assert second.to_segment_start_point_ref == PointRef(
        "public.raw", "raw-12", True,
    )
    assert second.vehicle_id == "b|c"
    assert second.timestamp == 13.0
    assert second.heading == 0.0 and second.speed == 6.0
