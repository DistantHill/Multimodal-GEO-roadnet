"""Columnar Turn relation lossless masks, bounded writes and corruption guards."""
from dataclasses import fields, replace
import inspect
import numpy as np
import pytest
from .config_factory import runtime_config
from .writer_relation_helpers import turn_batch
from track.v2.models import TurnBatch
from track.v2.candidates import turn_event_relation
from track.v2.candidates.turn_event_relation import TurnEventRelation


def _event(indices, **updates):
    indices=np.asarray(indices,dtype=np.int64); n=len(indices)
    values=dict(calculation_index=indices,point_index=indices,refine_point_id=100+indices,xy=np.column_stack((indices.astype(np.float64)+1,np.full(n,2.))),
        lonlat=np.tile([120.,30.],(n,1)),timestamp=indices.astype(np.float64)+10.5,
        entry_bearing=np.full(n,20.),exit_bearing=np.full(n,50.),turn_deg=np.full(n,30.),score=np.full(n,30.),
        source_id=np.full(n,'source'),source_row_key=indices.copy(),generated_reason=np.full(n,''),
        g_star_z=np.full(n,-1.25),g_star_z_valid=np.ones(n,dtype=np.bool_))
    values.update(updates)
    return turn_batch(n,**values)


def _relation(tmp_path, *, batch_size=1):
    return TurnEventRelation(runtime_config(duckdb_memory_limit='128MB',duckdb_threads=1,
        duckdb_max_temp_directory_size='256MB'),tmp_path,batch_size=batch_size,relation_id='test-relation')


def concatenate(batches):
    batches=list(batches)
    if not batches: return _event([])
    return TurnBatch(**{field.name:np.concatenate([getattr(batch,field.name) for batch in batches]) for field in fields(TurnBatch)})


def assert_batch_equal(actual,expected):
    for field in fields(TurnBatch): np.testing.assert_array_equal(getattr(actual,field.name),getattr(expected,field.name))


def test_empty_relation_seals_and_repeats(tmp_path):
    with _relation(tmp_path) as relation:
        relation.seal(); assert len(relation)==0
        assert list(relation.iter_batches())==list(relation.iter_batches())==[]
        assert list(relation.iter_coordinate_batches())==[]


def test_lossless_optional_source_order_index_and_batch_one(tmp_path):
    expected=_event([0,1],refine_point_id_valid=np.asarray([True,False]),g_star_z_valid=np.asarray([True,False]))
    with _relation(tmp_path) as relation:
        relation.append_batch(expected)
        with pytest.raises(RuntimeError,match='sealed'): list(relation.iter_batches())
        relation.seal()
        for _ in range(2): assert_batch_equal(concatenate(relation.iter_batches()),expected)
        assert_batch_equal(concatenate(relation.iter_batches(indices=np.asarray([1],dtype=np.int64))),expected.take(np.asarray([1])))
        parts=list(relation.iter_coordinate_batches())
        np.testing.assert_array_equal(np.concatenate([part[0] for part in parts]),[[1.,2.],[2.,2.]])
        assert np.concatenate([part[1] for part in parts]).tolist()==[0,1]
        assert all(not column.flags.writeable for part in parts for column in part)


@pytest.mark.parametrize('batch_size',[1,2,5])
def test_extend_is_lossless_bounded_and_preserves_nullable_bigints(tmp_path,batch_size):
    huge=2**60+7
    expected=_event([0,1,2],refine_point_id=np.asarray([100,huge,102],dtype=np.int64),
        subtrack_index=np.asarray([1,huge+1,1],dtype=np.int64),point_index=np.asarray([0,huge+2,2],dtype=np.int64),
        source_row_key=np.asarray([0,-1,huge+3],dtype=np.int64),source_row_key_valid=np.asarray([True,False,True]))
    with _relation(tmp_path,batch_size=batch_size) as relation:
        relation.extend(iter([expected])); assert len(relation)==3; relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),expected)


