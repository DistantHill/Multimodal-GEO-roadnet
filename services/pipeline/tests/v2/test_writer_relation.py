"""Columnar writer source resolution, atomicity and namespace behavior."""
from __future__ import annotations
from dataclasses import fields, replace
import inspect
import math
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np
import pytest
from .config_factory import runtime_config
from track.v2.intersections.relations import source_associations as writer_relation
from track.v2.intersections.relations.source_associations import WriterRelation
from track.v2.db import write_detect_results as db_writer
from track.v2.models import TurnBatch, CrossCandidateBatch, CrossingBatch, MemberBatch


def _relation(tmp_path, *, batch_size=2):
    return WriterRelation(runtime_config(duckdb_memory_limit="128MB",duckdb_threads=1,
                         duckdb_max_temp_directory_size="256MB"), tmp_path,"detect",
                         batch_size=batch_size,relation_id="relation")


def _batch(cls, size, **updates):
    columns = {}
    string_fields = {"vehicle_id","source_id","point_kind","generated_reason",
                     "from_vehicle_id","to_vehicle_id","from_source_id","to_source_id",
                     "from_point_kind","to_point_kind","from_generated_reason","to_generated_reason"}
    integer_fields = {"calculation_index","subtrack_index","point_index","source_row_key","refine_point_id",
                      "from_subtrack_index","to_subtrack_index","from_point_index","to_point_index",
                      "from_refine_point_id","to_refine_point_id","from_source_row_key","to_source_row_key"}
    for field in fields(cls):
        name = field.name
        if name == "cluster_ref": columns[name] = "fixture"
        elif name in {"xy","lonlat"}: columns[name] = np.zeros((size,2),dtype=np.float64)
        elif name.endswith("_valid") or name == "is_entry": columns[name] = np.ones(size,dtype=np.bool_)
        elif name in string_fields: columns[name] = np.full(size,"p" if name.endswith("point_kind") else "fixture")
        elif name in integer_fields: columns[name] = np.zeros(size,dtype=np.int64)
        else: columns[name] = np.zeros(size,dtype=np.float64)
    columns.update(updates)
    return cls(**columns)


def _turns(indices, ids=None, tracks=None, kinds=None, **updates):
    n = len(indices)
    return _batch(TurnBatch,n,calculation_index=np.asarray(indices,dtype=np.int64),
        refine_point_id=np.asarray(ids if ids is not None else [1]*n,dtype=np.int64),
        subtrack_index=np.asarray(tracks if tracks is not None else [0]*n,dtype=np.int64),
        point_kind=np.asarray(kinds if kinds is not None else ["p"]*n),
        entry_bearing=np.asarray(indices,dtype=np.float64)+10,exit_bearing=np.full(n,20.),
        turn_deg=np.full(n,-10.),score=np.full(n,.5), **updates)


def _crosses(indices, first=None, second=None):
    n=len(indices); first=first or [("p",1,0)]*n; second=second or [("p",2,0)]*n
    return _batch(CrossCandidateBatch,n,calculation_index=np.asarray(indices,dtype=np.int64),
        xy=np.column_stack((np.asarray(indices,dtype=np.float64)+.1,np.full(n,2.))),
        lonlat=np.tile([113.,23.],(n,1)),
        from_point_kind=np.asarray([v[0] for v in first]),from_refine_point_id=np.asarray([v[1] for v in first],dtype=np.int64),
        from_subtrack_index=np.asarray([v[2] for v in first],dtype=np.int64),
        to_point_kind=np.asarray([v[0] for v in second]),to_refine_point_id=np.asarray([v[1] for v in second],dtype=np.int64),
        to_subtrack_index=np.asarray([v[2] for v in second],dtype=np.int64))


def _crossings(refs, **updates):
    n=len(refs)
    columns=dict(calculation_index=np.arange(n,dtype=np.int64),
        point_kind=np.asarray([v[0] for v in refs]),refine_point_id=np.asarray([v[1] for v in refs],dtype=np.int64),
        subtrack_index=np.asarray([v[2] for v in refs],dtype=np.int64),
        xy=np.tile([1.,2.],(n,1)),lonlat=np.tile([113.,23.],(n,1)))
    columns.update(updates)
    return _batch(CrossingBatch,n,**columns)


def _cluster(index,kinds=(),indices=()):
    n=len(kinds)
    return NS(cluster_calculate_index=index,members=MemberBatch(np.asarray(kinds),np.asarray(indices,dtype=np.int64),
              np.arange(n,dtype=np.int64),np.full(n,"fixture"),np.ones(n,dtype=np.bool_)))


