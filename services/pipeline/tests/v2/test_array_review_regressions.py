"""Independent behavioral checks for the reviewed columnar identity boundary."""
from dataclasses import replace
import numpy as np
import pytest
from track.v2.models import SourceBatch, RefineConfig, SpatialContext, ReplayRefineResult, ExcludedBatch
from track.v2.trajectories.refine_trajectories import refine_vehicle


def _source():
    return SourceBatch("fixture", "original", np.array([[120.,30.],[120.001,30.]]),
                       np.array([0.,1.]), np.array([0.,0.]), np.array([1.,1.]),
                       np.array([0,-1],dtype=np.int64), np.array([False,True]),
                       np.array(["hash-zero","hash-negative"],dtype=object),
                       {"tag":np.array(["zero","negative"],dtype=object)})


def test_refine_numeric_identity_and_replay_kept_source_metadata_are_preserved():
    source = _source()
    result = refine_vehicle(source,
        spatial_context=SpatialContext(4326,32651,{"coordinate_semantics_version":2,
                         "enforce_area_of_use":False,"max_scale_error":0.1}),
        config=RefineConfig(100,45,135,50,1,20,10, 1))
    track = result.subtracks[0]
    assert track.source_row_key.dtype == np.int64
    assert track.source_row_key.tolist() == [0,-1]
    assert track.source_row_key_valid.tolist() == [True,True]
    assert track.xy.base is not None
    assert track.xy.shape == (2,2)
    assert not track.xy.flags.writeable
    replay = ReplayRefineResult("original", source, np.empty(0,dtype=np.int64),
                               np.empty(0,dtype=str), result.subtracks).as_dict()
    for original, active in zip(replay["original"], replay["active"], strict=True):
        assert active["point_ref"] == original["point_ref"]
        assert active["extra"] == original["extra"]
    assert replay["active"][0]["point_ref"]["stable"] is False
    assert replay["active"][0]["point_ref"]["source_row_key"] == "0"
    assert replay["active"][1]["point_ref"]["source_row_key"] == "-1"


@pytest.mark.parametrize("change", [
    {"heading":np.empty(1)},
    {"source_stable":np.array([[True],[False]])},
    {"source_row_key":np.array([0.,-1.])},
    {"source_stable":np.array([0,1],dtype=np.int64)},
])
def test_source_batch_rejects_misaligned_or_nonprimitive_identity_columns(change):
    with pytest.raises(ValueError):
        replace(_source(), **change)


def test_excluded_batch_rejects_misaligned_identity_reason_and_geometry():
    with pytest.raises(ValueError):
        ExcludedBatch("fixture",np.array([0,-1],dtype=np.int64),np.array(["stationary"]),
                      np.array([[120.,30.],[120.001,30.]]))
    with pytest.raises(ValueError):
        ExcludedBatch("fixture",np.array([0,-1],dtype=np.int64),np.array(["stationary"]*2),
                      np.array([120.,30.]))


@pytest.mark.parametrize("field",["calculation_index","refine_point_id","subtrack_index","source_row_key"])
def test_event_batch_rejects_noninteger_identity_columns(field):
    from .test_writer_relation import _turns
    with pytest.raises(ValueError):
        replace(_turns([1]), **{field:np.array([1.5])})


def test_member_batch_rejects_noninteger_index_and_nonboolean_mask():
    from track.v2.models import MemberBatch
    with pytest.raises(ValueError):
        MemberBatch(np.array(["turn"]),np.array([1.5]),np.array([0],dtype=np.int64),
                    np.array(["fixture"]),np.array([True]))
    with pytest.raises(ValueError):
        MemberBatch(np.array(["turn"]),np.array([1],dtype=np.int64),np.array([0],dtype=np.int64),
                    np.array(["fixture"]),np.array([1],dtype=np.int64))


@pytest.mark.parametrize("change", [
    {"heading": [0.]},
    {"source_stable": [True]},
    {"lonlat": [[120., 30.], [120.001, 30.]]},
    {"extra": {"tag": ["zero", "negative"]}},
])
def test_source_batch_rejects_mutable_nonarray_columns(change):
    with pytest.raises(ValueError):
        replace(_source(), **change)


def test_subtrack_and_member_batch_reject_nonarray_columns():
    from track.v2.models import MemberBatch
    result = refine_vehicle(_source(),
        spatial_context=SpatialContext(4326,32651,{"coordinate_semantics_version":2,
                         "enforce_area_of_use":False,"max_scale_error":0.1}),
        config=RefineConfig(100,45,135,50,1,20,10, 1))
    with pytest.raises(ValueError):
        replace(result.subtracks[0], source_row_key_valid=[True])
    with pytest.raises(ValueError):
        MemberBatch(np.array(["turn"]), np.array([1],dtype=np.int64),
                    np.array([0],dtype=np.int64),np.array(["fixture"]),[True])


def test_event_batch_rejects_nonarray_identity_mask():
    from .test_writer_relation import _turns
    with pytest.raises(ValueError):
        replace(_turns([1]), source_row_key_valid=[True])
