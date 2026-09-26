"""Columnar DuckDB cross relation ordering, atomicity and corruption guards."""
from dataclasses import fields, replace
import inspect
from pathlib import Path
import numpy as np
import pytest
from .config_factory import runtime_config
from .writer_relation_helpers import _batch
from track.v2.models import CrossCandidateBatch, MemberBatch
from track.v2.candidates import cross_track_relation as cross_candidate_relation
from track.v2.candidates.cross_track_relation import CrossCandidateRelation


def _candidate(indices, *, optional=False):
    indices=np.asarray(indices,dtype=np.int64); n=len(indices)
    return _batch(CrossCandidateBatch,n,calculation_index=indices,
        xy=np.column_stack((indices.astype(np.float64)+1,np.full(n,2.))),lonlat=np.tile([120.,30.],(n,1)),
        timestamp=indices.astype(np.float64)+10.5,heading=np.full(n,359.5),speed=np.full(n,3.25),
        from_vehicle_id=np.full(n,"a"),to_vehicle_id=np.full(n,"b"),
        from_subtrack_index=np.full(n,2,dtype=np.int64),to_subtrack_index=np.full(n,3,dtype=np.int64),
        from_refine_point_id=np.full(n,4,dtype=np.int64),to_refine_point_id=np.full(n,5,dtype=np.int64),
        from_refine_point_id_valid=np.full(n,optional,dtype=np.bool_),to_refine_point_id_valid=np.full(n,optional,dtype=np.bool_),
        from_point_kind=np.full(n,"p"),to_point_kind=np.full(n,"i"),from_source_id=np.full(n,"source"),to_source_id=np.full(n,"other"))


def _relation(tmp_path, *, batch_size=1):
    return CrossCandidateRelation(runtime_config(duckdb_memory_limit="128MB",duckdb_threads=1,
        duckdb_max_temp_directory_size="256MB"),tmp_path,batch_size=batch_size,relation_id="test-relation")


def concatenate(batches):
    batches=list(batches)
    if not batches: return _candidate([])
    return CrossCandidateBatch(**{field.name:np.concatenate([getattr(batch,field.name) for batch in batches]) for field in fields(CrossCandidateBatch)})


def assert_batch_equal(actual,expected):
    for field in fields(type(expected)):
        np.testing.assert_array_equal(getattr(actual,field.name),getattr(expected,field.name))


def test_empty_relation_seals_and_repeats(tmp_path):
    with _relation(tmp_path) as relation:
        relation.seal()
        assert len(relation)==0
        assert list(relation.iter_batches())==list(relation.iter_batches())==[]
        assert list(relation.iter_coordinate_batches())==[]


def test_lossless_refs_index_slices_coordinates_and_batch_one(tmp_path):
    candidates=_candidate([0,1],optional=True)
    with _relation(tmp_path) as relation:
        relation.append_batch(candidates)
        with pytest.raises(RuntimeError,match="sealed"): list(relation.iter_batches())
        relation.seal()
        for _ in range(2): assert_batch_equal(concatenate(relation.iter_batches()),candidates)
        assert_batch_equal(concatenate(relation.iter_batches(indices=np.asarray([1],dtype=np.int64))),candidates.take(np.asarray([1])))
        coordinates=list(relation.iter_coordinate_batches())
        np.testing.assert_array_equal(np.concatenate([part[0] for part in coordinates]),[[1.,2.],[2.,2.]])
        assert np.concatenate([part[1] for part in coordinates]).tolist()==[0,1]
        assert all(len(part[0])==1 for part in coordinates)
        assert all(not column.flags.writeable for part in coordinates for column in part)


@pytest.mark.parametrize("batch_size",[1,2,5])
def test_extend_is_lossless_and_preserves_nullable_bigints(tmp_path,batch_size):
    huge=2**60+9
    candidates=replace(_candidate([0,1,2],optional=True),from_refine_point_id=np.asarray([4,huge,4],dtype=np.int64),
        from_refine_point_id_valid=np.asarray([False,True,False]),to_refine_point_id=np.asarray([5,huge+3,5],dtype=np.int64))
    with _relation(tmp_path,batch_size=batch_size) as relation:
        relation.extend(iter([candidates]))
        relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),candidates)