def _mappings(relation, refs, ids):
    relation.add_mappings(np.asarray(["kept" if r[0] == "p" else "injected" for r in refs]),np.asarray([r[1] for r in refs],dtype=np.int64),
                          np.asarray([r[2] for r in refs],dtype=np.int64),np.asarray(ids,dtype=np.int64))


def _request(relation,consumer,ordinal,endpoint,ref):
    relation._connection.execute("INSERT INTO source_requests VALUES (?,?,?,?,?,?,?)",
                                (relation._request_order,"kept" if ref[0]=="p" else "injected",ref[1],ref[2],consumer,ordinal,endpoint))
    relation._request_order += 1


def _resolve_for_annotations(relation, refs, turns, crosses):
    relation.collect_sources(NS(turn_events=_turns(turns,[refs[0][1]]*len(turns)),
        cross_track_candidates=_crosses(crosses,[refs[0]]*len(crosses),[refs[1]]*len(crosses))),[],{}, {})
    _mappings(relation,refs,[100,101]); relation.resolve_sources()


class ClosingBatches:
    def __init__(self,batches,failure=None): self.batches,self.failure,self.closed=batches,failure,False
    def iter_batches(self):
        try:
            yield from self.batches
            if self.failure is not None: raise self.failure
        finally: self.closed=True

@pytest.mark.parametrize("failure",[None,RuntimeError("failed"),KeyboardInterrupt()])
def test_persist_detect_result_owns_relation_lifecycle(tmp_path,monkeypatch,failure):
    events=[];runtime=runtime_config()
    class Relation:
        def __init__(self,*args,**kwargs):events.append((args,kwargs))
        def __enter__(self):events.append("enter");return self
        def __exit__(self,*args):events.append("close")
    def persist(*args,**kwargs):
        if failure is not None:raise failure
        return "written"
    monkeypatch.setattr(db_writer,"WriterRelation",Relation);monkeypatch.setattr(db_writer,"_persist_detect_result",persist)
    if failure is None:assert db_writer.persist_detect_result(object(),"detect",object(),runtime=runtime,data_root=tmp_path,batch_size=7)=="written"
    else:
        with pytest.raises(type(failure)):db_writer.persist_detect_result(object(),"detect",object(),runtime=runtime,data_root=tmp_path,batch_size=7)
    assert events[-2:]==["enter","close"]
    assert events[0][0]==(runtime,tmp_path,"detect") and events[0][1]=={"batch_size":7}


def test_collect_resolve_exact_keys_last_wins_and_order_across_batches(tmp_path):
    refs=[("p",7,2),("i",7,2),("p",7,3)]
    turns=ClosingBatches([_turns([9,4],[7,7],[3,2])]);crosses=ClosingBatches([_crosses([8,3],[refs[1],refs[2]],[refs[0],refs[1]])])
    with _relation(tmp_path,batch_size=1) as relation:
        relation.collect_sources(NS(turn_events=turns,cross_track_candidates=crosses),
            [(_cluster(5),_crossings([refs[0]]))],{5:np.array([900],dtype=np.int64)},{5:50})
        assert relation.counts==[2,2,1]
        assert list(relation.iter_raw("turn"))==[(0,19.,20.,-10.,.5),(1,14.,20.,-10.,.5)]
        assert list(relation.iter_raw("crossing"))==[(0,900,50,1,1.,2.,113.,23.)]
        assert list(relation.iter_annotation_requests())==[("turn",9,0),("turn",4,1),("cross",8,0),("cross",3,1)]
        _mappings(relation,refs,[100,101,102]);_mappings(relation,[refs[0]],[999]);relation.resolve_sources()
        assert list(relation.iter_resolved_sources(0))==[(0,0,102),(1,0,999)]
        assert list(relation.iter_resolved_sources(1))==[(0,0,101),(0,1,999),(1,0,102),(1,1,101)]
        assert list(relation.iter_resolved_sources(2))==[(0,0,999)]
        relation.resolve_sources()
        with pytest.raises(RuntimeError,match="resolved"):_mappings(relation,[refs[0]],[1])
    assert turns.closed and crosses.closed


@pytest.mark.parametrize("count",[0,1,2,3])
def test_add_mappings_bounded_typed_order_and_large_ints(tmp_path,count):
    refs=[("p",2**53+i,10+i) for i in range(count)]
    with _relation(tmp_path,batch_size=2) as relation:
        _mappings(relation,refs,[2**53+100+i for i in range(count)])
        assert relation._connection.execute("SELECT mapping_order,point_kind,point_id,subtrack_index,resolved_id FROM source_mappings ORDER BY mapping_order").fetchall()==[(i,"kept",r[1],r[2],2**53+100+i) for i,r in enumerate(refs)]


