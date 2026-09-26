"""Patch boundaries keep business columns and Run identities intact."""
from dataclasses import fields
import numpy as np
import pytest
from .config_factory import intersect_config
from .detect_columns_helpers import subtrack,TRANSFORMER
from track.v2.candidates.build_patches import iter_track_patches,build_cross_track_index,iter_cross_track_patches,CrossTrackCapacityError
from track.v2.candidates.collect_candidates import extract_turn_candidates,extract_cross_track_candidates


def test_track_patches_preserve_complete_subtracks_and_tail():
    tracks={i:subtrack([[0,0],[1,1]],subtrack_index=i) for i in range(1,6)}
    seen=[]
    def inputs():
        for key,value in tracks.items(): seen.append(key); yield key,value
    patches=iter_track_patches(inputs(),2)
    first=next(patches)
    assert seen==[1,2] and first[1] is tracks[1]
    assert [len(first),*[len(value) for value in patches]]==[2,2,1]
    assert len(first)==2


@pytest.mark.parametrize("batch_size",[1,2,5,100])
def test_turn_patch_boundaries_preserve_global_calculation_identity(batch_size):
    tracks={i:subtrack([[0,0],[0,20],[20,20]],subtrack_index=i,headings=[0,90,90]) for i in range(1,6)}
    batch=extract_turn_candidates(iter_track_patches(tracks.items(),batch_size),intersect_config(turn_window=1,min_leg_m=1))
    assert batch.calculation_index.tolist()==list(range(5))
    assert batch.subtrack_index.tolist()==list(range(1,6))
    assert batch.vehicle_id.tolist()==["original_vehicle"]*5


@pytest.mark.parametrize("batch_size",[1,2,65536])
@pytest.mark.parametrize("cell_m",[10.,100.,1000.])
def test_grid_and_overflow_keep_known_same_vehicle_crossing(batch_size,cell_m):
    tracks={1:subtrack([[-2000,0],[2000,0]],subtrack_index=1,headings=[90,90]),2:subtrack([[0,-2000],[0,2000]],subtrack_index=2)}
    batch=extract_cross_track_candidates(tracks,TRANSFORMER,intersect_config(cross_track_pair_batch_size=batch_size,cross_track_grid_cell_m=cell_m,cross_track_max_grid_cells_per_segment=1))[0]
    assert len(batch)==1 and np.array_equal(batch.xy,[[0,0]])
    assert batch.from_subtrack_index.tolist()==[1] and batch.to_subtrack_index.tolist()==[2]


def test_index_holds_only_numeric_segment_spans_and_capacity_fails_before_grid():
    starts=np.asarray([[-20.,0],[0,-20.]])
    ends=-starts
    index=build_cross_track_index(starts,ends,np.asarray([1,2],dtype=np.int64),cell_m=10,max_cells=100)
    assert index.spans.shape==(2,4) and index.spans.dtype==np.int64
    assert sum(len(a) for a,b in iter_cross_track_patches(index,1))==1
    with pytest.raises(CrossTrackCapacityError,match="memberships"):
        build_cross_track_index(starts,ends,np.asarray([1,2],dtype=np.int64),cell_m=10,max_cells=100,max_grid_memberships=0)


def test_nonfinite_segment_is_excluded_from_exact_pairing():
    starts=np.asarray([[np.nan,0],[0,-20.]])
    index=build_cross_track_index(starts,np.asarray([[20.,0],[0,20.]]),np.asarray([1,2],dtype=np.int64),cell_m=10,max_cells=1)
    assert list(iter_cross_track_patches(index,1))==[]


def test_consumed_complete_patch_is_released_before_next_subtrack_build():
    import gc
    import weakref
    from track.v2.candidates.collect_candidates import extract_turn_candidates
    references=[]
    def tracks():
        for index in range(2):
            gc.collect()
            assert all(reference() is None for reference in references)
            track=subtrack([[0.,0.],[0.,20.],[20.,20.]],subtrack_index=index,headings=[0.,90.,90.])
            references.append(weakref.ref(track.xy))
            yield index,track
            del track
    result=extract_turn_candidates(iter_track_patches(tracks(),1),intersect_config(turn_window=1,min_leg_m=1))
    assert len(result)==2
    gc.collect()
    assert all(reference() is None for reference in references)
