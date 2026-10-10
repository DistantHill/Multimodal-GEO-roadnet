"""Plugin identity constraints and v10 migration preserve historical evidence."""
from __future__ import annotations

import os
import pytest
from .test_completion_postgres import connection, seed_detect
from track.v2.db.migrations.detect_plugins_v11 import migrate_detect_plugins_v11


class Cursor:
    def __init__(self, version, fail=False):
        self.version = version
        self.fail = fail
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append(statement)
        if self.fail and "ALTER TABLE turn_events" in statement:
            raise RuntimeError("migration failure")

    def fetchone(self):
        return (self.version,)


def test_version_is_only_advanced_after_success():
    cur = Cursor(10, fail=True)
    with pytest.raises(RuntimeError, match="migration failure"):
        migrate_detect_plugins_v11(cur)
    assert not any("SET version=11" in item for item in cur.calls)
    with pytest.raises(RuntimeError, match="requires.*v10"):
        migrate_detect_plugins_v11(Cursor(9))


postgres = pytest.mark.skipif(not os.getenv("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")


def add_turn(conn, detect, algorithm=None, instance=None, local=None, evidence=None):
    return conn.execute("""INSERT INTO turn_events
        (detect_id,resolved_point_id,entry_bearing,exit_bearing,turn_deg,score,
         algorithm_id,instance_id,local_candidate_id,evidence_index)
        VALUES (%s,1,0,90,90,0,%s,%s,%s,%s) RETURNING id""",
        (detect,algorithm,instance,local,evidence)).fetchone()[0]


@postgres
def test_same_source_is_retained_per_instance_and_identity_is_unique(connection):
    import psycopg
    conn = connection
    detect = seed_detect(conn)
    first = add_turn(conn, detect, "turn_threshold", "turn_30", 0, 0)
    second = add_turn(conn, detect, "turn_threshold", "turn_60", 0, 1)
    assert first != second
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            add_turn(conn, detect, "turn_threshold", "turn_30", 0, 0)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            add_turn(conn, detect, "turn_threshold", None, 1, 2)
    add_turn(conn, detect)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            add_turn(conn, detect)


@postgres
def test_v10_migration_preserves_old_rows_and_allows_new_instance_rows(connection):
    from psycopg import sql
    conn = connection
    # Build the v10 shape inside the fixture transaction, retaining all source data.
    conn.execute("DROP TABLE detect_candidate_instances")
    for name in ("turn_events", "tracks_cross", "turn_events_geom", "trun_hot_ponits"):
        conn.execute(sql.SQL("ALTER TABLE {} DROP COLUMN algorithm_id CASCADE, "
                            "DROP COLUMN instance_id CASCADE, DROP COLUMN local_candidate_id CASCADE, "
                            "DROP COLUMN evidence_index CASCADE").format(sql.Identifier(name)))
    conn.execute("ALTER TABLE turn_events ADD CONSTRAINT turn_events_resolved_point_key UNIQUE (detect_id,resolved_point_id)")
    conn.execute("ALTER TABLE tracks_cross ADD CONSTRAINT tracks_cross_segment_pair_key UNIQUE (detect_id,from_segment_start_point_id,to_segment_start_point_id)")
    conn.execute("INSERT INTO pipeline_schema_versions(component,version) VALUES ('track.v2',10)")
    detect = seed_detect(conn)
    old = conn.execute("INSERT INTO turn_events(detect_id,resolved_point_id,entry_bearing,exit_bearing,turn_deg,score) VALUES (%s,1,0,90,90,0) RETURNING id", (detect,)).fetchone()[0]
    with conn.cursor() as cur:
        migrate_detect_plugins_v11(cur)
    assert conn.execute("SELECT version FROM pipeline_schema_versions WHERE component='track.v2'").fetchone() == (11,)
    assert conn.execute("SELECT algorithm_id,instance_id,local_candidate_id,evidence_index FROM turn_events WHERE id=%s", (old,)).fetchone() == (None,None,None,None)
    add_turn(conn, detect, "turn_threshold", "turn_30", 0, 0)
    add_turn(conn, detect, "turn_threshold", "turn_60", 0, 1)
    assert conn.execute("SELECT count(*) FROM turn_events WHERE detect_id=%s", (detect,)).fetchone() == (3,)