def test_extend_typed_masks_preserve_false_empty_and_null_fields(tmp_path):
    expected=_event([0],source_row_key_valid=np.asarray([False]),refine_point_id_valid=np.asarray([False]),g_star_z_valid=np.asarray([False]))
    with _relation(tmp_path) as relation:
        relation.extend([expected]); relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),expected)
        actual=concatenate(relation.iter_batches())
        assert not actual.source_row_key_valid[0] and not actual.refine_point_id_valid[0] and not actual.g_star_z_valid[0]
        assert actual.generated_reason.tolist()==['']


@pytest.mark.parametrize('updates',[{'refine_point_id':np.asarray([1.5])},{'source_row_key_valid':np.asarray(['false'])}])
def test_extend_rejects_lossy_nullable_typed_values(tmp_path,updates):
    with _relation(tmp_path) as relation:
        with pytest.raises((TypeError,ValueError)): relation.extend([_event([0],**updates)])
        assert len(relation)==0


def test_extend_rolls_back_failed_batch_but_keeps_prior_commit(tmp_path):
    with _relation(tmp_path) as relation:
        def batches():
            yield _event([0])
            yield _event([1],refine_point_id=np.asarray(['bad']))
        with pytest.raises((TypeError,ValueError)): relation.extend(batches())
        assert len(relation)==1 and relation._connection.execute('SELECT count(*) FROM events').fetchone()==(1,)
        relation.extend([_event([1])]); relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),_event([0,1]))


def test_extend_null_source_id_follows_seal_rejection(tmp_path):
    relation=_relation(tmp_path); relation.extend([_event([0],source_id=np.asarray([None],dtype=object))])
    with pytest.raises(ValueError): relation.seal()


def test_extend_matches_append_bool_to_varchar_coercion(tmp_path):
    # Old source_ids row coercion is gone; both batch write paths preserve canonical source strings.
    expected=_event([0,1],source_id=np.asarray(['true','string']))
    with _relation(tmp_path/'append') as appended,_relation(tmp_path/'extend') as extended:
        appended.append_batch(expected); extended.extend([expected]); appended.seal(); extended.seal()
        assert_batch_equal(concatenate(appended.iter_batches()),concatenate(extended.iter_batches()))


def test_extend_streams_one_events_source_ids_in_bounded_chunks(tmp_path):
    expected=_event(range(7),source_id=np.asarray([f'source-{index}' for index in range(7)]))
    with _relation(tmp_path,batch_size=2) as relation:
        relation.append_many(iter([expected])); relation.seal()
        parts=list(relation.iter_batches()); assert [len(part) for part in parts]==[2,2,2,1]
        assert_batch_equal(concatenate(parts),expected)


def test_extend_nan_cannot_bypass_relation_validation(tmp_path):
    relation=_relation(tmp_path); relation.extend([_event([0],xy=np.asarray([[np.nan,2.]]))])
    with pytest.raises(ValueError): relation.seal()


def test_extend_rejects_nullable_nan_before_scanner_can_map_it_to_null(tmp_path):
    with _relation(tmp_path) as relation:
        relation.extend([_event([0])])
        with pytest.raises(ValueError): relation.extend([_event([1],g_star_z=np.asarray([np.nan]))])
        assert len(relation)==1; relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),_event([0]))


def test_extend_source_insert_failure_rolls_back_event_and_unregisters(tmp_path):
    first=_event([0],source_id=np.asarray(['first']))
    with _relation(tmp_path,batch_size=1) as relation:
        relation.extend([first]); relation._connection.execute('CREATE UNIQUE INDEX turn_source_unique ON events(source_id)')
        with pytest.raises(Exception,match='unique|duplicate|constraint'): relation.extend([_event([1,2],source_id=np.asarray(['duplicate','duplicate']))])
        assert len(relation)==1 and relation._connection.execute('SELECT count(*) FROM events').fetchone()==(1,)
        assert relation._connection.execute("SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'incoming_%'").fetchone()==(0,)
        second=_event([1],source_id=np.asarray(['second'])); relation.extend([second]); relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),concatenate([first,second]))


