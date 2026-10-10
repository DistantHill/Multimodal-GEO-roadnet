"""Native Refine XY is written once; upgrading does not backfill old geometry."""
import os
import numpy as np
import pytest
from track.v2.db.migrations.kept_xy_v9 import migrate_kept_xy_v9
from track.v2.db.write_refine_results import insert_kept_trajectory_points
from track.v2.models import SourceBatch, RefineConfig, SpatialContext
from track.v2.trajectories.refine_trajectories import refine_vehicle

pytestmark = pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')


def test_v8_upgrade_preserves_old_derived_keys_and_does_not_backfill():
    import psycopg
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as connection:
        connection.execute('SET LOCAL search_path TO pg_temp, public, pg_catalog')
        connection.execute('''CREATE TEMP TABLE pipeline_schema_versions(component text,version integer);
            INSERT INTO pipeline_schema_versions VALUES ('track.v2',8);
            CREATE TEMP TABLE kept_trajectory_points(source_row_key bigint);
            INSERT INTO kept_trajectory_points VALUES (0),(-1);
            CREATE TEMP TABLE protected_source(id bigint); INSERT INTO protected_source VALUES (0),(-1)''')
        with connection.cursor() as cursor:
            migrate_kept_xy_v9(cursor)
        assert connection.execute('SELECT version FROM pipeline_schema_versions').fetchone() == (9,)
        assert connection.execute('SELECT source_row_key,x,y FROM kept_trajectory_points ORDER BY source_row_key').fetchall() == [(-1,None,None),(0,None,None)]
        assert connection.execute('SELECT id FROM protected_source ORDER BY id').fetchall() == [(-1,),(0,)]
        connection.rollback()


def test_writer_persists_exact_authoritative_xy_with_zero_negative_keys():
    import psycopg
    source = SourceBatch('fixture','original',np.array([[120.,30.],[120.001,30.]]),
                         np.array([0.,1.]),np.array([0.,0.]),np.array([1.,1.]),
                         np.array([0,-1],dtype=np.int64),np.ones(2,dtype=bool),np.full(2,''))
    track = refine_vehicle(source, spatial_context=SpatialContext(4326,32651,{'coordinate_semantics_version':2,'enforce_area_of_use':False,'max_scale_error':.1}),
                           config=RefineConfig(100,45,135,50,1,20,10, 1)).subtracks[0]
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as connection:
        connection.execute('SET LOCAL search_path TO pg_temp, public, pg_catalog')
        connection.execute('''CREATE TEMP TABLE kept_trajectory_points(refine_id text, source_row_key bigint, vehicle_id text,
            subtrack_index integer, sequence_in_subtrack integer, x double precision,y double precision, geom geometry(Point,4326), heading_cal double precision)''')
        assert insert_kept_trajectory_points(connection,[track],refine_id='run') == 2
        rows = connection.execute('SELECT source_row_key,x,y FROM kept_trajectory_points ORDER BY sequence_in_subtrack').fetchall()
        assert [row[0] for row in rows] == [0,-1]
        np.testing.assert_array_equal(np.array([row[1:] for row in rows]),track.xy)
        assert connection.execute('SELECT ST_X(geom),ST_Y(geom),ST_SRID(geom) FROM kept_trajectory_points ORDER BY sequence_in_subtrack').fetchall() == [(120.,30.,4326),(120.001,30.,4326)]
        connection.rollback()
