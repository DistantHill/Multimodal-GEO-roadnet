from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from backend.api.map_contracts import ReplayRequest
from backend.map_errors import MapDataError, MapNotFoundError
from backend.stores.map_layers import PostgresMapStore

DETECT = "12345678-1234-5678-1234-567812345678"
REFINE = "22345678-1234-5678-1234-567812345678"


def point(identifier, subtrack=1, kind="kept"):
    return (subtrack, str(subtrack), identifier, identifier if kind == "kept" else None,
            108.0 + identifier / 1000, 34., kind, None, None, None)


def store_with_tracks():
    store = PostgresMapStore(Mock())
    refine = SimpleNamespace(refine_id=REFINE, quoted_source_table='"trajectory_points"', source_key_sql_type="bigint")
    store._resolve_detect = Mock(return_value=SimpleNamespace(detect_id=DETECT, refine=refine))
    store._resolve_refine = Mock(return_value=refine)
    store._fetch_resolved_tracks = Mock(return_value=[point(i) for i in range(1, 5)] + [point(i, 2) for i in range(11, 15)])
    store._select_resolved_tracks = Mock(return_value=([1, 2], {2, 12}, False))
    return store


def test_cross_event_highlights_exact_two_segments_not_adjacent_neighbors():
    store = store_with_tracks()
    store._fetchall = Mock(return_value=[(90, 2, 12)])
    result = store.replay(ReplayRequest(detect_id=DETECT, tracks_cross_ids=[90]))
    highlights = [f for f in result["features"] if f["properties"]["replay_role"] == "highlight"]
    assert [f["properties"]["resolved_point_ids"] for f in highlights] == [[2, 3], [12, 13]]
    assert len(result["features"]) == 6
    assert all(f["geometry"]["type"] == "LineString" for f in result["features"])
    assert result["metadata"]["context_segment_count"] == 4


def test_turn_event_keeps_injected_endpoint_and_full_subtrack_context():
    store = store_with_tracks()
    store._fetchall = Mock(return_value=[(80, 3)])
    store._select_resolved_tracks.return_value = ([1], {3}, False)
    store._fetch_resolved_tracks.return_value = [point(1), point(2), point(3, kind="injected"), point(4), point(5)]
    result = store.replay(ReplayRequest(detect_id=DETECT, turn_event_ids=[80]))
    assert [f["properties"]["replay_role"] for f in result["features"]] == ["context", "highlight", "highlight", "context"]
    assert result["features"][1]["properties"]["point_kinds"] == ["kept", "injected"]
    assert result["features"][1]["geometry"]["coordinates"][-1] == [108.003, 34.]


def test_two_intersections_expand_shared_events_once():
    store = store_with_tracks()
    store._fetchall = Mock(return_value=[(90, 2, 12)])
    members = {"features": [{"properties": {"events": [{"kind": "tracks_cross", "id": 90}]}}] * 2,
               "metadata": {"selection": "intersection_ids", "intersection_ids": [1, 2], "detect_id": DETECT,
                            "operation": "replay", "returned_point_count": 4, "truncated": False}}
    with patch("backend.replay.select_intersection_members.replay_intersections", return_value=members):
        result = store.replay(ReplayRequest(intersection_ids=[1, 2]))
    assert len(result["features"]) == 6
    assert result["metadata"]["intersection_ids"] == [1, 2]
    assert result["metadata"]["highlight_segment_count"] == 2
    store._fetchall.assert_called_once()


def test_injected_replay_uses_kept_brackets_without_detect():
    store = store_with_tracks()
    store._fetchall = Mock(side_effect=[[(70, REFINE, 1, 1), (71, REFINE, 1, 1)],
                                       [(70, 2, 3), (71, 2, 3)],
                                       [(1, "v", None, i, 108 + i / 1000, 34, "kept", None, None, None) for i in range(1, 5)]])
    result = store.replay(ReplayRequest(injected_point_ids=[70, 71], refine_id=REFINE))
    store._resolve_detect.assert_not_called()
    store._resolve_refine.assert_called_once_with(REFINE)
    assert [f["properties"]["replay_role"] for f in result["features"]] == ["context", "highlight", "context"]
    assert result["features"][1]["properties"]["source_row_keys"] == ["2", "3"]
    assert result["metadata"]["detect_id"] is None
    assert "ST_X(source.geom)" in store._fetchall.call_args.args[0]


