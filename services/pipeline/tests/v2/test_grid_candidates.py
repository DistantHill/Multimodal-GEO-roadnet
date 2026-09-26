
import numpy as np

from track.v2.candidates.detect_grid_crossings import iter_grid_candidates


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
        "start_source_row_key": np.array([0, 0, -1],dtype=np.int64),
        "start_generated_reason": ("", "orthogonal_heading", ""),
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
    class OffsetTransformer:
        def inverse_many(self, xy):
            return xy + np.array([110.,20.])
    result = list(iter_grid_candidates(segments, batches, OffsetTransformer()))
    assert len(result)==2 and all(len(batch)==1 for batch in result)
    first,second=result
    assert first.from_source_id.tolist()==["public.raw"]
    assert first.to_source_id.tolist()==["public.injected_points"]
    assert first.from_source_row_key.tolist()==[0]
    assert first.from_source_row_key_valid.tolist()==[True]
    assert first.to_source_row_key_valid.tolist()==[False]
    assert first.to_generated_reason.tolist()==["orthogonal_heading"]
    assert first.from_point_kind.tolist()==["p"]
    assert first.to_point_kind.tolist()==["i"]
    assert first.from_refine_point_id.tolist()==[10]
    assert first.to_refine_point_id.tolist()==[21]
    assert first.from_subtrack_index.tolist()==[1]
    assert first.to_subtrack_index.tolist()==[2]
    assert first.from_point_index.tolist()==[0]
    assert first.to_point_index.tolist()==[3]
    assert first.from_vehicle_id.tolist()==["a"]
    assert first.to_vehicle_id.tolist()==["b"]
    assert first.timestamp.tolist()==[11.]
    assert first.lonlat.tolist()==[[111.,22.]]
    assert first.xy.tolist()==[[1.,2.]]
    assert first.heading.tolist()==[90.] and first.speed.tolist()==[5.]
    assert first.calculation_index.tolist()==[0]
    assert second.from_source_id.tolist()==["public.injected_points"]
    assert second.from_source_row_key_valid.tolist()==[False]
    assert second.to_source_id.tolist()==["public.raw"]
    assert second.to_source_row_key.tolist()==[-1]
    assert second.to_source_row_key_valid.tolist()==[True]
    assert second.from_vehicle_id.tolist()==["b"] and second.to_vehicle_id.tolist()==["c"]
    assert second.timestamp.tolist()==[13.]
    assert second.heading.tolist()==[0.] and second.speed.tolist()==[6.]