def test_add_mappings_preserves_last_mapping_wins(tmp_path):
    ref=("p",7,2)
    with _relation(tmp_path) as relation:
        _request(relation,0,0,0,ref);_mappings(relation,[ref,ref],[100,999]);relation.resolve_sources()
        assert list(relation.iter_resolved_sources(0))==[(0,0,999)]


def test_add_mappings_sql_failure_rolls_back_batch_and_unregisters(tmp_path):
    with _relation(tmp_path,batch_size=2) as relation:
        relation._connection.execute("INSERT INTO source_mappings VALUES (2,'kept',999,9,999)")
        with pytest.raises(Exception,match="duplicate key|constraint"):_mappings(relation,[("p",i,1) for i in range(3)],[100,101,102])
        assert relation._mapping_order==2
        assert relation._connection.execute("SELECT mapping_order,point_id,resolved_id FROM source_mappings ORDER BY mapping_order").fetchall()==[(0,0,100),(1,1,101),(2,999,999)]
        assert relation._connection.execute("SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'writer_batch_%'").fetchone()==(0,)


@pytest.mark.parametrize("field,column",[("point_id",np.array([1.5])),("subtrack_index",np.array([True])),("resolved_ids",np.array(["7"])),("point_id",np.array([2**63],dtype=np.uint64))])
def test_add_mappings_rejects_lossy_typed_values(tmp_path,field,column):
    columns={"point_kind":np.array(["kept"]),"point_id":np.array([1],dtype=np.int64),"subtrack_index":np.array([2],dtype=np.int64),"resolved_ids":np.array([3],dtype=np.int64)};columns[field]=column
    with _relation(tmp_path) as relation:
        with pytest.raises((TypeError,ValueError)):relation.add_mappings(**columns)
        assert relation._mapping_order==0
        assert relation._connection.execute("SELECT count(*) FROM source_mappings").fetchone()==(0,)


def test_missing_key_reports_exact_three_field_identity(tmp_path):
    with _relation(tmp_path) as relation:
        _request(relation,0,0,0,("p",100,4));_mappings(relation,[("p",100,5),("i",100,4)],[1,2])
        with pytest.raises(KeyError) as error:relation.resolve_sources()
        assert error.value.args==(("kept",100,4),)


@pytest.mark.parametrize("stage,failure",[("success",None),("turn",RuntimeError("broken")),("cross",KeyboardInterrupt())])
def test_input_iterators_close_on_success_and_base_exception(tmp_path,stage,failure):
    turns=ClosingBatches([_turns([0])],failure if stage=="turn" else None);crosses=ClosingBatches([_crosses([0])],failure if stage=="cross" else None)
    with _relation(tmp_path) as relation:
        if failure is None:relation.collect_sources(NS(turn_events=turns,cross_track_candidates=crosses),[],{},{})
        else:
            with pytest.raises(type(failure)):relation.collect_sources(NS(turn_events=turns,cross_track_candidates=crosses),[],{},{})
    assert turns.closed
    if stage!="turn":assert crosses.closed


def test_collect_sources_preserves_nullable_nan_and_infinite_raw_values(tmp_path):
    turns=_turns([2**53+3],[2**53+1],[2**53+2]);turns=replace(turns,entry_bearing=np.ma.array([0.],mask=[True]),exit_bearing=np.array([np.nan]),turn_deg=np.array([np.inf]),score=np.array([-np.inf]))
    crossings=_crossings([("p",2**53+1,2**53+2)],is_entry=np.array([False]));crossings=replace(crossings,xy=np.ma.array([[0.,np.nan]],mask=[[True,False]]),lonlat=np.array([[np.inf,-np.inf]]))
    with _relation(tmp_path) as relation:
        relation.collect_sources(NS(turn_events=turns,cross_track_candidates=_crosses([])),[(_cluster(9),crossings)],{9:np.ma.array([0],mask=[True])},{9:None})
        row=list(relation.iter_raw("turn"))[0];assert row[1] is None and math.isnan(row[2]);assert row[3:]==(np.inf,-np.inf)
        row=list(relation.iter_raw("crossing"))[0];assert row[:4]==(0,None,None,0);assert row[4] is None and math.isnan(row[5]);assert row[6:]==(np.inf,-np.inf)
        assert relation._connection.execute("SELECT point_id,subtrack_index FROM source_requests ORDER BY request_order").fetchall()==[(2**53+1,2**53+2)]*2