@pytest.mark.parametrize('sql',[
    "UPDATE events SET timestamp='Infinity'::DOUBLE",
    "UPDATE events SET entry_bearing='NaN'::DOUBLE",
    "UPDATE events SET exit_bearing='-Infinity'::DOUBLE",
    "UPDATE events SET point_kind='bad'",
    'UPDATE events SET subtrack_index=-1,refine_point_id_valid=true',
    'UPDATE events SET point_index=-1,refine_point_id_valid=true',
    'UPDATE events SET ordinal=2,calculation_index=2',
    'UPDATE events SET ordinal=2','UPDATE events SET calculation_index=2','UPDATE events SET vehicle_id=NULL',
    "UPDATE events SET lonlat_0='NaN'::DOUBLE",'UPDATE events SET turn_deg=180','UPDATE events SET score=29',
    "UPDATE events SET source_id=''",'UPDATE events SET refine_point_id_valid=NULL',
    'UPDATE events SET refine_point_id=-1,refine_point_id_valid=true',
    "UPDATE events SET g_star_z='NaN'::DOUBLE,g_star_z_valid=true",'UPDATE events SET source_row_key_valid=NULL'])
def test_seal_rejects_corruption_and_closes(tmp_path,sql):
    relation=_relation(tmp_path); relation.append_batch(_event([0])); relation._connection.execute(sql)
    with pytest.raises(ValueError): relation.seal()
    with pytest.raises(RuntimeError,match='closed'): len(relation)


def test_sealed_and_closed_lifecycle(tmp_path):
    relation=_relation(tmp_path); relation.append_batch(_event([0])); relation.seal(); relation.seal()
    with pytest.raises(RuntimeError,match='sealed'): relation.append_batch(_event([1]))
    relation.close(); relation.close()
    with pytest.raises(RuntimeError,match='closed'): len(relation)
    with pytest.raises(RuntimeError,match='closed'): list(relation.iter_batches())
    with pytest.raises(RuntimeError,match='closed'): list(relation.iter_coordinate_batches())


@pytest.mark.parametrize('batch_size',[True,0])
def test_invalid_batch_size_is_rejected(tmp_path,batch_size):
    with pytest.raises(ValueError,match='batch_size'): _relation(tmp_path,batch_size=batch_size)


@pytest.mark.parametrize('root',['relative','/'])
def test_unsafe_data_root_is_rejected(root):
    with pytest.raises(ValueError,match='absolute and non-root'): TurnEventRelation(runtime_config(),root)


@pytest.mark.parametrize('relation_id',['.','..','bad/name','bad\x00name'])
def test_unsafe_relation_identity_is_rejected(tmp_path,relation_id):
    with pytest.raises(ValueError,match='relation_id'): TurnEventRelation(runtime_config(),tmp_path,relation_id=relation_id)


def test_connection_failure_removes_only_its_empty_leaf(tmp_path,monkeypatch):
    sibling=tmp_path/'duckdb'/'turn-event-relations'/'keep'; sibling.mkdir(parents=True)
    def fail(*args,**kwargs): raise RuntimeError('open failed')
    monkeypatch.setattr(turn_event_relation.duckdb,'connect',fail)
    with pytest.raises(RuntimeError,match='open failed'): TurnEventRelation(runtime_config(),tmp_path,relation_id='failed')
    assert sibling.is_dir() and not (sibling.parent/'failed').exists()


def test_relation_identity_collision_fails_closed(tmp_path):
    occupied=tmp_path/'duckdb'/'turn-event-relations'/'occupied'; occupied.mkdir(parents=True)
    marker=occupied/'owned-by-other-run'; marker.write_text('keep')
    with pytest.raises(FileExistsError): TurnEventRelation(runtime_config(),tmp_path,relation_id='occupied')
    assert marker.read_text()=='keep'


