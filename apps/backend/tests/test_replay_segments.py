from unittest.mock import Mock

from backend.map_api import _TrackPoint
from backend.map_schemas import ReplayRequest
from backend.replay_segments import segment_features

DETECT = "12345678-1234-5678-1234-567812345678"


def point(identifier, kind="kept", crossing=None):
    return _TrackPoint(identifier, str(identifier) if kind == "kept" else None,
                       120 + identifier / 1000, 30, kind, crossing_id=crossing)


def test_crossing_highlights_only_its_source_segment():
    store = Mock()
    store._fetchall.return_value = [(90, 2)]
    points = [point(1), point(2), point(20, "crossing", 90),
              point(21, "crossing", 91), point(3), point(4)]
    features = segment_features(store, {("v", 1): points},
                                ReplayRequest(detect_id=DETECT, resolved_point_ids=[20]), DETECT)
    assert [f["properties"]["replay_role"] for f in features] == ["context", "highlight", "context"]
    assert features[1]["properties"]["resolved_point_ids"] == [2, 20, 21, 3]
    assert len({f["id"] for f in features}) == 3
    assert store._fetchall.call_args.args[1] == (DETECT, [90])


def test_ordinary_point_highlights_adjacent_segments_only():
    store = Mock()
    features = segment_features(store, {("v", 1): [point(i) for i in range(1, 6)]},
                                ReplayRequest(detect_id=DETECT, resolved_point_ids=[3]), DETECT)
    assert [f["properties"]["replay_role"] for f in features] == ["context", "highlight", "highlight", "context"]
    store._fetchall.assert_not_called()


def test_source_key_and_single_point():
    features = segment_features(Mock(), {("v", 1): [point(1)]}, ReplayRequest(source_row_keys=["1"]), None)
    assert features[0]["properties"]["replay_role"] == "highlight"
    assert features[0]["geometry"]["type"] == "Point"