def test_collect_sources_failed_batch_is_atomic_and_retryable(tmp_path):
    with _relation(tmp_path,batch_size=2) as relation:
        relation._connection.execute("INSERT INTO turn_raw VALUES (2,0,0,0,0)")
        with pytest.raises(Exception,match="duplicate key|constraint"):relation.collect_sources(NS(turn_events=_turns([0,1,2]),cross_track_candidates=_crosses([])),[],{},{})
        assert relation.counts==[2,0,0] and relation._request_order==2
        assert relation._connection.execute("SELECT count(*) FROM source_requests").fetchone()==(2,)
        assert relation._connection.execute("SELECT count(*) FROM annotation_requests").fetchone()==(2,)
        assert relation._connection.execute("SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'writer_batch_%'").fetchone()==(0,)
        relation._connection.execute("DELETE FROM turn_raw WHERE ordinal=2")
        relation.collect_sources(NS(turn_events=_turns([2]),cross_track_candidates=_crosses([])),[],{},{})
        assert relation.counts==[3,0,0]
        assert [row[0] for row in relation.iter_raw("turn")]==[0,1,2]


def test_collect_sources_third_insert_failure_rolls_back_whole_batch(tmp_path):
    with _relation(tmp_path) as relation:
        relation._connection.execute("INSERT INTO annotation_requests VALUES (0,'existing',0,0)")
        with pytest.raises(Exception,match="duplicate key|constraint"):relation.collect_sources(NS(turn_events=_turns([0]),cross_track_candidates=_crosses([])),[],{},{})
        assert relation.counts==[0,0,0] and relation._request_order==0
        assert relation._connection.execute("SELECT count(*) FROM source_requests").fetchone()==(0,)
        assert relation._connection.execute("SELECT count(*) FROM turn_raw").fetchone()==(0,)


def test_pending_strict_length_mismatch_is_atomic(tmp_path):
    with _relation(tmp_path) as relation:
        with pytest.raises(ValueError):relation.collect_sources(NS(turn_events=_turns([]),cross_track_candidates=_crosses([])),[(_cluster(1),_crossings([("p",1,1)]))],{1:np.empty(0,dtype=np.int64)},{1:2})
        assert relation.counts==[0,0,0] and relation._request_order==0


@pytest.mark.parametrize("consumer",["turn","cross","crossing"])
def test_missing_stable_reference_fails_closed(tmp_path,consumer):
    turns=_turns([0] if consumer=="turn" else []);crosses=_crosses([0] if consumer=="cross" else []);crossing=_crossings([("p",1,1)])
    if consumer=="turn":turns=replace(turns,refine_point_id_valid=np.array([False]))
    if consumer=="cross":crosses=replace(crosses,to_refine_point_id_valid=np.array([False]))
    if consumer=="crossing":crossing=replace(crossing,refine_point_id_valid=np.array([False]))
    turns = ClosingBatches([turns])
    crosses = ClosingBatches([crosses])
    with _relation(tmp_path) as relation:
        with pytest.raises(ValueError,match="stable"):relation.collect_sources(NS(turn_events=turns,cross_track_candidates=crosses),[(_cluster(1),crossing)] if consumer=="crossing" else [],{1:np.array([4],dtype=np.int64)},{1:2})
        assert relation.counts==[0,0,0]
    assert turns.closed
    if consumer != "turn":
        assert crosses.closed


def test_annotations_preserve_namespaces_duplicates_hotspots_and_order(tmp_path):
    with _relation(tmp_path,batch_size=1) as relation:
        _resolve_for_annotations(relation,[("p",1,0),("p",2,0)],[7,8,9],[7,10])
        clusters=[_cluster(1,["turn","turn","other","orthogonal_cross_track"],[7,7,8,999]),_cluster(2,["turn","orthogonal_cross_track"],[9,7])]
        hotspots=_turns([7,7,99],g_star_z=np.array([1.5,2.5,9.]))
        relation.prepare_annotations(clusters,{1:41,2:42},hotspots)
        assert list(relation.iter_annotations("turn"))==[(0,41,2.5),(1,None,None),(2,42,None)]
        assert list(relation.iter_annotations("cross"))==[(0,42,None),(1,None,None)]
        with pytest.raises(RuntimeError,match="already prepared"):relation.prepare_annotations([],{},_turns([]))


