"""Centerline Replay selects persisted Clip members rather than nearby points."""
from dataclasses import replace
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.api.map_contracts import ReplayRequest
from backend.map_errors import MapDataError, MapNotFoundError
from backend.stores.map_layers import PostgresMapStore


FIT_ID = "12345678-1234-5678-1234-567812345679"
DETECT_ID = "12345678-1234-5678-1234-567812345678"
REFINE_ID = "12345678-1234-5678-1234-567812345670"


def replay_request(**changes):
    return ReplayRequest(**{
        "centerline_ids": [81], "fit_id": FIT_ID, "detect_id": DETECT_ID,
        "refine_id": REFINE_ID, **changes,
    })


def make_store(*, reverse=False):
    store = PostgresMapStore(Mock())
    store._resolve_fit = Mock(return_value=(FIT_ID, DETECT_ID))
    store._resolve_detect = Mock(return_value=SimpleNamespace(refine=SimpleNamespace(
        refine_id=REFINE_ID, quoted_source_table='"source_points"',
        source_key_sql_type="bigint",
    )))
    store._verify_source_fingerprint = Mock()
    # Persistent Refine vehicle identity may differ from the raw source field.
    vehicle = "vehicle#partition-1"
    points = [
        (1, 0, 7, "crossing", None, None, 100, 11, 0,
         120.1, 30.1, None, None, None, vehicle, None, None),
        (2, 1, 7, "kept", "0", None, None, None, None,
         120.2, 30.2, 20, 90, 10, vehicle, None, vehicle),
        (3, 2, 7, "crossing", None, None, 101, 12, 1,
         120.3, 30.3, None, None, None, vehicle, None, None),
    ]
    store.centerlines = [(81, DETECT_ID, "directed", 12 if reverse else 11,
                          11 if reverse else 12, int(not reverse), int(reverse))]
    store.manifest = [(0, 100, 101, reverse)]
    store.endpoints = [(100, 0, 9, 11, 1, 7, 0), (101, 1, 10, 12, 3, 7, 2)]
    store.points = points

    def fetch(sql, params):
        if "FROM centerlines" in sql:
            return store.centerlines
        if "FROM centerline_source_clips" in sql:
            return store.manifest
        if "FROM candidate_entry_exit_points AS crossing" in sql:
            return store.endpoints
        return store.points

    store._fetchall = Mock(side_effect=fetch)
    return store


@pytest.mark.parametrize("reverse", [False, True])
def test_centerline_returns_exact_points_and_oriented_source_clip(reverse):
    store = make_store(reverse=reverse)
    result = store.replay(replay_request())
    points, clip = result["features"][:3], result["features"][3]
    assert [point["properties"]["resolved_point_id"] for point in points] == [1, 2, 3]
    assert points[1]["properties"]["source_row_key"] == "0"
    assert points[1]["properties"]["source_vehicle_id"] is None
    assert points[1]["properties"]["vehicle_id"] == "vehicle#partition-1"
    assert clip["properties"]["resolved_point_ids"] == ([3, 2, 1] if reverse else [1, 2, 3])
    assert clip["properties"]["persisted_start_resolved_point_id"] == 1
    assert clip["properties"]["persisted_end_resolved_point_id"] == 3
    assert clip["geometry"]["coordinates"][0] == ([120.3, 30.3] if reverse else [120.1, 30.1])
    assert result["metadata"]["returned_point_count"] == 3
    assert result["metadata"]["returned_clip_count"] == 1
    assert result["metadata"]["source_fingerprint_verified"] is False
    assert result["metadata"]["truncated"] is False
    store._verify_source_fingerprint.assert_not_called()


def test_shared_points_keep_all_clip_memberships_without_spending_point_budget_twice():
    store = make_store()
    store.centerlines.append((82, DETECT_ID, "combined", 11, 12, 1, 0))
    result = store.replay(replay_request(centerline_ids=[81, 82], max_points=3))
    assert len(result["features"]) == 5
    assert result["metadata"]["membership_reference_count"] == 6
    assert all(feature["properties"]["centerline_ids"] == [81, 82]
               for feature in result["features"][:3])
    assert all(len(feature["properties"]["clip_sources"]) == 2
               for feature in result["features"][:3])
    queries = [call.args[0] for call in store._fetchall.call_args_list]
    assert sum("FROM candidate_entry_exit_points AS crossing" in sql for sql in queries) == 1
    assert sum("FROM resolved_subtrack_points AS resolved" in sql for sql in queries) == 1


