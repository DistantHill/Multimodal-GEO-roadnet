"""Completion checks under stale statistics, isolated and rolled back in full."""
from __future__ import annotations

import os
import re
from uuid import uuid4

import pytest

from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.trajectories.read_source import _configure_postgres_session

pytestmark = pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')


@pytest.fixture
def connection():
    import psycopg
    from psycopg import sql

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        try:
            schema = 'test_completion_' + uuid4().hex
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
            for _, ddl in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
                ddl = re.sub(r'\b(geometry|ST_[a-z_]+)\s*\(', r'public.\1(', ddl, flags=re.I)
                conn.execute(ddl)
            yield conn
        finally:
            conn.rollback()


def seed_detect(conn, *, tracks=1, offset=0, long_subtrack=False, same_vehicle=False, source_table="public.fixture"):
    refine, detect = uuid4(), uuid4()
    conn.execute("INSERT INTO refine_runs(id, source_table, status) VALUES (%s, %s, 'running')", (refine, source_table))
    conn.execute("""
        INSERT INTO kept_trajectory_points(id, refine_id, source_row_key, vehicle_id, subtrack_index, sequence_in_subtrack)
        SELECT %s + n, %s, n::bigint, ((n-1)/2)::text, (n-1)/2+1, (n-1)%%2
        FROM generate_series(1, %s) n
    """, (offset, refine, tracks * 2))
    if long_subtrack:
        conn.execute("""UPDATE kept_trajectory_points SET vehicle_id='one', subtrack_index=1,
            sequence_in_subtrack=id-%s-1 WHERE refine_id=%s""", (offset, refine))
    if same_vehicle:
        conn.execute("UPDATE kept_trajectory_points SET vehicle_id='original' WHERE refine_id=%s", (refine,))
    artifact = uuid4()
    conn.execute("""
        INSERT INTO pipeline_artifacts(
            id, stage, refine_id, relative_path, manifest_version, manifest,
            manifest_sha256, source_fingerprint, row_count, byte_count)
        VALUES (%s, 'refine', %s, %s, 1, '{}', %s, '{}', 0, 0)
    """, (artifact, refine, f"refine/{refine}", "a" * 64))
    conn.execute(
        "INSERT INTO pipeline_artifact_events(artifact_id, state) VALUES (%s, 'present')",
        (artifact,),
    )
    conn.execute("UPDATE refine_runs SET status='completed', finished_at=now() WHERE id=%s", (refine,))
    conn.execute("INSERT INTO detect_runs(id, refine_id, status) VALUES (%s, %s, 'running')", (detect, refine))
    conn.execute("""
        INSERT INTO resolved_subtrack_points(id, detect_id, subtrack_index, refined_sequence_in_subtrack, sequence_in_subtrack, kept_point_id)
        SELECT %s+n, %s, (n-1)/2+1, (n-1)%%2, ((n-1)%%2)*3, %s+n FROM generate_series(1, %s) n
    """, (offset, detect, offset, tracks*2))
    conn.execute("""INSERT INTO candidate_intersections(id, detect_id, centroid_x, centroid_y, centroid_lon, centroid_lat, radius_m)
        VALUES (%s, %s, 0, 0, 0, 0, 20)""", (offset+1, detect))
    conn.execute("""
        INSERT INTO candidate_entry_exit_points(id, detect_id, candidate_intersection_id, segment_start_point_id,
            crossing_type, crossing_x, crossing_y, crossing_lon, crossing_lat)
        SELECT %s+n, %s, %s+1, %s+((n-1)/2)*2+1, n%%2, 0, 0, 0, 0 FROM generate_series(1, %s) n
    """, (offset, detect, offset, offset, tracks*2))
    conn.execute("""INSERT INTO intersections(id, detect_id, candidate_intersection_id, observation_count, clip_radius_m, geom)
        VALUES (%s, %s, %s, %s, 20, public.ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))',4326))""",
        (offset+1, detect, offset+1, tracks))
    conn.execute("""
        INSERT INTO resolved_subtrack_points(id, detect_id, subtrack_index, sequence_in_subtrack, crossing_point_id)
        SELECT %s+%s+n, %s, (n-1)/2+1, (n-1)%%2+1, %s+n FROM generate_series(1, %s) n
    """, (offset, tracks*2, detect, offset, tracks*2))
    conn.execute("""INSERT INTO entry_exit_points(detect_id, intersection_id, entry_bearing, exit_bearing, entry_crossing_id, exit_crossing_id)
        SELECT %s, %s+1, 0, 180, %s+2*n-1, %s+2*n FROM generate_series(1, %s) n""",
        (detect, offset, offset, offset, tracks))
    if long_subtrack:
        conn.execute("""UPDATE resolved_subtrack_points SET subtrack_index=1,
            refined_sequence_in_subtrack=kept_point_id-%s-1,
            sequence_in_subtrack=CASE WHEN kept_point_id IS NOT NULL
                THEN ((kept_point_id-%s-1)/2)*4 + ((kept_point_id-%s-1)%%2)*3
                ELSE ((crossing_point_id-%s-1)/2)*4 + (crossing_point_id-%s-1)%%2 + 1 END
            WHERE detect_id=%s""", (offset, offset, offset, offset, offset, detect))
    return detect


