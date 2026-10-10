"""Cross column relation matches eager clustering and retains output ownership."""
from .detect_columns_helpers import cluster_candidate_batches
from contextlib import ExitStack
from dataclasses import replace
import gc
import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
import weakref
import numpy as np
import pytest
from .config_factory import intersect_config,intersect_run_config,runtime_config
from .detect_columns_helpers import CONTEXT,TRANSFORMER
from .writer_relation_helpers import _batch,turn_batch
from .test_cross_candidate_relation import concatenate,assert_batch_equal
from track.v2.models import CrossCandidateBatch,MemberBatch,HotspotCluster
from track.v2.candidates.cross_track_relation import CrossCandidateRelation
from track.v2.candidates.detect_turn_events import empty_turn_batch
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.intersections.relations.source_associations import WriterRelation
from track.v2.db.write_detect_results import _insert_tracks_cross
import track.v2.intersections.detect_intersections as detect


def candidates(count=17):
    index=np.arange(count,dtype=np.int64)
    x,y=TRANSFORMER.forward(116.,39.)
    xy=np.column_stack((x+(index//8)*30+(index%4)*2,np.full(count,y)))
    xy[-1]=[x+10000,y]
    return _batch(CrossCandidateBatch,count,calculation_index=index,xy=xy,lonlat=TRANSFORMER.inverse_many(xy),
        from_source_id=np.full(count,'repeated'),to_source_id=np.full(count,'other'),
        from_source_row_key=index%3,from_refine_point_id=index*2+1,to_refine_point_id=index*2+2,
        from_subtrack_index=index+1,to_subtrack_index=index+count+1)


def turns_for(values,count=4):
    xy=values.xy[:count].copy()
    return turn_batch(count,xy=xy,lonlat=TRANSFORMER.inverse_many(xy),source_id=np.full(count,'turn-source'),source_row_key=np.arange(count,dtype=np.int64)%2)


def stored(tmp_path,values):
    spool=CrossCandidateRelation(runtime_config(),tmp_path,batch_size=3)
    spool.append_batch(values); spool.seal(); return spool


def serialized(clusters):
    return json.dumps([cluster.as_dict() for cluster in clusters],sort_keys=True)


def membership_rows(tmp_path,clusters,ids):
    with WriterRelation(runtime_config(),tmp_path,'membership',batch_size=3) as relation:
        relation.resolve_sources(); relation.prepare_annotations(clusters,ids,empty_turn_batch())
        return relation._connection.execute("SELECT kind,calculation_index,candidate_id FROM members QUALIFY row_number() OVER (PARTITION BY kind,calculation_index ORDER BY encounter)=1 ORDER BY kind,calculation_index").fetchall()


def eager_membership_rows(clusters,ids):
    mappings={}
    for cluster in clusters:
        members=cluster.members
        if members is None: continue
        for kind,index in zip(members.kind,members.calculation_index):
            kind='cross' if kind=='orthogonal_cross_track' else str(kind)
            key=(kind,int(index)); value=ids[cluster.cluster_calculate_index]
            if key in mappings and mappings[key]!=value: raise ValueError('belongs to multiple candidates')
            mappings[key]=value
    return sorted((*key,value) for key,value in mappings.items())


@pytest.mark.parametrize('merge_distance',[0.,30.,100.])
def test_complete_pipeline_matches_eager_order_refs_ablation_and_writer_membership(tmp_path,merge_distance):
    values=candidates(); turns=turns_for(values)
    cfg=intersect_config(g_star_min_z=-100.,g_star_radius_m=15.,candidate_dbscan_eps_m=2.,candidate_dbscan_min_samples=2,
        merge_centroid_distance_m=merge_distance)
    expected_hotspots,expected=cluster_candidate_batches(turns,values,TRANSFORMER,cfg)
    with stored(tmp_path,values) as spool:
        actual_hotspots,actual=cluster_candidate_batches(turns,spool,TRANSFORMER,cfg)
        from .test_turn_event_relation import assert_batch_equal as assert_turn_equal
        assert_turn_equal(actual_hotspots,expected_hotspots)
        assert serialized(actual)==serialized(expected)==serialized(actual)
        assert_batch_equal(concatenate(spool.iter_batches()),values) # noise remains in relation
        ids={cluster.cluster_calculate_index:100+i for i,cluster in enumerate(actual)}
        assert membership_rows(tmp_path,actual,ids)==membership_rows(tmp_path,expected,ids)==eager_membership_rows(expected,ids)
        for actual_cluster,expected_cluster in zip(actual,expected,strict=True):
            for name in ('kind','calculation_index','member_order','source_id','source_id_valid'):
                np.testing.assert_array_equal(getattr(actual_cluster.members,name),getattr(expected_cluster.members,name))


@pytest.mark.parametrize('count',[17,273])
def test_cross_dbscan_uses_same_kernel_and_compact_float_arrays(tmp_path,count):
    values=candidates(count)
    expected=detect._cluster_orthogonal_cross_track_candidates(values,TRANSFORMER,eps_m=2.,min_samples=2)
    real=detect._run_dbscan
    def compact(xs,ys,**kwargs):
        assert isinstance(xs,np.ndarray) and xs.dtype==np.float64 and xs.ndim==1
        assert isinstance(ys,np.ndarray) and ys.dtype==np.float64 and ys.ndim==1
        np.testing.assert_array_equal(xs,values.xy[:,0]);np.testing.assert_array_equal(ys,values.xy[:,1])
        return real(xs,ys,**kwargs)
    with stored(tmp_path,values) as spool,patch.object(detect,'_run_dbscan',side_effect=compact):
        actual=detect._cluster_orthogonal_cross_track_candidates(spool,TRANSFORMER,eps_m=2.,min_samples=2)
    assert serialized(actual)==serialized(expected)
    assert all(isinstance(cluster.members,MemberBatch) for cluster in actual)


def test_lazy_members_merge_and_provenance_do_not_retain_decoded_payloads(tmp_path):
    values=candidates(129); cfg=intersect_config(candidate_dbscan_eps_m=2.,candidate_dbscan_min_samples=2,
        merge_centroid_distance_m=100.)
    with stored(tmp_path,values) as spool:
        reference=weakref.ref(values.xy); del values; gc.collect(); assert reference() is None
        # Coordinate-only clustering must not read business payload batches.
        with patch.object(spool,'iter_batches',side_effect=AssertionError('business payload decoded')):
            clusters=detect._build_candidate_clusters((("orthogonal_cross_track",spool),),cfg,TRANSFORMER)
            turns=turn_batch(4,xy=np.tile([clusters[0].centroid_x,clusters[0].centroid_y],(4,1)))
            sources=(("turn",turns),("orthogonal_cross_track",spool))
            combined=detect._build_candidate_clusters(sources,cfg,TRANSFORMER)
            annotated=detect._annotate_source_ablation(combined,sources,cfg,TRANSFORMER)
            assert serialized(annotated)
        assert all(isinstance(cluster.members,MemberBatch) for cluster in clusters)


def test_manual_reference_source_ids_and_duplicate_order_survive_lazy_merge(tmp_path):
    values=candidates()
    with stored(tmp_path,values) as spool:
        lazy=detect._cluster_orthogonal_cross_track_candidates(spool,TRANSFORMER,eps_m=2.,min_samples=2)
        eager=detect._cluster_orthogonal_cross_track_candidates(values,TRANSFORMER,eps_m=2.,min_samples=2)
        first=lazy[0]
        members=MemberBatch(np.asarray(['turn','turn']),np.asarray([31,31],dtype=np.int64),np.asarray([0,1],dtype=np.int64),np.asarray(['manual-only','manual-only']),np.asarray([True,True]))
        manual=HotspotCluster(91,first.centroid_x,first.centroid_y,first.centroid_lon,first.centroid_lat,2.,members=members,
            source_ids=('manual-only',),source_counts={'turn':2},algorithm_sources=('turn',))
        actual=detect._merge_clusters([manual,*lazy],100.,TRANSFORMER)
        expected=detect._merge_clusters([manual,*eager],100.,TRANSFORMER)
        assert serialized(actual)==serialized(expected)
        assert 'manual-only' in actual[0].source_ids
        assert actual[0].members.source_id[actual[0].members.kind=='turn'].tolist()==['manual-only','manual-only']
        assert actual[0].members.calculation_index[actual[0].members.kind=='turn'].tolist()==[31,31]


@pytest.mark.parametrize('failure',[RuntimeError('ablation failed'),KeyboardInterrupt()])
def test_ablation_failure_closes_only_its_pass(tmp_path,failure):
    values=candidates();cfg=intersect_config(candidate_dbscan_eps_m=2.,candidate_dbscan_min_samples=2)
    with stored(tmp_path,values) as spool:
        main=detect._build_candidate_clusters((("orthogonal_cross_track",spool),),cfg,TRANSFORMER);before=serialized(main)
        mixed=[replace(main[0],algorithm_sources=('turn','orthogonal_cross_track'))]
        real=detect._build_candidate_clusters
        def fail(*args,**kwargs):
            kwargs['disabled_sources']={'turn'};real(*args,**kwargs);raise failure
        with patch.object(detect,'_build_candidate_clusters',side_effect=fail):
            with pytest.raises(type(failure)):detect._annotate_source_ablation(mixed,(("turn",empty_turn_batch()),("orthogonal_cross_track",spool)),cfg,TRANSFORMER)
        assert serialized(main)==before
        assert_batch_equal(concatenate(spool.iter_batches()),values)
        assert not spool._connection.execute("SELECT table_name FROM duckdb_tables() WHERE table_name LIKE '%member%'").fetchall()


def test_sorted_relation_to_result_writes_same_rows(tmp_path):
    cfg=intersect_config(cross_track_pair_batch_size=3,use_turn_events=False,candidate_dbscan_min_samples=1)
    expected=candidates(8)
    with stored(tmp_path,expected) as spool:
        _,eager_clusters=cluster_candidate_batches(empty_turn_batch(),expected,TRANSFORMER,cfg)
        _,lazy_clusters=cluster_candidate_batches(empty_turn_batch(),spool,TRANSFORMER,cfg)
        base=detect._empty_intersect_result(CONTEXT)
        result=replace(base,clusters=lazy_clusters,cross_track_candidates=spool)
        eager=replace(base,clusters=eager_clusters,cross_track_candidates=expected)
        ids={cluster.cluster_calculate_index:100+i for i,cluster in enumerate(lazy_clusters)}
        assert membership_rows(tmp_path,lazy_clusters,ids)==eager_membership_rows(eager_clusters,ids)
        keys=[('kept',int(value),int(track)) for values,tracks in ((expected.from_refine_point_id,expected.from_subtrack_index),(expected.to_refine_point_id,expected.to_subtrack_index)) for value,track in zip(values,tracks)]
        resolved={key:i+1 for i,key in enumerate(sorted(set(keys)))}
        expected_pairs=[tuple(sorted((resolved[keys[i]],resolved[keys[i+len(expected)]]))) for i in range(len(expected))]
        assert all(a!=b for a,b in expected_pairs) and len(expected_pairs)==len(set(expected_pairs))
        rows=[]
        for output in (result,eager):
            copied=[]
            def capture(cursor,table,columns,values,**kwargs): assert len(values)<=3;copied.extend(values)
            with WriterRelation(runtime_config(),tmp_path,'run',batch_size=3) as relation:
                relation.collect_sources(output,[],{},ids)
                relation.add_mappings(np.asarray([key[0] for key in resolved]),np.asarray([key[1] for key in resolved],dtype=np.int64),np.asarray([key[2] for key in resolved],dtype=np.int64),np.asarray(list(resolved.values()),dtype=np.int64))
                relation.resolve_sources();relation.prepare_annotations(output.clusters,ids,output.hotspot_events)
                with patch('track.v2.db.write_detect_results._copy_insert',side_effect=capture):
                    _insert_tracks_cross(NS(cursor=lambda:ExitStack()),'run',relation,batch_size=3)
            rows.append(copied)
        membership={index:value for kind,index,value in eager_membership_rows(eager_clusters,ids) if kind=='cross'}
        expected_rows=[('run',membership.get(i),*expected_pairs[i],*expected.xy[i],*expected.lonlat[i],None,None,None,None) for i in range(len(expected))]
        assert all(row[-4:] == (None,None,None,None) for row in rows[0])
        assert rows[0]==rows[1]==expected_rows
        assert json.loads(json.dumps(result.as_dict()))==json.loads(json.dumps(eager.as_dict()))
        result.close();result.close()
        assert not list(tmp_path.glob('duckdb/cross-event-relations/*'))
        with pytest.raises(RuntimeError):list(spool.iter_batches())
        # Members are independent published columns and remain readable after owning relation closes.
        assert lazy_clusters[0].members.calculation_index.size>0


@pytest.mark.parametrize('phase',['reader','clustering','topology'])
@pytest.mark.parametrize('failure',[RuntimeError('stage failed'),KeyboardInterrupt()])
def test_db_owner_closes_all_spools_on_failure(tmp_path,monkeypatch,phase,failure):
    cfg=intersect_run_config(use_turn_events=False,candidate_dbscan_min_samples=1,refine_id='refine-1')
    reader=MagicMock();reader.extent.return_value=(0.,0.,1.,1.);reader.track_count.return_value=0
    with ExitStack() as stack:
        target={'reader':'run_grid_cross_track_task','clustering':'_build_candidate_clusters','topology':'_build_topologies_from_reader'}[phase]
        if phase!='reader':stack.enter_context(patch.object(detect,'run_grid_cross_track_task',return_value=empty_cross_batch()))
        stack.enter_context(patch.object(detect,target,side_effect=failure))
        with pytest.raises(type(failure)):
            detect._detect_intersections_db(object(),cfg,CONTEXT,runtime=runtime_config(),data_root=tmp_path,refine_reader=reader,connect_worker=lambda:object())
    assert not list(tmp_path.glob('duckdb/turn-event-relations/*'))
    assert not list(tmp_path.glob('duckdb/cross-event-relations/*'))


def test_joint_cluster_summary_and_reference_lengths_do_not_decode_members_again(tmp_path):
    values=candidates();cfg=intersect_config(candidate_dbscan_eps_m=2.,candidate_dbscan_min_samples=2,merge_centroid_distance_m=100.)
    turns=turns_for(values)
    with stored(tmp_path,values) as spool,patch.object(spool,'iter_batches',side_effect=AssertionError('business payload decoded')):
        crosses=detect._cluster_orthogonal_cross_track_candidates(spool,TRANSFORMER,eps_m=2.,min_samples=2)
        assert len(crosses)>1
        sources=(("turn",turns),("orthogonal_cross_track",spool))
        initial=detect._build_candidate_clusters(sources,cfg,TRANSFORMER)
        assert sum(np.count_nonzero(cluster.members.kind=='orthogonal_cross_track') for cluster in initial)==16
        assert sum(np.count_nonzero(cluster.members.kind=='turn') for cluster in initial)==4
        annotated=detect._annotate_source_ablation(initial,(("turn",turns),("orthogonal_cross_track",spool)),cfg,TRANSFORMER)
        eager_sources=(("turn",turns),("orthogonal_cross_track",values))
        expected=detect._annotate_source_ablation(detect._build_candidate_clusters(eager_sources,cfg,TRANSFORMER),eager_sources,cfg,TRANSFORMER)
        assert serialized(annotated)==serialized(expected)


def test_eager_and_lazy_membership_conflicts_still_raise(tmp_path):
    values=candidates()
    with stored(tmp_path,values) as spool:
        lazy=detect._cluster_orthogonal_cross_track_candidates(spool,TRANSFORMER,eps_m=2.,min_samples=2)
        eager=detect._cluster_orthogonal_cross_track_candidates(values,TRANSFORMER,eps_m=2.,min_samples=2)
        for groups in (eager,lazy):
            first=groups[0];duplicate=replace(first,cluster_calculate_index=999)
            ids={first.cluster_calculate_index:10,999:20}
            with pytest.raises(ValueError,match='belongs to multiple candidates'):membership_rows(tmp_path,[first,duplicate],ids)
        with pytest.raises(ValueError,match='belongs to multiple candidates'):
            eager_membership_rows([eager[0],replace(eager[0],cluster_calculate_index=999)],{eager[0].cluster_calculate_index:10,999:20})
