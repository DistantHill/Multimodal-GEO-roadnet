from unittest.mock import Mock

from backend.replay.track_points import group_track_rows
from backend.api.map_contracts import ReplayRequest
from backend.replay.build_segment_features import build_segment_features

DETECT = "12345678-1234-5678-1234-567812345678"


def point(identifier, kind="kept", crossing=None):
    return (1,"v",identifier, str(identifier) if kind == "kept" else None,
            120+identifier/1000,30,kind,crossing,None,None)


def test_crossing_highlights_only_its_source_segment():
    store = Mock()
    store._fetchall.return_value = [(90, 2)]
    points = [point(1), point(2), point(20, "crossing", 90),
              point(21, "crossing", 91), point(3), point(4)]
    features = build_segment_features(store, group_track_rows(points),
                                ReplayRequest(detect_id=DETECT, resolved_point_ids=[20]), DETECT)
    assert [f["properties"]["replay_role"] for f in features] == ["context", "highlight", "context"]
    assert features[1]["properties"]["resolved_point_ids"] == [2, 20, 21, 3]
    assert len({f["id"] for f in features}) == 3
    assert store._fetchall.call_args.args[1] == (DETECT, [90])


def test_ordinary_point_highlights_adjacent_segments_only():
    store = Mock()
    features = build_segment_features(store, group_track_rows([point(i) for i in range(1,6)]),
                                ReplayRequest(detect_id=DETECT, resolved_point_ids=[3]), DETECT)
    assert [f["properties"]["replay_role"] for f in features] == ["context", "highlight", "highlight", "context"]
    store._fetchall.assert_not_called()


def test_source_key_and_single_point():
    features = build_segment_features(Mock(), group_track_rows([point(1)]), ReplayRequest(source_row_keys=["1"]), None)
    assert features[0]["properties"]["replay_role"] == "highlight"
    assert features[0]["geometry"]["type"] == "Point"


def test_track_batch_zero_negative_and_null_source_keys_remain_distinct():
    from backend.replay.track_points import build_track_feature
    batch = group_track_rows([
        (1,"v",0,0,120.,30.,"kept",None,None,None),
        (1,"v",-1,-1,120.1,30.,"kept",None,None,None),
        (1,"v",None,None,120.2,30.,"injected",None,None,None),
    ])[("v",1)]
    assert batch.source_row_keys.dtype.name == "int64"
    assert batch.source_valid.tolist() == [True,True,False]
    assert not batch.lonlat.flags.writeable
    assert not hasattr(batch,"points")
    output = build_track_feature(("v",1),batch,None)["properties"]
    assert output["source_row_keys"] == ["0","-1",None]
    assert output["resolved_point_ids"] == [0,-1,None]
