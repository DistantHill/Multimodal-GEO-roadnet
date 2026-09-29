from dataclasses import replace
import numpy as np
import pytest
from .config_factory import intersect_config,runtime_config
from .detect_columns_helpers import CONTEXT,TRANSFORMER,subtrack,sql_turns_from_subtrack
from track.v2.candidates.collect_candidates import extract_cross_track_candidates
from track.v2.candidates.turn_event_relation import TurnEventRelation
from track.v2.candidates.cross_track_relation import CrossCandidateRelation
from track.v2.intersections.detect_intersections import _compute_getis_ord_g_star,_cluster_pipeline,_run_dbscan
from track.v2.intersections.extract_topology import _extract_entry_exit_observations_batch
from track.v2.models import HotspotCluster


def test_same_vehicle_distinct_run_subtracks_can_cross():
    a=subtrack([[-10,0],[10,0]],subtrack_index=701,headings=[90,90],keys=[0,-1])
    b=subtrack([[0,-10],[0,10]],subtrack_index=902,headings=[0,0])
    result,_,_=extract_cross_track_candidates({701:a,902:b},TRANSFORMER,intersect_config())
    assert len(result)==1
    assert result.from_subtrack_index.tolist()==[701]
    assert result.to_subtrack_index.tolist()==[902]
    assert result.from_vehicle_id.tolist()==result.to_vehicle_id.tolist()==["original_vehicle"]
    assert result.from_source_row_key.tolist()==[0]
    assert result.from_source_row_key_valid.tolist()==[True]
    assert np.array_equal(result.xy,[[0,0]])
    assert not result.xy.flags.writeable


def test_same_subtrack_segments_are_excluded():
    a=subtrack([[-10,0],[10,0],[0,-10],[0,10]],subtrack_index=701,headings=[90,90,0,0])
    result,_,_=extract_cross_track_candidates({701:a},TRANSFORMER,intersect_config(cross_track_straightness_deg=180))
    assert len(result)==0


@pytest.mark.parametrize("batch_size",[1,2,10])
def test_turn_relation_roundtrip_preserves_numeric_keys_masks_and_identity(tmp_path,batch_size):
    track=subtrack([[0,0],[0,20],[20,20],[40,20]],subtrack_index=701,headings=[0,90,90,90],keys=[-1,0,1,2])
    batch=sql_turns_from_subtrack(track,intersect_config(turn_window=1,min_leg_m=1),first_index=0)
    assert len(batch)==1
    assert batch.source_row_key.tolist()==[0]
    with TurnEventRelation(runtime_config(),tmp_path.resolve(),batch_size=batch_size) as relation:
        relation.append_batch(batch); relation.seal()
        read=list(relation.iter_batches())
        assert len(relation)==1 and len(read)==1
        assert read[0].calculation_index.tolist()==[0]
        assert read[0].subtrack_index.tolist()==[701]
        assert read[0].source_row_key.dtype==np.int64
        assert read[0].source_row_key_valid.tolist()==[True]
        assert not read[0].xy.flags.writeable
        with pytest.raises(RuntimeError,match="sealed"): relation.append_batch(batch)


def test_cross_relation_reads_batches_without_candidate_objects(tmp_path):
    a=subtrack([[-10,0],[10,0]],subtrack_index=3,headings=[90,90]); b=subtrack([[0,-10],[0,10]],subtrack_index=11)
    batch=extract_cross_track_candidates({3:a,11:b},TRANSFORMER,intersect_config())[0]
    with CrossCandidateRelation(runtime_config(),tmp_path.resolve(),batch_size=1) as relation:
        relation.append_batch(batch); relation.seal()
        read=list(relation.iter_batches(indices=np.asarray([0],dtype=np.int64)))
        assert len(read)==1 and read[0].from_subtrack_index.tolist()==[3]
        assert np.array_equal(read[0].xy,[[0,0]])


def test_circle_two_roots_have_cluster_local_ids_and_complete_observation():
    track=subtrack([[-20,0],[20,0]],subtrack_index=901,headings=[90,90])
    cluster=HotspotCluster(17,0,0,0,0,5,cluster_ref="cluster")
    obs,cross=_extract_entry_exit_observations_batch({901:track},cluster=cluster,transformer=TRANSFORMER,radius_m=5,first_crossing_index=8)
    assert cross.calculation_index.tolist()==[8,9]
    assert cross.subtrack_index.tolist()==[901,901]
    assert cross.is_entry.tolist()==[True,False]
    assert cross.segment_fraction.tolist()==[.375,.625]
    assert obs.entry_crossing_calculate_index.tolist()==[8]
    assert obs.exit_crossing_calculate_index.tolist()==[9]
    assert obs.subtrack_index.tolist()==[901]


def test_circle_tangent_does_not_make_crossing():
    track=subtrack([[-20,5],[20,5]])
    cluster=HotspotCluster(0,0,0,0,0,5)
    obs,cross=_extract_entry_exit_observations_batch({7:track},cluster=cluster,transformer=TRANSFORMER,radius_m=5)
    assert len(obs)==len(cross)==0


def test_dbscan_core_border_and_noise_columns():
    labels=_run_dbscan(np.asarray([0.,1.,2.,20.]),np.zeros(4),eps_m=1,min_samples=2)
    assert labels==[0,0,0,-1]


@pytest.mark.parametrize("headings,expected",[([0.,90.,90.,90.],1),([0.,0.,0.,0.],0)])
def test_injected_point_participates_only_when_turn_checks_pass(headings,expected):
    track=subtrack([[0.,0.],[0.,20.],[20.,20.],[40.,20.]],headings=headings)
    track=replace(track,kind=np.asarray(["p","i","p","p"]),source_row_key_valid=np.asarray([True,False,True,True]),generated_reason=np.asarray(["","orthogonal","",""]),after_sequence=np.asarray([-1,0,-1,-1],dtype=np.int64))
    result=sql_turns_from_subtrack(track,intersect_config(turn_window=1,min_leg_m=1))
    assert len(result)==expected
    if expected:
        assert result.point_kind.tolist()==["i"]
        assert result.source_row_key_valid.tolist()==[False]
        assert result.refine_point_id_valid.tolist()==[True]
        assert result.refine_point_id.tolist()==[701]
        assert result.generated_reason.tolist()==["orthogonal"]
