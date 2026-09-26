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
