from types import SimpleNamespace

import numpy as np
import pytest

from track.v2.candidates.build_patches import CrossTrackCapacityError
from track.v2.candidates.detect_grid_crossings import iter_grid_crossings


def _segments():
    # Horizontal a crosses vertical b and e. The vertical a shares its
    # subtrack and is excluded; c is parallel and away; d fails straightness.
    values = (
        ("a", 0.0, 0.0, 2.0, 0.0, 90.0, 90.0),
        ("a", 0.5, -1.0, 0.5, 1.0, 0.0, 0.0),
        ("b", 1.0, -1.0, 1.0, 1.0, 0.0, 0.0),
        ("c", 0.0, 3.0, 2.0, 3.0, 90.0, 90.0),
        ("d", 1.25, -1.0, 1.25, 1.0, 0.0, 90.0),
        ("e", 1.5, -1.0, 1.5, 1.0, 0.0, 0.0),
    )
    return {
        "vehicle_id": np.asarray([row[0] for row in values],dtype=object),
        "subtrack_index": np.asarray([0,0,1,2,3,4],dtype=np.int64),
        "segment_identity": np.column_stack((np.asarray([0,0,1,2,3,4],dtype=np.int64),
            np.zeros(len(values),dtype=np.int64),np.arange(len(values),dtype=np.int64)*2,
            np.zeros(len(values),dtype=np.int64),np.arange(len(values),dtype=np.int64)*2+1)),
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
        (tuple(batch["segment_identity_a"][0]), tuple(batch["segment_identity_b"][0])):
        (float(batch["x"][0]), float(batch["y"][0]))
        for batch in batches
    }
    identities = segments["segment_identity"]
    assert hits == {
        (tuple(identities[0]), tuple(identities[2])): (1.0, 0.0),
        (tuple(identities[0]), tuple(identities[5])): (1.5, 0.0),
    }
    assert {int(batch["segment_index_a"][0]) for batch in batches} == {0}
    assert {int(batch["segment_index_b"][0]) for batch in batches} == {2, 5}


def test_iter_grid_crossings_preserves_bucket_capacity_error():
    with pytest.raises(CrossTrackCapacityError):
        list(iter_grid_crossings(
            _segments(), _cfg(), max_grid_memberships=0,
            max_overflow_pair_space=0,
        ))


def test_same_vehicle_different_subtracks_are_exact_crossing_participants():
    segments=_segments()
    segments['subtrack_index'][1]=5
    segments['segment_identity'][1,0]=5
    batches=list(iter_grid_crossings(segments,_cfg(),max_grid_memberships=1000,max_overflow_pair_space=1000))
    assert sum(len(batch['x']) for batch in batches)==3
    assert sorted((float(x),float(y)) for batch in batches for x,y in zip(batch['x'],batch['y']))==[(.5,0.),(1.,0.),(1.5,0.)]
    same_vehicle=[batch for batch in batches if {int(batch['segment_index_a'][0]),int(batch['segment_index_b'][0])}=={0,1}]
    assert len(same_vehicle)==1
    assert segments['vehicle_id'][0]==segments['vehicle_id'][1]=='a'
    assert segments['subtrack_index'][0]!=segments['subtrack_index'][1]


def test_same_subtrack_is_excluded_independently_of_vehicle_identity():
    segments=_segments()
    segments['subtrack_index'][2]=0
    segments['segment_identity'][2,0]=0
    batches=list(iter_grid_crossings(segments,_cfg(),max_grid_memberships=1000,max_overflow_pair_space=1000))
    assert sum(len(batch['x']) for batch in batches)==1
    assert batches[0]['segment_index_a'].tolist()==[0]
    assert batches[0]['segment_index_b'].tolist()==[5]
    assert segments['vehicle_id'][0]!=segments['vehicle_id'][2]