def test_shared_clip_keeps_both_directions_and_does_not_cache_between_requests():
    store = make_store()
    store.centerlines.append((82, DETECT_ID, "combined", 12, 11, 0, 1))
    fetch = store._fetchall.side_effect

    def fetch_direction(sql, params):
        if "FROM centerline_source_clips" in sql and params[0] == 82:
            return [(0, 100, 101, True)]
        return fetch(sql, params)

    store._fetchall.side_effect = fetch_direction
    request = replay_request(centerline_ids=[81, 82], max_points=3)
    result = store.replay(request)
    assert result["features"][-2]["properties"]["resolved_point_ids"] == [1, 2, 3]
    assert result["features"][-1]["properties"]["resolved_point_ids"] == [3, 2, 1]
    assert result["metadata"]["membership_reference_count"] == 6
    changed = list(store.points[1])
    changed[9] = 120.25
    store.points[1] = tuple(changed)
    result = store.replay(request)
    assert result["features"][1]["geometry"]["coordinates"][0] == 120.25


def test_distinct_clips_share_endpoint_query_but_keep_separate_members():
    store = make_store()
    store.centerlines.append((82, DETECT_ID, "combined", 11, 12, 1, 0))
    store.endpoints += [(200, 0, 9, 11, 4, 8, 0), (201, 1, 10, 12, 6, 8, 2)]
    other_points = []
    for original in store.points:
        row = list(original)
        row[0] += 3
        row[2] = 8
        if row[6] is not None:
            row[6] += 100
        other_points.append(tuple(row))
    fetch = store._fetchall.side_effect

    def fetch_distinct(sql, params):
        if "FROM centerline_source_clips" in sql and params[0] == 82:
            return [(0, 200, 201, False)]
        if "FROM resolved_subtrack_points AS resolved" in sql and params[5] == 8:
            return other_points
        return fetch(sql, params)

    store._fetchall.side_effect = fetch_distinct
    result = store.replay(replay_request(centerline_ids=[81, 82]))
    assert result["metadata"]["returned_point_count"] == 6
    assert result["features"][-1]["properties"]["resolved_point_ids"] == [4, 5, 6]
    endpoint_calls = [call for call in store._fetchall.call_args_list
                      if "FROM candidate_entry_exit_points AS crossing" in call.args[0]]
    assert len(endpoint_calls) == 1
    assert endpoint_calls[0].args[1] == (DETECT_ID, [100, 101, 200, 201])


@pytest.mark.parametrize("damage,message", [
    ("missing_centerline", "not part"), ("missing_manifest", "incomplete"),
    ("missing_endpoint", "missing crossing"), ("missing_member", "missing members"),
    ("wrong_direction", "directions disagree"), ("wrong_vehicle", "vehicle lineage"),
    ("missing_source", "longitude"),
])
def test_invalid_manifest_or_members_refuse_the_entire_replay(damage, message):
    store = make_store()
    if damage == "missing_centerline":
        store.centerlines = []
    elif damage == "missing_manifest":
        store.manifest = []
    elif damage == "missing_endpoint":
        store.endpoints.pop()
    elif damage == "missing_member":
        store.points.pop(1)
    elif damage == "wrong_direction":
        store.manifest = [(0, 100, 101, True)]
    else:
        point = list(store.points[1])
        point[16 if damage == "wrong_vehicle" else 9] = "other" if damage == "wrong_vehicle" else None
        store.points[1] = tuple(point)
    with pytest.raises((MapDataError, MapNotFoundError), match=message):
        store.replay(replay_request())


@pytest.mark.parametrize("changes", [
    {"detect_id": REFINE_ID}, {"refine_id": DETECT_ID},
])
def test_cross_run_selection_fails_before_member_queries(changes):
    store = make_store()
    with pytest.raises(MapDataError, match="does not belong"):
        store.replay(replay_request(**changes))
    store._fetchall.assert_not_called()


def test_unique_point_clip_and_membership_budgets_refuse_partial_results(backend_config):
    from backend.database import configure_database

    with pytest.raises(MapDataError, match="max_points"):
        make_store().replay(replay_request(max_points=2))
    configure_database(replace(backend_config, map=replace(backend_config.map, clip_feature_limit=1)))
    store = make_store()
    store.centerlines.append((82, DETECT_ID, "combined", 11, 12, 1, 0))
    with pytest.raises(MapDataError, match="clip limit"):
        store.replay(replay_request(centerline_ids=[81, 82]))
    configure_database(replace(backend_config, map=replace(
        backend_config.map, replay_max_points_default=3, replay_max_points_max=3)))
    with pytest.raises(MapDataError, match="membership reference limit"):
        store.replay(replay_request(centerline_ids=[81, 82]))


