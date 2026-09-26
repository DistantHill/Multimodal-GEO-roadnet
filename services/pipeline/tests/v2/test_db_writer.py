from __future__ import annotations
from types import SimpleNamespace as NS
import numpy as np
import pytest
from track.v2.db.write_detect_results import WriterCrossingIDs
from track.v2.db.write_refine_results import finish_refine_run
from track.v2.models import CrossingBatch, TurnBatch, CrossCandidateBatch
from .writer_relation_helpers import _batch


def member_columns(kinds, ids, tracks, sequences):
    return {"point_kind":np.asarray(kinds),"point_id":np.asarray(ids,dtype=np.int64),
            "subtrack_index":np.asarray(tracks,dtype=np.int64),"refined_sequence":np.asarray(sequences,dtype=np.int64)}


def crossing_batch(indices, *, ids, tracks, sequences, fractions, entries, kinds=None):
    size=len(indices)
    return _batch(CrossingBatch,size,calculation_index=np.asarray(indices,dtype=np.int64),
        refine_point_id=np.asarray(ids,dtype=np.int64),subtrack_index=np.asarray(tracks,dtype=np.int64),
        point_index=np.asarray(sequences,dtype=np.int64),point_kind=np.asarray(kinds or ["p"]*size),
        segment_fraction=np.asarray(fractions,dtype=np.float64),is_entry=np.asarray(entries,dtype=np.bool_))


def crossing_ids(clusters, indices, ids):
    order=np.lexsort((indices,clusters))
    return WriterCrossingIDs(np.asarray(clusters,dtype=np.int64)[order],
        np.asarray(indices,dtype=np.int64)[order],np.asarray(ids,dtype=np.int64)[order])


def empty_result():
    return NS(turn_events=_batch(TurnBatch,0),cross_track_candidates=_batch(CrossCandidateBatch,0))


# Independent small-fixture ordering oracle: SQL rows exist only at I/O boundary.
def _build_resolved_rows(detect_id,members,refined_ids,complete_crossings,crossing_resolved_ids,ids):
    by_subtrack={}
    for i,resolved_id in enumerate(refined_ids):
        track=int(members["subtrack_index"][i]); sequence=int(members["refined_sequence"][i])
        point_id=int(members["point_id"][i]); kept=members["point_kind"][i]=="kept"
        by_subtrack.setdefault(track,[]).append(((float(sequence),1,-1,-1,resolved_id),
            (resolved_id,detect_id,track,sequence,point_id if kept else None,None if kept else point_id,None)))
    position=0
    for cluster,batch in complete_crossings:
        for i in range(len(batch)):
            if not batch.refine_point_id_valid[i]: raise ValueError("a persisted crossing requires a stable segment start")
            resolved_id=crossing_resolved_ids[position]; position+=1
            track=int(batch.subtrack_index[i]); fraction=float(batch.segment_fraction[i])
            crossing_id=ids.resolve(cluster.cluster_calculate_index,int(batch.calculation_index[i]))
            key=(int(batch.point_index[i])+fraction,0 if fraction>=1-1e-12 else 2,
                 int(batch.is_entry[i]),cluster.cluster_calculate_index,crossing_id)
            by_subtrack.setdefault(track,[]).append((key,(resolved_id,detect_id,track,None,None,None,crossing_id)))
    return [(*row[:4],sequence,*row[4:]) for track in sorted(by_subtrack)
            for sequence,(_,row) in enumerate(sorted(by_subtrack[track],key=lambda item:item[0]))]


def test_resolved_rows_order_four_crossings_on_one_refined_segment():
    members=member_columns(["kept","kept"],[101,102],[1,1],[0,1])
    complete=[(NS(cluster_calculate_index=0),crossing_batch([0,1],ids=[101,101],tracks=[1,1],sequences=[0,0],fractions=[.2,.4],entries=[True,False])),
              (NS(cluster_calculate_index=1),crossing_batch([0,1],ids=[101,101],tracks=[1,1],sequences=[0,0],fractions=[.6,.8],entries=[True,False]))]
    rows=_build_resolved_rows("detect",members,(301,302),complete,(303,304,305,306),crossing_ids([0,0,1,1],[0,1,0,1],[201,202,203,204]))
    assert [row[0] for row in rows]==[301,303,304,305,306,302]
    assert [row[4] for row in rows]==list(range(6))
    assert [row[3] for row in rows]==[0,None,None,None,None,1]
    assert [row[7] for row in rows]==[None,201,202,203,204,None]


def test_resolved_rows_place_t1_before_endpoint_and_t0_after_start():
    batch=crossing_batch([0,1],ids=[1,1],tracks=[7,7],sequences=[0,0],fractions=[0.,1.],entries=[True,False])
    rows=_build_resolved_rows("detect",member_columns(["kept","kept"],[1,2],[7,7],[0,1]),(10,11),
        [(NS(cluster_calculate_index=0),batch)],(12,13),crossing_ids([0,0],[0,1],[20,21]))
    assert [row[0] for row in rows]==[10,12,13,11]


def test_same_progress_crossings_sort_exit_before_entry():
    complete=[(NS(cluster_calculate_index=1),crossing_batch([0],ids=[1],tracks=[1],sequences=[0],fractions=[.5],entries=[True])),
              (NS(cluster_calculate_index=0),crossing_batch([0],ids=[1],tracks=[1],sequences=[0],fractions=[.5],entries=[False]))]
    rows=_build_resolved_rows("detect",member_columns(["kept","kept"],[1,2],[1,1],[0,1]),(1,2),complete,(3,4),crossing_ids([1,0],[0,0],[31,30]))
    assert [row[7] for row in rows]==[None,30,31,None]


def test_refine_key_uses_stable_internal_identity_not_sequence(tmp_path):
    from .writer_relation_helpers import prepared_turn_relation, turn_batch
    result=NS(turn_events=turn_batch(2,refine_point_id=np.asarray([8,8],dtype=np.int64),
        subtrack_index=np.asarray([4,4],dtype=np.int64),point_index=np.asarray([2,99],dtype=np.int64)),hotspot_events=turn_batch(0))
    with prepared_turn_relation(result,{("kept",8,4):15},{},batch_size=1,data_root=tmp_path) as relation:
        assert list(relation.iter_resolved_sources(0))==[(0,0,15),(1,0,15)]


def test_completed_refine_requires_a_source_fingerprint():
    with pytest.raises(ValueError,match="requires source_fingerprint"):
        finish_refine_run(object(),"refine",status="completed")
