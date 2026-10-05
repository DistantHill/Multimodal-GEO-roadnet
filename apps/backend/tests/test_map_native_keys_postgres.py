"""Replay source ID strings bind to native BIGINT storage without text casts."""
import os
import pytest
from backend.stores.map_layers import PostgresMapStore, _DetectContext, _RefineContext

pytestmark = pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')


def test_source_string_keys_select_numeric_zero_and_negative_keys():
    import psycopg
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as connection:
        connection.execute('SET LOCAL search_path TO pg_temp, pg_catalog')
        connection.execute('''CREATE TEMP TABLE kept_trajectory_points (
            id bigint, refine_id text, vehicle_id text, subtrack_index integer, source_row_key bigint);
            CREATE TEMP TABLE resolved_subtrack_points (id bigint, detect_id text, kept_point_id bigint, subtrack_index integer);
            INSERT INTO kept_trajectory_points VALUES (11,'refine','same',37,0),(12,'refine','same',212,-1),(13,'other','same',999,0);
            INSERT INTO resolved_subtrack_points VALUES (21,'detect',11,37),(22,'detect',12,212),(23,'other',13,999)''')
        store = PostgresMapStore(connection)
        refine = _RefineContext('refine', 'private', 'private', 'bigint', {})
        context = _DetectContext('detect', refine, 4326, 32651, {})
        assert store._select_refine_tracks(refine, source_row_keys=['0','-1'], bbox=None, max_vehicles=10) == ([37,212], {0,-1}, False)
        assert store._select_resolved_tracks(context, resolved_point_ids=None, source_row_keys=['0','-1'], bbox=None, max_vehicles=10) == ([37,212], {0,-1}, False)
        connection.rollback()


def test_refine_replay_merges_kept_and_injected_points_with_native_keys():
    import psycopg
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as connection:
        connection.execute('SET LOCAL search_path TO pg_temp, public, pg_catalog')
        connection.execute('''CREATE TEMP TABLE source_points (id bigint, geom geometry(Point,4326));
            CREATE TEMP TABLE kept_trajectory_points (
                id bigint, refine_id text, vehicle_id text, subtrack_index integer,
                source_row_key bigint, sequence_in_subtrack integer);
            CREATE TEMP TABLE injected_points (
                id bigint, refine_id text, vehicle_id text, subtrack_index integer,
                geom geometry(Point,4326), after_sequence_in_subtrack integer);
            INSERT INTO source_points VALUES (0,ST_SetSRID(ST_Point(120,30),4326)),
                (-1,ST_SetSRID(ST_Point(121,31),4326));
            INSERT INTO kept_trajectory_points VALUES (11,'refine','vehicle',37,0,0),
                (12,'refine','vehicle',37,-1,1);
            INSERT INTO injected_points VALUES
                (13,'refine','vehicle',37,ST_SetSRID(ST_Point(120.5,30.5),4326),0)''')
        store = PostgresMapStore(connection)
        refine = _RefineContext('refine', 'source_points', '"source_points"', 'bigint', {})
        rows = store._fetch_refine_tracks(refine, [37], 10)
        assert [row[3] for row in rows] == [0, None, -1]
        assert [(row[4], row[5]) for row in rows] == [(120, 30), (120.5, 30.5), (121, 31)]
        assert [row[6] for row in rows] == ['kept', 'injected', 'kept']
        connection.rollback()