def test_annotation_last_hotspot_preserves_null_and_nan(tmp_path):
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation,[("p",1,0),("p",2,0)],[7,8],[])
        relation.prepare_annotations([],{},_turns([7,8,7,8],g_star_z=np.array([1.,2.,0.,np.nan]),g_star_z_valid=np.array([True,True,False,True])))
        rows=list(relation.iter_annotations("turn"));assert rows[0]==(0,None,None);assert rows[1][:2]==(1,None) and math.isnan(rows[1][2])


def test_annotation_second_batch_sql_failure_rolls_back_all_and_retries(tmp_path):
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation,[("p",1,0),("p",2,0)],[1,2,3],[])
        relation._connection.execute("CREATE UNIQUE INDEX members_encounter_unique ON members(encounter)");relation._connection.execute("INSERT INTO members VALUES ('turn',999,2,999)")
        with pytest.raises(Exception,match="duplicate key|constraint"):relation.prepare_annotations([_cluster(1,["turn"]*3,[1,2,3])],{1:10},_turns([]))
        assert relation._connection.execute("SELECT kind,calculation_index,encounter,candidate_id FROM members").fetchall()==[("turn",999,2,999)]
        assert relation._connection.execute("SELECT count(*) FROM annotations").fetchone()==(0,)
        relation._connection.execute("DELETE FROM members");relation.prepare_annotations([_cluster(1,["turn"]*3,[1,2,3])],{1:10},_turns([]))
        assert list(relation.iter_annotations("turn"))==[(0,10,None),(1,10,None),(2,10,None)]


def test_annotation_conflict_reports_earliest_encounter_not_smallest_key(tmp_path):
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation,[("p",1,0),("p",2,0)],[99,1],[])
        with pytest.raises(ValueError,match="Detect entity 99 belongs"):relation.prepare_annotations([_cluster(1,["turn","turn"],[99,1]),_cluster(2,["turn","turn"],[99,1])],{1:10,2:20},_turns([]))
        assert relation._connection.execute("SELECT count(*) FROM members").fetchone()==(0,)
        relation.prepare_annotations([_cluster(1,["turn"],[99])],{1:10},_turns([]))
        assert list(relation.iter_annotations("turn"))==[(0,10,None),(1,None,None)]


@pytest.mark.parametrize("failure",[None,RuntimeError("members"),KeyboardInterrupt()])
def test_annotation_member_iterator_failure_is_atomic(tmp_path,failure):
    class Clusters:
        closed=False
        def __iter__(self):
            try:
                yield _cluster(1,["turn"],[1])
                if failure is not None:raise failure
            finally:self.closed=True
    clusters=Clusters()
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation,[("p",1,0),("p",2,0)],[1],[])
        if failure is None:relation.prepare_annotations(clusters,{1:10},_turns([]))
        else:
            with pytest.raises(type(failure)):relation.prepare_annotations(clusters,{1:10},_turns([]))
            assert relation._connection.execute("SELECT count(*) FROM members").fetchone()==(0,)
        assert clusters.closed

def test_empty_unresolved_and_closed_access(tmp_path):
    relation = _relation(tmp_path)
    with pytest.raises(RuntimeError, match="not resolved"):
        list(relation.iter_resolved_sources(0))
    relation.resolve_sources()
    assert relation.counts == [0, 0, 0]
    assert list(relation.iter_resolved_sources(0)) == []
    assert list(relation.iter_raw("turn")) == []
    assert list(relation.iter_annotation_requests()) == []
    leaf = relation._temp_directory
    relation.close()
    relation.close()
    assert not leaf.exists() and leaf.parent.is_dir()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_raw("turn"))
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_resolved_sources(0))


def test_annotation_state_is_fail_closed(tmp_path):
    relation = _relation(tmp_path)
    with pytest.raises(RuntimeError, match="not resolved"):
        relation.prepare_annotations([], {}, _turns([]))
    with pytest.raises(RuntimeError, match="not prepared"):
        list(relation.iter_annotations("turn"))
    relation.resolve_sources()
    assert list(relation.iter_annotation_requests()) == []
    relation.prepare_annotations([], {}, _turns([]))
    assert list(relation.iter_annotations("turn")) == []
    with pytest.raises(ValueError, match="unknown"):
        list(relation.iter_annotations("crossing"))
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_annotations("turn"))



