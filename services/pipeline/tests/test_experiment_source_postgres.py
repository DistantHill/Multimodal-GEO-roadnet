"""Read strict epoch time/region source slices and keep exit/reentry boundaries."""
import os
import uuid
from pathlib import Path

import pytest
import psycopg
from psycopg import sql

from track.v2.config.field_mapping import load_field_config
from track.v2.db.source_table import validate_source_table
from track.v2.experiments.config import load_case
from track.v2.models import PostgresSource
from track.v2.trajectories.read_source import get_database
from v2.config_factory import runtime_config


@pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'),reason='ROADNET_TEST_DSN not set')
def test_real_epoch_source_and_reader_preserve_segment_marker(tmp_path):
    dsn=os.environ['ROADNET_TEST_DSN']
    name='experiment_slice_'+uuid.uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('''CREATE TABLE {}.points(id bigint PRIMARY KEY,vehicle_id text,
                timestamp double precision,lon double precision,lat double precision,heading double precision,
                speed double precision,geom geometry(Point,4326))''').format(sql.Identifier(name)))
            rows=[(1,1677600000.,111.,30.),(2,1677600001.,111.1,30.1),
                  (3,1677600002.,113.,30.),(4,1677600003.,111.2,30.2),
                  (5,1677686400.,111.2,30.2)]
            for key,timestamp,lon,lat in rows:
                conn.execute(sql.SQL('INSERT INTO {}.points VALUES(%s,\'car\',%s,%s,%s,0,10,ST_SetSRID(ST_MakePoint(%s,%s),4326))').format(sql.Identifier(name)),
                    (key,timestamp,lon,lat,lon,lat))
            conn.commit()
            case=load_case('sample',{'source_table':name+'.points','time_start':'2023-03-01T00:00:00+08:00',
                'time_end':'2023-03-02T00:00:00+08:00','bbox':[[111,30],[112,31]]},tmp_path)
            contract=validate_source_table(conn,case.source_table)
            query,params=case.source_query(contract)
            fields=load_field_config(Path(__file__).resolve().parents[1]/'config/field_specs.toml')
            source=PostgresSource(None,query,params,connection_factory=lambda config: psycopg.connect(dsn),
                source_id=case.source_table,primary_key_field='id',break_column='_experiment_segment')
            with get_database(source,field_config=fields,runtime=runtime_config()) as handle:
                batches=list(handle.iter_vehicle_batches())
            assert len(batches)==1
            assert batches[0].source_row_key.tolist()==[1,2,4]
            assert batches[0].extra['_experiment_segment'].tolist()==[0,0,1]
    finally:
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))


@pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN not set')
def test_spatial_candidate_query_matches_full_day_window(tmp_path):
    import random

    rng = random.Random(42)
    case = load_case('sample', {
        'source_table': 'pg_temp.experiment_points',
        'time_start': '2023-03-01T00:00:00+08:00',
        'time_end': '2023-03-02T00:00:00+08:00',
        'bbox': [[111, 30], [112, 31]],
    }, tmp_path)
    start, end = case.time_start.timestamp(), case.time_end.timestamp()
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        conn.execute('''CREATE TEMP TABLE experiment_points (
            id bigint PRIMARY KEY, vehicle_id text NOT NULL,
            timestamp double precision NOT NULL, lon double precision,
            lat double precision, heading double precision, speed double precision,
            geom geometry(Point,4326))''')
        rows = []
        for key in range(1, 1001):
            # Duplicate timestamps exercise the existing lexical id tie-break.
            timestamp = start + rng.randrange(0, 50)
            lon = rng.choice([110., 111., 111.5, 112., 113., None])
            rows.append((key, f'car-{key % 10}', timestamp, lon, 30.5))
        # Vehicle with no inside points, exact time bounds, and leading exits.
        rows.extend([(1001, 'outside', start, 110., 30.5),
                     (1002, 'boundary', start - 1, 111.5, 30.5),
                     (1003, 'boundary', start, 110., 30.5),
                     (1004, 'boundary', start + 1, 111., 30.5),
                     (1005, 'boundary', end, 111.5, 30.5)])
        with conn.cursor() as cur:
            cur.executemany('''INSERT INTO experiment_points
                VALUES (%s,%s,%s,%s,%s,0,10,ST_SetSRID(ST_MakePoint(%s,%s),4326))''',
                [(*row, row[3], row[4]) for row in rows])
        query, params = case.source_query(validate_source_table(conn, case.source_table))
        actual = conn.execute(query, params).fetchall()
        # Previous full-day window is the semantic oracle: compare every column,
        # including the exact outside-point count, not just segment boundaries.
        expected = conn.execute('''WITH timed AS (
            SELECT *, ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),geom)
                AS _experiment_inside FROM experiment_points
            WHERE timestamp >= %s AND timestamp < %s
        ), segmented AS (
            SELECT *, SUM(CASE WHEN _experiment_inside THEN 0 ELSE 1 END)
                OVER (PARTITION BY vehicle_id ORDER BY timestamp,id::text
                      ROWS UNBOUNDED PRECEDING) AS _experiment_segment FROM timed
        ) SELECT * FROM segmented WHERE _experiment_inside
          ORDER BY vehicle_id,timestamp,id::text''', params[:3]).fetchall()
        assert actual == expected
        assert actual
