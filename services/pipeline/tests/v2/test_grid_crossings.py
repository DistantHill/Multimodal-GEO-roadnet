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
        "speed": np.full(len(values), 20.), "end_speed": np.full(len(values), 20.),
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
        cross_track_min_avg_speed=20.0,
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


@pytest.mark.parametrize("batch_size", [1, 2, 10])
def test_membership_fallback_completes_known_crossings(batch_size):
    cfg=_cfg()
    cfg.cross_track_pair_batch_size=batch_size
    batches=list(iter_grid_crossings(_segments(),cfg,max_grid_memberships=0,max_overflow_pair_space=0))
    assert all(len(batch['x'])<=batch_size for batch in batches)
    assert sorted((float(x),float(y)) for batch in batches for x,y in zip(batch['x'],batch['y']))==[(1.,0.),(1.5,0.)]
    pairs=[(int(a),int(b)) for batch in batches for a,b in zip(batch['segment_index_a'],batch['segment_index_b'])]
    assert sorted(pairs)==[(0,2),(0,5)]


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


@pytest.mark.parametrize("angle_range", [(0., 180.), (80., 100.), (0., 0.), (180., 180.)])
@pytest.mark.parametrize("batch_size", [1, 7, 65536])
@pytest.mark.parametrize("memberships,max_cells", [(0, 1), (100000, 1), (100000, 1000)])
def test_filtered_grid_pairs_match_exhaustive_geometry(batch_size, memberships, max_cells, angle_range):
    from track.v2._geometry import _paired_segment_intersections
    from track.v2.candidates.cross_track_timing import CrossTrackTiming

    rng = np.random.default_rng(882)
    n = 70
    start = rng.uniform(-8, 8, (n, 2))
    end = rng.uniform(-8, 8, (n, 2))
    # Coincident minima/endpoints, zero-length segments, angle-bin boundaries,
    # negative/large headings, same subtrack and same vehicle are intentional.
    start[:8] = 0
    end[:2] = 0
    headings = np.resize(np.array([-360., -10., 0., 10., 45., 80., 90., 100.,
                                   135., 180., 350., 360., 1e20]), n)
    partitions = rng.integers(0, 10, n)
    segments = {
        "speed": np.full(n, 20.), "end_speed": np.full(n, 20.),
        "vehicle_id": np.full(n, "same", dtype=object),
        "subtrack_index": partitions,
        "segment_identity": np.column_stack((partitions, np.zeros(n, dtype=int),
                                               np.arange(n), np.zeros(n, dtype=int), np.arange(n)+1)),
        "x_start": start[:, 0], "y_start": start[:, 1],
        "x_end": end[:, 0], "y_end": end[:, 1],
        "heading": headings, "end_heading": headings,
    }
    cfg = _cfg()
    cfg.cross_track_pair_batch_size = batch_size
    cfg.cross_track_min_angle_deg, cfg.cross_track_max_angle_deg = angle_range
    cfg.cross_track_grid_cell_m = 4.
    cfg.cross_track_max_grid_cells_per_segment = max_cells
    a, b = np.triu_indices(n, 1)
    keep = partitions[a] != partitions[b]
    a, b = a[keep], b[keep]
    swap = partitions[a] > partitions[b]
    a, b = np.where(swap, b, a), np.where(swap, a, b)
    x, y, hit = _paired_segment_intersections(start[a], end[a], headings[a],
        start[b], end[b], headings[b], cfg.cross_track_min_angle_deg, cfg.cross_track_max_angle_deg)
    expected = {(int(a[i]), int(b[i])): (xx, yy) for i, xx, yy in zip(hit, x, y)}
    timing = CrossTrackTiming()
    batches = list(iter_grid_crossings(segments, cfg, max_grid_memberships=memberships,
                                      max_overflow_pair_space=0, timing=timing))
    actual = {}
    for batch in batches:
        assert len(batch['x']) <= batch_size
        for a, b, x, y in zip(batch['segment_index_a'], batch['segment_index_b'], batch['x'], batch['y']):
            assert (int(a), int(b)) not in actual
            actual[int(a), int(b)] = (x, y)
    assert actual == expected
    assert timing.values['hit_count'] == len(expected)
    assert timing.values['pair_count'] >= len(expected)


def test_parallel_dense_bucket_is_rejected_before_pair_enumeration():
    from track.v2.candidates.cross_track_timing import CrossTrackTiming
    n = 10000
    segments = {
        "speed": np.full(n, 20.), "end_speed": np.full(n, 20.),
        "vehicle_id": np.full(n, "same", dtype=object),
        "subtrack_index": np.arange(n),
        "segment_identity": np.zeros((n, 5), dtype=np.int64),
        "x_start": np.zeros(n), "y_start": np.zeros(n),
        "x_end": np.ones(n), "y_end": np.zeros(n),
        "heading": np.full(n, 90.), "end_heading": np.full(n, 90.),
    }
    cfg = _cfg()
    cfg.cross_track_pair_batch_size = 10000
    timing = CrossTrackTiming()
    assert list(iter_grid_crossings(segments, cfg, max_grid_memberships=100000,
                                   max_overflow_pair_space=0, timing=timing)) == []
    assert timing.values.get('bbox_query_pairs', 0) == 0
    assert timing.values['pair_count'] == 0