def complete(conn, detect):
    artifact = uuid4()
    conn.execute("""
        INSERT INTO pipeline_artifacts(
            id, stage, detect_id, relative_path, manifest_version, manifest,
            manifest_sha256, source_fingerprint, row_count, byte_count)
        VALUES (%s, 'detect', %s, %s, 1, '{}', %s, '{}', 0, 0)
    """, (artifact, detect, f"detect/{detect}", "a" * 64))
    conn.execute(
        "INSERT INTO pipeline_artifact_events(artifact_id, state) VALUES (%s, 'present')",
        (artifact,),
    )
    conn.execute("UPDATE detect_runs SET status='completed', finished_at=now() WHERE id=%s", (detect,))
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


@pytest.mark.parametrize('long_subtrack', [False, True], ids=['many-subtracks', 'one-long-subtrack'])
def test_new_run_completes_with_stale_statistics(connection, long_subtrack):
    conn = connection
    old = seed_detect(conn, tracks=100)
    complete(conn, old)
    for table, _ in DDL_STATEMENTS:
        conn.execute(f'ANALYZE {table}')
    conn.execute('SET CONSTRAINTS ALL DEFERRED')
    detect = seed_detect(conn, tracks=10000, offset=100000, long_subtrack=long_subtrack)
    conn.execute("SET LOCAL statement_timeout='15s'")
    complete(conn, detect)
    assert conn.execute('SHOW enable_nestloop').fetchone() == ('on',)
    assert conn.execute('SELECT status FROM detect_runs WHERE id=%s', (detect,)).fetchone() == ('completed',)


@pytest.mark.parametrize('mutation, message', [
    ("DELETE FROM resolved_subtrack_points WHERE crossing_point_id=2; UPDATE resolved_subtrack_points SET sequence_in_subtrack=2 WHERE kept_point_id=2", 'absent from resolved'),
    ("UPDATE candidate_entry_exit_points SET segment_start_point_id=2", 'between its refined segment endpoints'),
    ("UPDATE entry_exit_points SET entry_crossing_id=2, exit_crossing_id=1", 'types must be 1/0'),
])
def test_invalid_detect_is_rejected(connection, mutation, message):
    import psycopg
    detect = seed_detect(connection)
    connection.execute(mutation)
    with pytest.raises(psycopg.Error, match=message):
        complete(connection, detect)


def test_reader_preserves_statement_timeout_and_restores_idle_timeout(connection):
    conn = connection
    conn.execute("SET LOCAL statement_timeout='2s'")
    conn.execute("SET LOCAL idle_in_transaction_session_timeout='30s'")
    conn.execute('SAVEPOINT reader')
    _configure_postgres_session(conn)
    assert conn.execute('SHOW statement_timeout').fetchone() == ('2s',)
    assert conn.execute('SHOW idle_in_transaction_session_timeout').fetchone() == ('0',)
    conn.execute('ROLLBACK TO SAVEPOINT reader')
    assert conn.execute('SHOW idle_in_transaction_session_timeout').fetchone() == ('30s',)


