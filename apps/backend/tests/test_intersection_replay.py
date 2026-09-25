from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.map_errors import MapDataError, MapNotFoundError
from backend.stores.map_layers import PostgresMapStore
from backend.api.map_contracts import ReplayRequest


DETECT_ID = "12345678-1234-5678-1234-567812345678"


def make_store(rows=()):
    store = PostgresMapStore(Mock())
    store._fetchone = Mock(return_value=(DETECT_ID, 42))
    store._fetchall = Mock(return_value=list(rows))
    store._resolve_detect = Mock(return_value=SimpleNamespace(refine=SimpleNamespace(
        refine_id="refine-1", quoted_source_table='"public"."source_points"',
        source_key_sql_type="bigint",
    )))
    store._verify_source_fingerprint = Mock()
    return store


def member(point_id, source_key, timestamp, injected_id=None):
    return (point_id, source_key, injected_id, "vehicle-1", timestamp,
            120.1, 30.2, 90.0, 10.0, [{"kind": "turn_event", "id": 9}])


def test_intersection_reuses_pipeline_helper_and_preserves_injected_identity(monkeypatch):
    from track.v2.validation import resolve_point_references

    helper = Mock(wraps=resolve_point_references.replay_points)
    monkeypatch.setattr(resolve_point_references, "replay_points", helper)
    store = make_store([member(1, "7", 20), member(2, None, 10, injected_id=7)])
    result = store.replay(ReplayRequest(intersection_id=5))
    helper.assert_called_once()
    store._verify_source_fingerprint.assert_not_called()
    assert [feature["id"] for feature in result["features"]] == [2, 1]
    assert all(feature["geometry"]["type"] == "Point" for feature in result["features"])
    assert result["features"][0]["properties"]["candidate_intersection_id"] == 42
    sql, params = store._fetchall.call_args.args
    assert "FROM turn_events" in sql and "FROM tracks_cross" in sql
    assert "successor.refined_sequence_in_subtrack = start.refined_sequence_in_subtrack + 1" in sql
    assert params[:4] == (DETECT_ID, 42, DETECT_ID, 42)


def test_intersection_missing_or_wrong_detect():
    store = make_store()
    store._fetchone.return_value = None
    with pytest.raises(MapNotFoundError):
        store.replay(ReplayRequest(intersection_id=5))
    store._fetchone.return_value = (DETECT_ID, 42)
    with pytest.raises(MapNotFoundError):
        store.replay(ReplayRequest(intersection_id=5, detect_id="22345678-1234-5678-1234-567812345678"))
    store._fetchall.assert_not_called()


def test_intersection_does_not_silently_truncate():
    store = make_store([member(1, "1", 1), member(2, "2", 2), member(3, "3", 3)])
    with pytest.raises(MapDataError, match="max_points"):
        store.replay(ReplayRequest(intersection_id=5, max_points=2))


def test_intersection_missing_source_fails():
    store = make_store([member(1, "1", None)])
    with pytest.raises(MapDataError, match="构成点缺失"):
        store.replay(ReplayRequest(intersection_id=5))


def test_intersection_empty_members_returns_empty_collection():
    assert make_store().replay(ReplayRequest(intersection_id=5))["features"] == []


@pytest.mark.parametrize("selection", [{"source_row_keys": ["1"]}, {"bbox": [0, 0, 1, 1]}])
def test_intersection_selection_is_exclusive(selection):
    with pytest.raises(ValidationError):
        ReplayRequest(intersection_id=5, **selection)
