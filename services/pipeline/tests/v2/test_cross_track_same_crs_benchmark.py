"""Known same-CRS crossing geometry and original vehicle identity."""
import numpy as np
from .config_factory import intersect_config
from .detect_columns_helpers import subtrack, TRANSFORMER
from track.v2.candidates.collect_candidates import extract_cross_track_candidates


def test_complete_stage_preserves_subtrack_keys_and_vehicle_identity():
    tracks = {1: subtrack([[-100, -100], [100, -100]], subtrack_index=1, vehicle_id="A", headings=[90, 90]),
              2: subtrack([[-100, 100], [100, 100]], subtrack_index=2, vehicle_id="A", headings=[90, 90]),
              3: subtrack([[0, -200], [0, 200]], subtrack_index=3, vehicle_id="B", headings=[0, 0])}
    candidates, eligible, scheduled = extract_cross_track_candidates(tracks, TRANSFORMER, intersect_config())
    assert eligible == scheduled == 3
    assert candidates.from_vehicle_id.tolist() == ["A", "A"]
    assert candidates.to_vehicle_id.tolist() == ["B", "B"]
    assert candidates.from_subtrack_index.tolist() == [1, 2]
    assert candidates.to_subtrack_index.tolist() == [3, 3]
    np.testing.assert_array_equal(candidates.xy, [[0, -100], [0, 100]])
    np.testing.assert_array_equal(candidates.lonlat, TRANSFORMER.inverse_many(candidates.xy))
    assert candidates.calculation_index.tolist() == [0, 1]