def seed_fit(conn, *, tracks=1):
    detect = seed_detect(conn, tracks=tracks)
    conn.execute("""INSERT INTO candidate_intersections(id, detect_id, centroid_x, centroid_y, centroid_lon, centroid_lat, radius_m)
        VALUES (2, %s, 1, 0, 1, 0, 20)""", (detect,))
    conn.execute("""INSERT INTO intersections(id, detect_id, candidate_intersection_id, observation_count, clip_radius_m, geom)
        VALUES (2, %s, 2, 0, 20, public.ST_GeomFromText('POLYGON((0 0,0 1,1 1,0 0))',4326))""", (detect,))
    conn.execute('DELETE FROM entry_exit_points')
    conn.execute('UPDATE candidate_entry_exit_points SET candidate_intersection_id=(id-1)%2+1')
    conn.execute('UPDATE candidate_entry_exit_points SET crossing_type=1-crossing_type')
    complete(conn, detect)
    fit = uuid4()
    conn.execute("""INSERT INTO fit_runs(id, detect_id, clip_algorithm, clip_version, fitting_algorithm, fitting_version, status)
        VALUES (%s, %s, 'fixture', '1', 'fixture', '1', 'running')""", (fit, detect))
    conn.execute("""INSERT INTO centerlines(id, fit_id, detect_id, variant, from_intersection_id, to_intersection_id,
        geom, length_m, forward_clip_count, reverse_clip_count)
        VALUES (1, %s, %s, 'combined', 1, 2, public.ST_GeomFromText('LINESTRING(0 0,1 0)',4326), 1, %s, 0)""",
        (fit, detect, tracks))
    # The following bulk append has no statistics for its new fit.
    conn.execute('ANALYZE centerline_source_clips')
    conn.execute("""INSERT INTO centerline_source_clips
        SELECT 1, %s, n-1, n*2-1, n*2, false FROM generate_series(1, %s) n""", (detect, tracks))
    conn.execute('SET CONSTRAINTS ALL DEFERRED')
    return fit


def complete_fit(conn, fit):
    conn.execute("UPDATE fit_runs SET status='completed', finished_at=now() WHERE id=%s", (fit,))
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_fit_completion_with_unanalyzed_manifest(connection):
    fit = seed_fit(connection, tracks=10000)
    connection.execute("SET LOCAL statement_timeout='15s'")
    complete_fit(connection, fit)
    assert connection.execute('SHOW enable_nestloop').fetchone() == ('on',)


@pytest.mark.parametrize('mutation, message', [
    ('UPDATE centerlines SET forward_clip_count=2', 'direction counts'),
    ('UPDATE centerline_source_clips SET reverse_to_centerline_direction=true; UPDATE centerlines SET forward_clip_count=0, reverse_clip_count=1', 'reversed clip endpoints'),
    ('ALTER TABLE resolved_subtrack_points DISABLE TRIGGER USER; DELETE FROM resolved_subtrack_points WHERE crossing_point_id IS NOT NULL', 'endpoints must exist'),
])
def test_invalid_fit_is_rejected_and_planner_setting_restored(connection, mutation, message):
    import psycopg
    fit = seed_fit(connection)
    with pytest.raises(psycopg.Error, match=message):
        with connection.transaction():
            connection.execute(mutation)
            complete_fit(connection, fit)
    assert connection.execute('SHOW enable_nestloop').fetchone() == ('on',)
    assert connection.execute('SELECT status FROM fit_runs WHERE id=%s', (fit,)).fetchone() == ('running',)


def test_notice_updates_heartbeat_during_blocking_sql_and_detaches(connection):
    from threading import Event
    from unittest.mock import patch
    from track.v2.telemetry.heartbeat import operation_heartbeat, validation_notices

    tick = Event()
    messages = []

    def emit(message):
        messages.append(message)
        if '校验端点' in message:
            tick.set()

    with patch('track.v2.telemetry.heartbeat.log_info'):
        with operation_heartbeat('提交', emit=emit, interval=.01), validation_notices(connection):
            connection.execute("DO $$ BEGIN RAISE NOTICE 'track_v2 validation: 校验端点'; PERFORM pg_sleep(0.15); END $$")
            assert tick.is_set(), 'heartbeat did not receive the current SQL check during execution'
    count = len(messages)
    connection.execute("DO $$ BEGIN RAISE NOTICE 'track_v2 validation: 已退出'; END $$")
    assert len(messages) == count


@pytest.mark.parametrize("same_subtrack", [False, True], ids=["same-vehicle-distinct-partitions", "same-partition"])
def test_cross_completion_uses_run_partition_identity(connection, same_subtrack):
    conn = connection
    detect = seed_detect(conn, tracks=2, same_vehicle=True, long_subtrack=same_subtrack)
    conn.execute("""INSERT INTO tracks_cross(detect_id,candidate_intersection_id,
        from_segment_start_point_id,to_segment_start_point_id,x,y,lon,lat)
        VALUES (%s,1,1,3,0,0,0,0)""", (detect,))
    if same_subtrack:
        with pytest.raises(Exception, match="different subtracks"):
            complete(conn, detect)
    else:
        complete(conn, detect)
        assert conn.execute("SELECT status FROM detect_runs WHERE id=%s", (detect,)).fetchone()[0] == "completed"