def test_extend_insert_failure_rolls_back_batch_and_unregisters(tmp_path):
    first=_candidate([0],optional=True)
    with _relation(tmp_path,batch_size=1) as relation:
        relation.append_batch(first)
        relation._connection.execute("CREATE UNIQUE INDEX cross_vehicle_unique ON events(from_vehicle_id)")
        invalid=replace(_candidate([1,2],optional=True),from_vehicle_id=np.asarray(["duplicate","duplicate"]))
        with pytest.raises(Exception,match="unique|duplicate|constraint"): relation.append_batch(invalid)
        assert len(relation)==1
        assert relation._connection.execute("SELECT count(*) FROM events").fetchone()==(1,)
        assert relation._connection.execute("SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'incoming_%'").fetchone()==(0,)
        second=replace(_candidate([1],optional=True),from_vehicle_id=np.asarray(["second"]))
        relation.append_batch(second); relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),concatenate([first,second]))


def test_extend_nan_cannot_bypass_relation_validation(tmp_path):
    relation=_relation(tmp_path)
    relation.append_batch(replace(_candidate([0]),xy=np.asarray([[np.nan,2.]])))
    with pytest.raises(ValueError): relation.seal()
    with pytest.raises(RuntimeError,match="closed"): len(relation)


@pytest.mark.parametrize("field",["from_source_id","to_source_id","from_generated_reason","to_generated_reason"])
def test_extend_and_append_reject_the_same_null_point_strings(tmp_path,field):
    invalid=replace(_candidate([0]),**{field:np.asarray([None],dtype=object)})
    for method in ("append_batch","extend"):
        relation=_relation(tmp_path/method)
        getattr(relation,method)([invalid] if method=="extend" else invalid)
        with pytest.raises(ValueError): relation.seal()


@pytest.mark.parametrize("sql",[
    "UPDATE events SET timestamp='Infinity'::DOUBLE",
    "UPDATE events SET heading='NaN'::DOUBLE",
    "UPDATE events SET speed='-Infinity'::DOUBLE",
    "UPDATE events SET from_point_kind='bad'",
    "UPDATE events SET to_point_kind='bad'",
    'UPDATE events SET from_subtrack_index=-1,from_refine_point_id_valid=true',
    'UPDATE events SET to_point_index=-1,to_refine_point_id_valid=true',
    'UPDATE events SET ordinal=2,calculation_index=2',
    "UPDATE events SET ordinal=2","UPDATE events SET calculation_index=2",
    "UPDATE events SET from_vehicle_id=NULL","UPDATE events SET xy_0='NaN'::DOUBLE",
    "UPDATE events SET lonlat_0=181","UPDATE events SET from_source_id=''",
    "UPDATE events SET from_refine_point_id_valid=NULL","UPDATE events SET from_refine_point_id=NULL",
    "UPDATE events SET to_refine_point_id=-1,to_refine_point_id_valid=true"])
def test_seal_rejects_corruption_and_closes(tmp_path,sql):
    relation=_relation(tmp_path); relation.append_batch(_candidate([0],optional=True))
    relation._connection.execute(sql)
    with pytest.raises(ValueError): relation.seal()
    with pytest.raises(RuntimeError,match="closed"): len(relation)


def test_sealed_and_closed_lifecycle(tmp_path):
    relation=_relation(tmp_path); relation.append_batch(_candidate([0])); relation.seal(); relation.seal()
    with pytest.raises(RuntimeError,match="sealed"): relation.append_batch(_candidate([1]))
    relation.close(); relation.close()
    with pytest.raises(RuntimeError,match="closed"): list(relation.iter_batches())
    with pytest.raises(RuntimeError,match="closed"): list(relation.iter_coordinate_batches())


def test_seal_rejects_non_contiguous_appended_calculate_index(tmp_path):
    relation=_relation(tmp_path); relation.append_batch(_candidate([4]))
    with pytest.raises(ValueError): relation.seal()
    with pytest.raises(RuntimeError,match="closed"): len(relation)


@pytest.mark.parametrize("batch_size",[True,0])
def test_invalid_batch_size_is_rejected(tmp_path,batch_size):
    with pytest.raises(ValueError,match="batch_size"): _relation(tmp_path,batch_size=batch_size)


@pytest.mark.parametrize("root",["relative","/"])
def test_unsafe_data_root_is_rejected(root):
    with pytest.raises(ValueError,match="absolute and non-root"): CrossCandidateRelation(runtime_config(),root)