@pytest.mark.parametrize("batch_size", [True, 0])
def test_invalid_batch_size(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        WriterRelation(runtime_config(), tmp_path, "detect", batch_size=batch_size)


@pytest.mark.parametrize("root", ["relative", "/"])
def test_unsafe_data_root(root):
    with pytest.raises(ValueError, match="absolute and non-root"):
        WriterRelation(runtime_config(), root, "detect")


@pytest.mark.parametrize("field,value", [
    ("detect_id", "bad/name"), ("detect_id", ".."),
    ("relation_id", "bad/name"), ("relation_id", "bad\x00name"),
])
def test_unsafe_identity(tmp_path, field, value):
    kwargs = {field: value}
    detect_id = kwargs.pop("detect_id", "detect")
    with pytest.raises(ValueError, match=field):
        WriterRelation(runtime_config(), tmp_path, detect_id, **kwargs)


def test_connection_and_schema_failures_clean_only_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "writer-relations" / "detect" / "keep"
    sibling.mkdir(parents=True)
    monkeypatch.setattr(writer_relation.duckdb, "connect",
                        lambda **_kw: (_ for _ in ()).throw(RuntimeError("open failed")))
    with pytest.raises(RuntimeError, match="open failed"):
        WriterRelation(runtime_config(), tmp_path, "detect", relation_id="failed")
    assert sibling.is_dir() and not (sibling.parent / "failed").exists()


def test_schema_failure_closes_connection_and_cleans_only_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "writer-relations" / "detect" / "keep"
    sibling.mkdir(parents=True)
    real_connect = writer_relation.duckdb.connect
    connection = real_connect(":memory:")
    monkeypatch.setattr(writer_relation.duckdb, "connect", lambda **_kw: connection)
    monkeypatch.setattr(WriterRelation, "_create_schema",
                        lambda _self: (_ for _ in ()).throw(RuntimeError("schema failed")))
    with pytest.raises(RuntimeError, match="schema failed"):
        WriterRelation(runtime_config(), tmp_path, "detect", relation_id="failed")
    with pytest.raises(Exception):
        connection.execute("SELECT 1")
    assert sibling.is_dir() and not (sibling.parent / "failed").exists()


def test_identity_collision_preserves_foreign_leaf(tmp_path):
    leaf = tmp_path / "duckdb" / "writer-relations" / "detect" / "occupied"
    leaf.mkdir(parents=True)
    marker = leaf / "owned"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        WriterRelation(runtime_config(), tmp_path, "detect", relation_id="occupied")
    assert marker.read_text() == "keep"


def test_offline_limits_and_columnar_primitives(tmp_path):
    with _relation(tmp_path) as relation:
        names = ("threads", "memory_limit", "max_temp_directory_size", "temp_directory",
                 "allow_unsigned_extensions",
                 "autoload_known_extensions", "autoinstall_known_extensions")
        settings = dict(relation._connection.execute(
            "SELECT name,value FROM duckdb_settings() WHERE name IN (?,?,?,?,?,?,?)", names
        ).fetchmany(20))
        assert settings == {
            "threads": "1", "memory_limit": "122.0 MiB",
            "max_temp_directory_size": "244.1 MiB",
            "temp_directory": str(relation._temp_directory),
            "allow_unsigned_extensions": "false",
            "autoload_known_extensions": "false", "autoinstall_known_extensions": "false",
        }
    source = inspect.getsource(writer_relation).lower()
    assert 'database=":memory:"' in source and "fetchmany" in source and "order by" in source
    for forbidden in ("fetchall", "pickle", "heapq", "tempfile"):
        assert forbidden not in source



def _prepared_final_relation(tmp_path, *, batch_size=1, cross_refs=None):
    refs=[("p",index,0) for index in range(1,7)]
    relation=_relation(tmp_path,batch_size=batch_size)
    pairs=cross_refs or [(refs[1],refs[0]),(refs[2],refs[3]),(refs[5],refs[4])]
    relation.collect_sources(NS(turn_events=_turns([8,9,10],[1,2,3]),
        cross_track_candidates=_crosses([20,21,22],[pair[0] for pair in pairs],[pair[1] for pair in pairs])),
        [(_cluster(7),_crossings([refs[3]])),(_cluster(8),_crossings([refs[4]],is_entry=np.array([False]),xy=np.array([[3.,4.]]),lonlat=np.array([[114.,24.]])))],
        {7:np.array([900],dtype=np.int64),8:np.array([901],dtype=np.int64)},{7:70,8:80})
    _mappings(relation,refs,list(range(101,107)));relation.resolve_sources()
    relation.prepare_annotations([_cluster(7,["turn","orthogonal_cross_track"],[8,20]),_cluster(8,["turn","orthogonal_cross_track"],[10,22])],{7:70,8:80},_turns([8],g_star_z=np.array([4.5])))
    return relation


def test_final_rows_exact_copy_shape_and_original_order_across_batches(tmp_path):
    with _prepared_final_relation(tmp_path) as relation:
        assert list(relation.iter_rows(0, "detect")) == [
            ("detect", 70, 101, 18.0, 20.0, -10.0, 0.5, 4.5),
            ("detect", None, 102, 19.0, 20.0, -10.0, 0.5, None),
            ("detect", 80, 103, 20.0, 20.0, -10.0, 0.5, None),
        ]
        expected_cross = [
            ("detect", 70, 101, 102, 20.1, 2.0, 113.0, 23.0),
            ("detect", None, 103, 104, 21.1, 2.0, 113.0, 23.0),
            ("detect", 80, 105, 106, 22.1, 2.0, 113.0, 23.0),
        ]
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_rows(1, "detect"))
        assert list(relation.iter_rows(2, "detect")) == [
            (900, "detect", 70, 104, 1, 1.0, 2.0, 113.0, 23.0),
            (901, "detect", 80, 105, 0, 3.0, 4.0, 114.0, 24.0),
        ]
        relation.validate_cross_pairs("detect")
        assert list(relation.iter_validated_cross_rows()) == expected_cross
        assert list(relation.iter_rows(1, "detect")) == expected_cross
        relation.validate_cross_pairs("detect")