def test_constructor_failure_after_connect_closes_and_cleans(tmp_path,monkeypatch):
    connection=turn_event_relation.duckdb.connect(":memory:")
    monkeypatch.setattr(turn_event_relation.duckdb,"connect",lambda *args,**kwargs:connection)
    monkeypatch.setattr(TurnEventRelation,"_create_schema",lambda self:1/0)
    with pytest.raises(ZeroDivisionError): TurnEventRelation(runtime_config(),tmp_path,relation_id="failed")
    with pytest.raises(Exception): connection.execute("SELECT 1")
    assert not (tmp_path/"duckdb"/"turn-event-relations"/"failed").exists()


def test_runtime_limits_and_columnar_implementation(tmp_path):
    with _relation(tmp_path) as relation:
        settings=dict(relation._connection.execute("SELECT name,value FROM duckdb_settings() WHERE name IN ('threads','memory_limit','max_temp_directory_size','temp_directory','autoload_known_extensions','autoinstall_known_extensions')").fetchall())
        assert settings['threads']=='1' and settings['memory_limit']=='122.0 MiB'
        assert settings['max_temp_directory_size']=='244.1 MiB'
        assert settings['autoload_known_extensions']==settings['autoinstall_known_extensions']=='false'
        assert settings['temp_directory']==str(relation._temp_directory)
    assert 'fetchmany' in inspect.getsource(TurnEventRelation.iter_batches)
    assert 'unnest' in inspect.getsource(TurnEventRelation.append_batch)
    for forbidden in ('sqlite','pickle','struct','temporarydirectory'): assert forbidden not in inspect.getsource(turn_event_relation).lower()


@pytest.mark.parametrize("kind",["p","i","kept","injected"])
def test_valid_refined_kinds_and_numeric_zero_negative_source_keys(tmp_path,kind):
    expected=_event([0,1],point_kind=np.full(2,kind),source_row_key=np.asarray([0,-1],dtype=np.int64),
        source_row_key_valid=np.asarray([True,True]),refine_point_id=np.asarray([0,1],dtype=np.int64))
    with _relation(tmp_path) as relation:
        relation.append_batch(expected);relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),expected)


def test_invalid_nullable_float_placeholder_remains_nan(tmp_path):
    expected=_event([0],g_star_z=np.asarray([np.nan]),g_star_z_valid=np.asarray([False]),
        refine_point_id=np.asarray([-1],dtype=np.int64),refine_point_id_valid=np.asarray([False]))
    with _relation(tmp_path) as relation:
        relation.append_batch(expected);relation.seal()
        assert_batch_equal(concatenate(relation.iter_batches()),expected)


@pytest.mark.parametrize("indices",[np.asarray([.5]),np.asarray([True]),np.asarray(['1']),np.asarray([[0]],dtype=np.int64)])
def test_selector_rejects_lossy_or_noncolumn_identity(tmp_path,indices):
    with _relation(tmp_path) as relation:
        relation.append_batch(_event([0]));relation.seal()
        with pytest.raises(ValueError,match="one-dimensional int64"):list(relation.iter_batches(indices=indices))
        assert concatenate(relation.iter_batches()).calculation_index.tolist()==[0]


def test_typed_driver_boundary_preserves_nan_null_inf_unicode_and_int64(tmp_path):
    expected = _event([0, 1, 2, 3],
        timestamp=np.ma.array([np.nan, np.nan, np.inf, -np.inf], mask=[False, True, False, False]),
        source_row_key=np.array([-(2**63), 2**63-1, 0, -1], dtype=np.int64),
        source_id=np.array(["中文🚗", "", "source", "source"], dtype=object))
    with _relation(tmp_path, batch_size=2) as relation:
        relation.append_batch(expected)
        rows = relation._connection.execute("SELECT timestamp,source_row_key,source_id FROM events ORDER BY ordinal").fetchall()
        assert np.isnan(rows[0][0]) and rows[1][0] is None
        assert rows[2][0] == np.inf and rows[3][0] == -np.inf
        assert [row[1] for row in rows] == [-(2**63), 2**63-1, 0, -1]
        assert [row[2] for row in rows] == ["中文🚗", "", "source", "source"]
        with pytest.raises(ValueError):
            relation.seal()