@pytest.mark.parametrize("relation_id",[".","..","bad/name","bad\x00name"])
def test_unsafe_relation_identity_is_rejected(tmp_path,relation_id):
    with pytest.raises(ValueError,match="relation_id"): CrossCandidateRelation(runtime_config(),tmp_path,relation_id=relation_id)


def test_connection_failure_removes_only_its_empty_leaf(tmp_path,monkeypatch):
    sibling=tmp_path/"duckdb"/"cross-event-relations"/"keep"; sibling.mkdir(parents=True)
    def fail(*args,**kwargs): raise RuntimeError("open failed")
    monkeypatch.setattr(cross_candidate_relation.duckdb,"connect",fail)
    with pytest.raises(RuntimeError,match="open failed"): CrossCandidateRelation(runtime_config(),tmp_path,relation_id="failed")
    assert sibling.is_dir() and not (sibling.parent/"failed").exists()


def test_relation_identity_collision_fails_closed(tmp_path):
    occupied=tmp_path/"duckdb"/"cross-event-relations"/"occupied"; occupied.mkdir(parents=True)
    marker=occupied/"owned-by-other-run"; marker.write_text("keep")
    with pytest.raises(FileExistsError): CrossCandidateRelation(runtime_config(),tmp_path,relation_id="occupied")
    assert marker.read_text()=="keep"


def test_constructor_failure_after_connect_closes_and_cleans(tmp_path,monkeypatch):
    connection=cross_candidate_relation.duckdb.connect(":memory:")
    monkeypatch.setattr(cross_candidate_relation.duckdb,"connect",lambda *args,**kwargs:connection)
    monkeypatch.setattr(CrossCandidateRelation,"_create_schema",lambda self:1/0)
    with pytest.raises(ZeroDivisionError): CrossCandidateRelation(runtime_config(),tmp_path,relation_id="failed")
    with pytest.raises(Exception): connection.execute("SELECT 1")
    assert not (tmp_path/"duckdb"/"cross-event-relations"/"failed").exists()


def test_runtime_limits_bounded_fetch_and_columnar_implementation(tmp_path):
    with _relation(tmp_path) as relation:
        settings=dict(relation._connection.execute("SELECT name,value FROM duckdb_settings() WHERE name IN ('threads','memory_limit','max_temp_directory_size','temp_directory','autoload_known_extensions','autoinstall_known_extensions')").fetchall())
        assert settings['threads']=='1' and settings['memory_limit']=='122.0 MiB'
        assert settings['max_temp_directory_size']=='244.1 MiB'
        assert settings['autoload_known_extensions']==settings['autoinstall_known_extensions']=='false'
        assert settings['temp_directory']==str(relation._temp_directory)
    assert 'fetchmany' in inspect.getsource(CrossCandidateRelation.iter_batches)
    assert 'fetchmany' in inspect.getsource(CrossCandidateRelation.iter_coordinate_batches)
    assert 'unnest' in inspect.getsource(CrossCandidateRelation.append_batch)
    for forbidden in ('sqlite','pickle','struct','temporarydirectory'): assert forbidden not in inspect.getsource(cross_candidate_relation).lower()


def test_member_pass_has_independent_expected_views_and_ignores_noise(tmp_path):
    labels=np.asarray([4,-1,2,4,2],dtype=np.int64)
    with _relation(tmp_path) as relation:
        relation.append_batch(_candidate(range(5))); relation.seal()
        for label,expected in ((2,[2,4]),(4,[0,3])):
            indices=np.flatnonzero(labels==label).astype(np.int64)
            members=MemberBatch(np.full(len(indices),'orthogonal_cross_track'),indices,np.arange(len(indices),dtype=np.int64),np.full(len(indices),'source'),np.ones(len(indices),dtype=np.bool_))
            actual=concatenate(relation.iter_batches(indices=members.calculation_index))
            assert actual.calculation_index.tolist()==expected
            assert members.member_order.tolist()==list(range(len(expected)))
            assert actual.from_source_id.tolist()==['source']*len(expected)
            assert actual.to_source_id.tolist()==['other']*len(expected)


def test_multiple_member_passes_close_independently(tmp_path):
    with _relation(tmp_path) as relation:
        relation.append_batch(_candidate(range(3))); relation.seal()
        first=relation.iter_batches(indices=np.asarray([0,1],dtype=np.int64))
        second=relation.iter_batches(indices=np.asarray([0,2],dtype=np.int64))
        assert next(first).calculation_index.tolist()==[0]; first.close()
        assert concatenate(second).calculation_index.tolist()==[0,2]


