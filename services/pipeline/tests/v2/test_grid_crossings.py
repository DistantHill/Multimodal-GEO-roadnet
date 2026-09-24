from types import SimpleNamespace

import numpy as np
import pytest

from track.v2.candidate_patches import CrossTrackCapacityError
from track.v2.grid_cross_track_candidates import iter_grid_crossings


def _segments():
    # Horizontal a crosses vertical b and e. The same-car vertical a is
    # excluded; c is parallel and away; d fails the straightness threshold.
    values = (
        ("a", 0.0, 0.0, 2.0, 0.0, 90.0, 90.0),
        ("a", 0.5, -1.0, 0.5, 1.0, 0.0, 0.0),
        ("b", 1.0, -1.0, 1.0, 1.0, 0.0, 0.0),
        ("c", 0.0, 3.0, 2.0, 3.0, 90.0, 90.0),
        ("d", 1.25, -1.0, 1.25, 1.0, 0.0, 90.0),
        ("e", 1.5, -1.0, 1.5, 1.0, 0.0, 0.0),
    )
    return {
        "vehicle_id": tuple(row[0] for row in values),
        "segment_identity": tuple((row[0], 1, 0, i * 2, 0, i * 2 + 1)
                                  for i, row in enumerate(values)),
        **{
            name: np.asarray([row[position] for row in values], dtype=np.float64)
            for name, position in (
                ("x_start", 1), ("y_start", 2),
                ("x_end", 3), ("y_end", 4),
                ("heading", 5), ("end_heading", 6),
            )
        },
    }


def _cfg():
    return SimpleNamespace(
        cross_track_straightness_deg=10.0,
        cross_track_min_angle_deg=45.0,
        cross_track_max_angle_deg=135.0,
        cross_track_grid_cell_m=1.0,
        cross_track_max_grid_cells_per_segment=64,
        cross_track_pair_batch_size=1,
    )


def test_iter_grid_crossings_batches_only_cross_vehicle_exact_hits():
    segments = _segments()
    batches = list(iter_grid_crossings(
        segments, _cfg(), max_grid_memberships=1000,
        max_overflow_pair_space=1000,
    ))

    assert len(batches) == 2
    assert all(len(batch["x"]) == 1 for batch in batches)
    hits = {
        (batch["segment_identity_a"][0], batch["segment_identity_b"][0]):
        (float(batch["x"][0]), float(batch["y"][0]))
        for batch in batches
    }
    identities = segments["segment_identity"]
    assert hits == {
        (identities[0], identities[2]): (1.0, 0.0),
        (identities[0], identities[5]): (1.5, 0.0),
    }
    assert {int(batch["segment_index_a"][0]) for batch in batches} == {0}
    assert {int(batch["segment_index_b"][0]) for batch in batches} == {2, 5}


def test_iter_grid_crossings_preserves_bucket_capacity_error():
    with pytest.raises(CrossTrackCapacityError):
        list(iter_grid_crossings(
            _segments(), _cfg(), max_grid_memberships=0,
            max_overflow_pair_space=0,
        ))
