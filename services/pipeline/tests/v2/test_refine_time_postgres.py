"""Time-slice persistence and completion in an isolated, rolled-back schema."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from .test_completion_postgres import connection, seed_detect, complete
from track.v2.db.write_detect_results import _iter_refine_member_batches
from track.v2.db.source_table import SourceTableContract
from track.v2.time_selection import validate_refine_time

pytestmark = pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')


def at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


@pytest.mark.parametrize('mutation', [None, 'missing', 'outside'])
def test_time_slice_members_and_completion(connection, monkeypatch, mutation):
    conn = connection
    conn.execute('CREATE TABLE time_source (id bigint PRIMARY KEY, timestamp double precision)')
    conn.execute('INSERT INTO time_source SELECT n,n FROM generate_series(1,6) n')
    detect = seed_detect(conn, tracks=3, long_subtrack=True, source_table="time_source")
    refine = conn.execute("SELECT refine_id FROM detect_runs WHERE id=%s", (detect,)).fetchone()[0]
    selection = {'time_start': at(2).isoformat(), 'time_end': at(5).isoformat()}
    conn.execute('UPDATE detect_runs SET parameters=%s WHERE id=%s',
                 (Jsonb({'refine_time_selection': selection}), detect))
    monkeypatch.setattr('track.v2.db.source_table.validate_source_table',
                        lambda *_: SourceTableContract('time_source','"time_source"','bigint',False))
    batches = list(_iter_refine_member_batches(conn,str(refine),2,**selection))
    assert [int(v) for b in batches for v in b['point_id']] == [2,3,4]
    assert [int(v) for b in batches for v in b['refined_sequence']] == [0,1,2]
    record = SimpleNamespace(id=str(refine),source_table='time_source',parameters={})
    store = SimpleNamespace(connection=conn)
    assert validate_refine_time(store,record,at(2),at(5)) == (at(2),at(5))
    with pytest.raises(ValueError,match='exceeds'):
        validate_refine_time(store,record,at(0),at(5))
    conn.execute('DELETE FROM entry_exit_points WHERE detect_id=%s', (detect,))
    conn.execute('DELETE FROM intersections WHERE detect_id=%s', (detect,))
    conn.execute('DELETE FROM resolved_subtrack_points WHERE detect_id=%s AND crossing_point_id IS NOT NULL', (detect,))
    conn.execute('DELETE FROM candidate_entry_exit_points WHERE detect_id=%s', (detect,))
    conn.execute('DELETE FROM candidate_intersections WHERE detect_id=%s', (detect,))
    conn.execute('DELETE FROM resolved_subtrack_points WHERE detect_id=%s', (detect,))
    points = [2,3,4]
    if mutation == 'missing': points.remove(3)
    if mutation == 'outside': points.append(5)
    for sequence, point in enumerate(points):
        conn.execute('''INSERT INTO resolved_subtrack_points
            (detect_id,subtrack_index,sequence_in_subtrack,refined_sequence_in_subtrack,kept_point_id)
            VALUES (%s,1,%s,%s,%s)''', (detect,sequence,sequence,point))
    if mutation:
        with pytest.raises(Exception,match='resolved p/i set|resolved refined sequence'):
            complete(conn,detect)
    else:
        complete(conn,detect)


def test_v9_migration_keeps_existing_refine_artifact(connection):
    from track.v2.db.migrations.detect_time_v10 import migrate_detect_time_v10
    conn = connection
    seed_detect(conn)
    before = conn.execute('SELECT id,refine_id FROM pipeline_artifacts').fetchall()
    conn.execute("INSERT INTO pipeline_schema_versions(component,version) VALUES ('track.v2',9)")
    with conn.cursor() as cursor:
        migrate_detect_time_v10(cursor)
    assert conn.execute("SELECT version FROM pipeline_schema_versions WHERE component='track.v2'").fetchone() == (10,)
    assert conn.execute('SELECT id,refine_id FROM pipeline_artifacts').fetchall() == before