def test_member_label_length_and_failed_conversion_roll_back(tmp_path):
    # Selection now uses explicit identity columns, and structural model construction rejects malformed labels.
    with pytest.raises(ValueError): MemberBatch(np.asarray(['turn']),np.asarray([0,1],dtype=np.int64),np.asarray([0],dtype=np.int64),np.asarray(['s']),np.asarray([True]))
    with pytest.raises((ValueError,TypeError)): np.asarray([0,'bad',1],dtype=np.int64)
    with _relation(tmp_path) as relation:
        relation.append_batch(_candidate(range(3))); relation.seal()
        assert concatenate(relation.iter_batches(indices=np.asarray([1,2],dtype=np.int64))).calculation_index.tolist()==[1,2]


@pytest.mark.parametrize('items',[[0,1],[0,1,2,3]])
def test_member_iteration_length_mismatch_rolls_back(tmp_path,items):
    with pytest.raises(ValueError): MemberBatch(np.full(3,'turn'),np.asarray(items,dtype=np.int64),np.arange(3,dtype=np.int64),np.full(3,'s'),np.ones(3,dtype=np.bool_))


def test_relation_close_invalidates_member_views(tmp_path):
    relation=_relation(tmp_path); relation.append_batch(_candidate([0])); relation.seal()
    iterator=relation.iter_batches(indices=np.asarray([0],dtype=np.int64)); relation.close()
    with pytest.raises(RuntimeError,match='closed'): next(iterator)


@pytest.mark.parametrize("indices",[np.asarray([.5]),np.asarray([True]),np.asarray(['1']),np.asarray([[0]],dtype=np.int64)])
def test_selector_rejects_lossy_or_noncolumn_identity(tmp_path,indices):
    with _relation(tmp_path) as relation:
        relation.append_batch(_candidate([0]));relation.seal()
        with pytest.raises(ValueError,match="one-dimensional int64"):list(relation.iter_batches(indices=indices))
        assert concatenate(relation.iter_batches()).calculation_index.tolist()==[0]


def test_source_summary_reads_both_sources_without_business_payloads(tmp_path):
    expected=_candidate([0,1],optional=True)
    with _relation(tmp_path) as relation:
        relation.append_batch(expected);relation.seal()
        parts=list(relation.iter_source_batches())
        assert [len(part[0]) for part in parts]==[1,1]
        assert all(not column.flags.writeable for part in parts for column in part)
        assert np.concatenate([part[0] for part in parts]).tolist()==[0,1]
        assert np.concatenate([part[1] for part in parts]).tolist()==['source','source']
        assert np.concatenate([part[2] for part in parts]).tolist()==['other','other']


def test_typed_cross_driver_keeps_nan_and_sql_null_distinct(tmp_path):
    batch = replace(_candidate([0, 1, 2, 3]),
        heading=np.ma.array([np.nan, np.nan, np.inf, -np.inf], mask=[False, True, False, False]))
    with _relation(tmp_path, batch_size=2) as relation:
        relation.append_batch(batch)
        values = [row[0] for row in relation._connection.execute("SELECT heading FROM events ORDER BY ordinal").fetchall()]
        assert np.isnan(values[0]) and values[1] is None
        assert values[2:] == [np.inf, -np.inf]
        with pytest.raises(ValueError):
            relation.seal()


def test_typed_json_column_boundary_keeps_signed_zero_and_int64_extrema(tmp_path):
    batch = replace(_candidate([0, 1], optional=True), heading=np.array([-0.0, 0.0]),
        from_source_row_key=np.array([-(2**63), 2**63-1], dtype=np.int64),
        from_source_row_key_valid=np.array([True, True]), from_source_id=np.array(["中文🚗", "another"], dtype=object))
    with _relation(tmp_path, batch_size=2) as relation:
        relation.append_batch(batch)
        relation.seal()
        actual = next(relation.iter_batches())
        assert np.signbit(actual.heading).tolist() == [True, False]
        np.testing.assert_array_equal(actual.from_source_row_key, batch.from_source_row_key)
        np.testing.assert_array_equal(actual.from_source_id, batch.from_source_id)