@pytest.mark.skipif(not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")
def test_centerline_member_sql_executes_against_private_postgres_tables():
    import psycopg

    # Temp tables shadow business tables; rollback leaves existing Runs untouched.
    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as connection:
        connection.execute("SET LOCAL search_path TO pg_temp, pg_catalog")
        connection.execute("""
            CREATE TEMP TABLE centerlines (
                id bigint, fit_id uuid, detect_id uuid, variant text,
                from_intersection_id bigint, to_intersection_id bigint,
                forward_clip_count integer, reverse_clip_count integer);
            CREATE TEMP TABLE centerline_source_clips (
                centerline_id bigint, detect_id uuid, input_ordinal integer,
                start_crossing_id bigint, end_crossing_id bigint,
                reverse_to_centerline_direction boolean);
            CREATE TEMP TABLE candidate_entry_exit_points (
                id bigint, detect_id uuid, crossing_type integer,
                candidate_intersection_id bigint, crossing_lon float8, crossing_lat float8);
            CREATE TEMP TABLE intersections (
                id bigint, detect_id uuid, candidate_intersection_id bigint);
            CREATE TEMP TABLE resolved_subtrack_points (
                id bigint, detect_id uuid, subtrack_index integer,
                sequence_in_subtrack integer, kept_point_id bigint,
                injected_point_id bigint, crossing_point_id bigint);
            CREATE TEMP TABLE kept_trajectory_points (
                id bigint, refine_id uuid, source_row_key bigint, vehicle_id text);
            CREATE TEMP TABLE injected_points (
                id bigint, refine_id uuid, lon float8, lat float8,
                timestamp float8, heading float8, speed float8, vehicle_id text);
            CREATE TEMP TABLE source_points (
                id bigint, lon float8, lat float8, timestamp float8,
                heading float8, speed float8, vehicle_id text);
            INSERT INTO source_points VALUES (0,120.2,30.2,20,90,10,NULL);
        """)
        connection.execute("INSERT INTO centerlines VALUES (81,%s,%s,'directed',11,12,1,0),"
                           "(82,%s,%s,'combined',12,11,0,1)",
                           (FIT_ID, DETECT_ID, FIT_ID, DETECT_ID))
        connection.execute("INSERT INTO centerline_source_clips VALUES "
                           "(81,%s,0,100,101,false),(82,%s,0,100,101,true)",
                           (DETECT_ID, DETECT_ID))
        connection.execute("INSERT INTO candidate_entry_exit_points VALUES "
                           "(100,%s,0,9,120.1,30.1),(101,%s,1,10,120.3,30.3)",
                           (DETECT_ID, DETECT_ID))
        connection.execute("INSERT INTO intersections VALUES (11,%s,9),(12,%s,10)",
                           (DETECT_ID, DETECT_ID))
        connection.execute("INSERT INTO kept_trajectory_points VALUES (10,%s,0,'vehicle#partition-1')",
                           (REFINE_ID,))
        connection.execute("INSERT INTO resolved_subtrack_points VALUES "
                           "(1,%s,7,0,NULL,NULL,100),(2,%s,7,1,10,NULL,NULL),"
                           "(3,%s,7,2,NULL,NULL,101),(4,%s,7,3,10,NULL,NULL)",
                           (DETECT_ID,) * 4)
        store = PostgresMapStore(connection)
        store._resolve_fit = Mock(return_value=(FIT_ID, DETECT_ID))
        store._resolve_detect = make_store()._resolve_detect
        result = store.replay(replay_request(centerline_ids=[81, 82], max_points=3))
        assert result["metadata"]["returned_point_count"] == 3
        assert result["metadata"]["returned_clip_count"] == 2
        assert result["metadata"]["membership_reference_count"] == 6
        assert result["features"][-1]["properties"]["resolved_point_ids"] == [3, 2, 1]
        assert result["features"][1]["properties"]["source_vehicle_id"] is None
        assert all(feature["properties"]["resolved_point_id"] != 4
                   for feature in result["features"][:3])
        connection.execute("DELETE FROM source_points")
        with pytest.raises(MapDataError, match="longitude"):
            store.replay(replay_request())
        connection.rollback()