@pytest.mark.parametrize("pairs", [
    ((0, 1), (1, 0), (2, 3)),
    ((0, 1), (0, 1), (2, 3)),
    ((0, 0), (1, 2), (3, 4)),
])
def test_cross_pair_validation_is_atomic_for_duplicates_and_degenerate(tmp_path, pairs):
    refs = [("p", index, 0) for index in range(1,7)]
    relation = _prepared_final_relation(
        tmp_path, cross_refs=[(refs[first], refs[second]) for first, second in pairs],
    )
    try:
        with pytest.raises(ValueError, match="duplicate/degenerate track cross segment pair"):
            relation.validate_cross_pairs("detect")
        assert relation._connection.execute(
            "SELECT count(*) FROM validated_cross"
        ).fetchone() == (0,)
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_validated_cross_rows())
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_rows(1, "detect"))
    finally:
        relation.close()


@pytest.mark.parametrize("mutation,error", [
    ("DELETE FROM resolved_sources WHERE consumer=0 AND ordinal=0", "source"),
    ("INSERT INTO resolved_sources VALUES (0,99,0,1)", "source"),
    ("INSERT INTO resolved_sources VALUES (0,0,0,1)", "source"),
    ("DELETE FROM annotations WHERE kind='turn' AND ordinal=0", "annotation"),
    ("INSERT INTO annotations VALUES (999,'turn',99,NULL,NULL)", "annotation"),
    ("DELETE FROM turn_raw WHERE ordinal=0", "raw"),
])
def test_final_rows_fail_closed_for_tampered_relations(tmp_path, mutation, error):
    with _prepared_final_relation(tmp_path) as relation:
        relation._connection.execute(mutation)
        rows = relation.iter_rows(0, "detect")
        with pytest.raises(RuntimeError, match=error):
            next(rows)


def test_cross_validation_association_failure_never_exposes_prefix(tmp_path):
    with _prepared_final_relation(tmp_path) as relation:
        relation._connection.execute(
            "DELETE FROM annotations WHERE kind='cross' AND ordinal=2"
        )
        with pytest.raises(RuntimeError, match="annotation"):
            relation.validate_cross_pairs("detect")
        assert relation._connection.execute(
            "SELECT count(*) FROM validated_cross"
        ).fetchone() == (0,)
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_validated_cross_rows())


def test_final_row_states_identity_empty_and_close(tmp_path):
    relation = _relation(tmp_path)
    with pytest.raises(RuntimeError, match="not resolved"):
        next(relation.iter_rows(2, "detect"))
    relation.resolve_sources()
    assert list(relation.iter_rows(2, "detect")) == []
    with pytest.raises(RuntimeError, match="not prepared"):
        next(relation.iter_rows(0, "detect"))
    relation.prepare_annotations([], {}, _turns([]))
    for consumer in (0, 2):
        assert list(relation.iter_rows(consumer, "detect")) == []
    with pytest.raises(RuntimeError, match="not validated"):
        next(relation.iter_rows(1, "detect"))
    relation.validate_cross_pairs("detect")
    assert list(relation.iter_validated_cross_rows()) == []
    assert list(relation.iter_rows(1, "detect")) == []
    with pytest.raises(ValueError, match="unknown"):
        next(relation.iter_rows(3, "detect"))
    with pytest.raises(ValueError, match="does not match"):
        next(relation.iter_rows(0, "other"))
    with pytest.raises(ValueError, match="does not match"):
        relation.validate_cross_pairs("other")
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        next(relation.iter_rows(0, "detect"))
    with pytest.raises(RuntimeError, match="closed"):
        relation.validate_cross_pairs("detect")
    with pytest.raises(RuntimeError, match="closed"):
        next(relation.iter_validated_cross_rows())


