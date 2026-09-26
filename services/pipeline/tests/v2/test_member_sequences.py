import numpy as np
from track.v2.models import MemberBatch
from track.v2.intersections.member_sequences import concatenate_members,member_source_counts,member_source_ids


def test_members_keep_namespace_and_independent_order():
    first=MemberBatch(np.asarray(["turn","turn"]),np.asarray([8,2],dtype=np.int64),np.asarray([0,1],dtype=np.int64),np.asarray(["a","b"]),np.asarray([True,False]))
    second=MemberBatch(np.asarray(["orthogonal_cross_track"]),np.asarray([8],dtype=np.int64),np.asarray([0],dtype=np.int64),np.asarray(["c"]),np.asarray([True]))
    combined=concatenate_members([first,second])
    assert combined.calculation_index.tolist()==[8,2,8]
    assert combined.member_order.tolist()==[0,1,2]
    assert member_source_counts(combined)=={"turn":2,"orthogonal_cross_track":1}
    assert member_source_ids(combined)==("a","c")
    assert not combined.member_order.flags.writeable


def test_empty_members_have_typed_columns():
    empty=concatenate_members([])
    assert len(empty)==0 and empty.calculation_index.dtype==np.int64


def test_array_selection_preserves_reverse_slice_and_duplicate_semantics():
    members=MemberBatch(np.asarray(["turn","orthogonal_cross_track","turn"]),np.asarray([8,8,2],dtype=np.int64),np.asarray([0,1,2],dtype=np.int64),np.asarray(["a","b","c"]),np.asarray([True,True,True]))
    reverse=members.take(np.asarray([2,1,0],dtype=np.int64))
    assert reverse.calculation_index.tolist()==[2,8,8]
    assert reverse.kind.tolist()==["turn","orthogonal_cross_track","turn"]
    assert reverse.member_order.tolist()==[2,1,0]
    duplicate=members.take(np.asarray([1,1],dtype=np.int64))
    assert duplicate.kind.tolist()==["orthogonal_cross_track","orthogonal_cross_track"]
    assert duplicate.member_order.tolist()==[1,1]