def test_event_missing_ids_and_resource_limits_fail_without_partial_output():
    store = store_with_tracks()
    store._fetchall = Mock(return_value=[])
    with pytest.raises(MapNotFoundError):
        store.replay(ReplayRequest(detect_id=DETECT, tracks_cross_ids=[90]))
    store._fetchall.return_value = [(90, 2, 12)]
    with pytest.raises(MapDataError, match="max_points"):
        store.replay(ReplayRequest(detect_id=DETECT, tracks_cross_ids=[90], max_points=2))


@pytest.mark.parametrize("selection", [
    {"tracks_cross_ids": [1]}, {"turn_event_ids": [1]},
    {"detect_id": DETECT, "injected_point_ids": [1]},
    {"detect_id": DETECT, "tracks_cross_ids": [1], "source_row_keys": ["1"]},
    {"detect_id": DETECT, "tracks_cross_ids": []},
])
def test_typed_replay_rejects_invalid_context_and_mixed_namespaces(selection):
    with pytest.raises(ValidationError):
        ReplayRequest(**selection)


def test_event_and_injected_queries_against_postgis():
    import os
    import psycopg
    if not os.environ.get("ROADNET_TEST_DSN"):
        pytest.skip("ROADNET_TEST_DSN is not set")
    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as connection:
        connection.execute("SET LOCAL search_path TO pg_temp, public, pg_catalog")
        connection.execute("""
            CREATE TEMP TABLE source_points (id bigint, lon float8, lat float8,
                timestamp float8 DEFAULT 1, heading float8 DEFAULT 90, speed float8 DEFAULT 10,
                geom geometry(Point,4326));
            CREATE TEMP TABLE kept_trajectory_points (id bigint, refine_id uuid, subtrack_index int,
                sequence_in_subtrack int, source_row_key bigint, vehicle_id text);
            CREATE TEMP TABLE injected_points (id bigint, refine_id uuid, subtrack_index int,
                after_sequence_in_subtrack int, vehicle_id text, lon float8, lat float8,
                timestamp float8 DEFAULT 1, heading float8 DEFAULT 90, speed float8 DEFAULT 10,
                geom geometry(Point,4326));
            CREATE TEMP TABLE resolved_subtrack_points (id bigint, detect_id uuid, subtrack_index int,
                sequence_in_subtrack int, refined_sequence_in_subtrack int, kept_point_id bigint,
                injected_point_id bigint, crossing_point_id bigint);
            CREATE TEMP TABLE tracks_cross (id bigint, detect_id uuid, candidate_intersection_id bigint,
                from_segment_start_point_id bigint, to_segment_start_point_id bigint, geom geometry(Point,4326));
            CREATE TEMP TABLE turn_events (id bigint, detect_id uuid, candidate_intersection_id bigint,
                resolved_point_id bigint);
            CREATE TEMP TABLE turn_evnets_gemo (LIKE turn_events, geom geometry(Point,4326));
            CREATE TEMP TABLE candidate_entry_exit_points (id bigint, detect_id uuid,
                candidate_intersection_id bigint, crossing_type int, segment_start_point_id bigint,
                geom geometry(Point,4326));
            CREATE TEMP TABLE intersections (id bigint, detect_id uuid, candidate_intersection_id bigint);
        """)
        for subtrack, ids in ((1, range(1, 5)), (2, range(11, 15))):
            for sequence, identifier in enumerate(ids):
                connection.execute("INSERT INTO source_points (id,lon,lat,geom) VALUES (%s,0,0,ST_SetSRID(ST_MakePoint(%s,34),4326))",
                                   (identifier, 108 + identifier / 1000))
                connection.execute("INSERT INTO kept_trajectory_points VALUES (%s,%s,%s,%s,%s,%s)",
                                   (identifier, REFINE, subtrack, sequence, identifier, str(subtrack)))
                refined = sequence + (1 if subtrack == 1 and sequence >= 2 else 0)
                final = refined + (1 if subtrack == 1 and sequence >= 2 else 0)
                connection.execute("INSERT INTO resolved_subtrack_points VALUES (%s,%s,%s,%s,%s,%s,NULL,NULL)",
                                   (identifier, DETECT, subtrack, final, refined, identifier))
        connection.execute("INSERT INTO injected_points (id,refine_id,subtrack_index,after_sequence_in_subtrack,vehicle_id,lon,lat,geom) "
                           "VALUES (70,%s,1,1,'1',108.0025,34,ST_SetSRID(ST_MakePoint(108.0025,34),4326))", (REFINE,))
        connection.execute("INSERT INTO resolved_subtrack_points VALUES (70,%s,1,3,2,NULL,70,NULL),(90,%s,1,2,NULL,NULL,NULL,90)", (DETECT, DETECT))
        connection.execute("INSERT INTO candidate_entry_exit_points VALUES (90,%s,42,0,2,ST_SetSRID(ST_MakePoint(108.0022,34),4326))", (DETECT,))
        connection.execute("INSERT INTO tracks_cross VALUES (9,%s,42,2,12,ST_SetSRID(ST_MakePoint(108.0022,34),4326))", (DETECT,))
        connection.execute("INSERT INTO turn_events VALUES (8,%s,43,70)", (DETECT,))
        connection.execute("INSERT INTO turn_evnets_gemo SELECT *,ST_SetSRID(ST_MakePoint(108.0025,34),4326) FROM turn_events")
        connection.execute("INSERT INTO intersections VALUES (101,%s,42),(202,%s,43)", (DETECT, DETECT))
        store = PostgresMapStore(connection)
        refine = SimpleNamespace(refine_id=REFINE, quoted_source_table='"source_points"', source_key_sql_type="bigint")
        store._resolve_detect = Mock(return_value=SimpleNamespace(detect_id=DETECT, refine=refine))
        store._resolve_refine = Mock(return_value=refine)
        result = store.replay(ReplayRequest(detect_id=DETECT, tracks_cross_ids=[9]))
        highlighted = [f for f in result["features"] if f["properties"]["replay_role"] == "highlight"]
        assert [f["properties"]["resolved_point_ids"] for f in highlighted] == [[2, 90, 70], [12, 13]]
        assert highlighted[0]["geometry"]["coordinates"][-1] == [108.0025, 34]
        assert highlighted[0]["geometry"]["coordinates"][0] == [108.002, 34]
        result = store.replay(ReplayRequest(intersection_ids=[101, 202]))
        assert result["metadata"]["highlight_segment_count"] == 3
        assert result["metadata"]["intersection_ids"] == [101, 202]
        store._resolve_detect.reset_mock()
        result = store.replay(ReplayRequest(injected_point_ids=[70]))
        store._resolve_detect.assert_not_called()
        highlighted = [f for f in result["features"] if f["properties"]["replay_role"] == "highlight"]
        assert highlighted[0]["properties"]["source_row_keys"] == ["2", "3"]
        assert highlighted[0]["geometry"]["coordinates"] == [[108.002, 34], [108.003, 34]]
        for layer_id, expected in (("tracks_cross", 9), ("turn_event_geom", 8)):
            layer = store.read_layer(layer_id, bbox=(108,33,109,35), detect_id=DETECT, fit_id=None, limit=10)
            assert layer["features"][0]["id"] == expected
            assert layer["features"][0]["geometry"]["type"] == "Point"
        connection.execute("UPDATE source_points SET geom=NULL WHERE id=2")
        with pytest.raises(MapDataError, match="geom"):
            store.replay(ReplayRequest(injected_point_ids=[70]))
        connection.rollback()