def test_typed_text_scanner_preserves_wide_strings_null_nan_and_large_ids(tmp_path):
    size = 4096
    with _relation(tmp_path) as relation:
        ids = np.arange(size,dtype=np.int64)+2**53
        texts = np.asarray(["p" if i%2 else "injected-with-long-text中文🚗" for i in range(size)],dtype=object)
        nullable = np.ma.array(np.full(size,np.nan),mask=np.arange(size)%2==1)
        columns = {"id":ids,"kind":texts,**relation._float_columns({"value":nullable})}
        name = relation._register_batch(columns)
        try:
            rows = relation._connection.execute(f"SELECT id,kind FROM {name}").fetchmany(size)
            np.testing.assert_array_equal(np.fromiter((row[0] for row in rows),dtype=np.int64),ids)
            assert [row[1] for row in rows] == texts.tolist()
            value = relation._float_sql(("value",))
            assert relation._connection.execute(f"SELECT count(*) FROM (SELECT {value} AS v FROM {name}) WHERE v IS NULL").fetchone()[0] == size//2
            assert relation._connection.execute(f"SELECT count(*) FROM (SELECT {value} AS v FROM {name}) WHERE isnan(v)").fetchone()[0] == size//2
        finally:
            relation._connection.unregister(name)


def test_plugin_instance_sources_keep_duplicate_turns_and_cluster_assignments(tmp_path):
    from track.v2.plugins.execution import CandidateResult, CompositeRelation
    events = _turns([0])
    params = None
    first = CandidateResult('turn_threshold', 'low', 'turn', events, events, params, 0)
    second = CandidateResult('turn_threshold', 'high', 'turn', events, events, params, 1)
    with _relation(tmp_path) as relation:
        relation.collect_sources(NS(turn_events=CompositeRelation(((events, 0), (events, 1))),
            cross_track_candidates=_crosses([]), candidate_results=(first, second)), [], {}, {})
        _mappings(relation, [('p', 1, 0)], [100])
        relation.resolve_sources()
        relation.prepare_annotations([_cluster(0, [first.source_key], [0]),
                                      _cluster(1, [second.source_key], [1])], {0: 10, 1: 20},
                                     CompositeRelation(((events, 0), (events, 1))))
        rows = list(relation.with_instance_identity('turn', relation.iter_rows(0, 'detect')))
    assert len(rows) == 2
    assert [row[1] for row in rows] == [10, 20]
    assert [row[-4:] for row in rows] == [('turn_threshold', 'low', 0, 0), ('turn_threshold', 'high', 0, 1)]


def test_plugin_instances_allow_same_cross_pair_but_reject_duplicate_within_instance(tmp_path):
    from track.v2.plugins.execution import CandidateResult, CompositeRelation
    events = _crosses([0])
    first = CandidateResult('cross', 'a', 'orthogonal_cross_track', events, events, None, 0)
    second = CandidateResult('cross', 'b', 'orthogonal_cross_track', events, events, None, 1)
    with _relation(tmp_path) as relation:
        relation.collect_sources(NS(turn_events=_turns([]),
            cross_track_candidates=CompositeRelation(((events, 0), (events, 1))),
            candidate_results=(first, second)), [], {}, {})
        _mappings(relation, [('p', 1, 0), ('p', 2, 0)], [100, 101])
        relation.resolve_sources()
        relation.prepare_annotations([], {}, _turns([]))
        relation.validate_cross_pairs('detect')
        rows = list(relation.with_instance_identity('cross', relation.iter_validated_cross_rows()))
        assert [row[-3] for row in rows] == ['a', 'b']
        relation._cross_validated = False
        combined = _crosses([0, 1])
        relation._plugin_results = (CandidateResult('cross', 'a', 'orthogonal_cross_track', combined, combined, None, 0),)
        with pytest.raises(ValueError, match='duplicate'):
            relation.validate_cross_pairs('detect')
