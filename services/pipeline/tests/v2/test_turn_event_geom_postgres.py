"""Run-scoped geometry snapshots, including existing CTAS table adoption."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.db.write_detect_results import materialize_turn_event_geom

pytestmark = pytest.mark.skipif(
    not os.getenv("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


@pytest.fixture
def connection():
    import psycopg
    from psycopg import sql

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            schema = "test_turn_geom_" + uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
            for _, ddl in DDL_STATEMENTS:
                conn.execute(ddl)
            conn.execute(dict(POST_DDL_STATEMENTS)["turn_evnets_gemo_constraints"])
            for name, ddl in INDEX_STATEMENTS:
                if name.startswith("idx_turn_evnets_gemo"):
                    conn.execute(ddl)
            conn.execute("CREATE TABLE custom_source (id bigint PRIMARY KEY, geom geometry(Point,4326))")
            yield conn
        finally:
            conn.rollback()


def seed(conn):
    refine, detect = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO refine_runs(id, source_table, status) VALUES (%s, 'custom_source', 'running')",
        (refine,),
    )
    conn.execute("INSERT INTO detect_runs(id, refine_id, status) VALUES (%s,%s,'running')", (detect, refine))
    source = conn.execute("SELECT COALESCE(max(id),0)+1 FROM custom_source").fetchone()[0]
    conn.execute("INSERT INTO custom_source VALUES (%s, ST_SetSRID(ST_MakePoint(120,30),4326))", (source,))
    kept = conn.execute("""
        INSERT INTO kept_trajectory_points(refine_id,source_row_key,vehicle_id,subtrack_index,sequence_in_subtrack)
        VALUES (%s,%s,'v',1,0) RETURNING id
    """, (refine, source)).fetchone()[0]
    injected = conn.execute("""
        INSERT INTO injected_points(refine_id,vehicle_id,subtrack_index,after_sequence_in_subtrack,
            timestamp,lon,lat,x,y,heading)
        VALUES (%s,'v',1,0,0,121,31,0,0,0) RETURNING id
    """, (refine,)).fetchone()[0]
    for sequence, kept_id, injected_id in [(0, kept, None), (1, None, injected)]:
        point = conn.execute("""
            INSERT INTO resolved_subtrack_points(detect_id,subtrack_index,
                refined_sequence_in_subtrack,sequence_in_subtrack,kept_point_id,injected_point_id)
            VALUES (%s,1,%s,%s,%s,%s) RETURNING id
        """, (detect, sequence, sequence, kept_id, injected_id)).fetchone()[0]
        conn.execute("""
            INSERT INTO turn_events(detect_id,resolved_point_id,entry_bearing,exit_bearing,turn_deg,score)
            VALUES (%s,%s,10,90,80,1)
        """, (detect, point))
    return detect, source


def test_materializes_both_point_types_and_only_requested_run(connection):
    conn = connection
    detect, _ = seed(conn)
    other, _ = seed(conn)
    assert materialize_turn_event_geom(conn, str(detect)) == 2
    rows = conn.execute("""
        SELECT ST_AsText(geom), ST_SRID(geom) FROM turn_evnets_gemo ORDER BY id
    """).fetchall()
    assert rows == [("POINT(120 30)", 4326), ("POINT(121 31)", 4326)]
    assert conn.execute("""
        SELECT count(*) FROM turn_evnets_gemo g JOIN turn_events e USING(id,detect_id)
        WHERE (to_jsonb(g)-'geom') = to_jsonb(e)
    """).fetchone() == (2,)
    assert materialize_turn_event_geom(conn, str(other)) == 2
    conn.execute("DELETE FROM turn_events WHERE detect_id=%s", (detect,))
    assert conn.execute("SELECT count(*) FROM turn_evnets_gemo").fetchone() == (2,)


def test_missing_geometry_rolls_back_entire_insert(connection):
    import psycopg

    conn = connection
    detect, source = seed(conn)
    conn.execute("UPDATE custom_source SET geom=NULL WHERE id=%s", (source,))
    with pytest.raises(psycopg.errors.NotNullViolation), conn.transaction():
        materialize_turn_event_geom(conn, str(detect))
    assert conn.execute("SELECT count(*) FROM turn_evnets_gemo").fetchone() == (0,)


def test_init_adopts_existing_ctas_without_losing_rows(connection):
    conn = connection
    detect, _ = seed(conn)
    materialize_turn_event_geom(conn, str(detect))
    conn.execute("CREATE TEMP TABLE saved AS TABLE turn_evnets_gemo")
    conn.execute("DROP TABLE turn_evnets_gemo")
    conn.execute("CREATE TABLE turn_evnets_gemo AS TABLE saved")
    for _ in range(2):
        conn.execute(dict(DDL_STATEMENTS)["turn_evnets_gemo"])
        conn.execute(dict(POST_DDL_STATEMENTS)["turn_evnets_gemo_constraints"])
        for name, ddl in INDEX_STATEMENTS:
            if name.startswith("idx_turn_evnets_gemo"):
                conn.execute(ddl)
    assert conn.execute("SELECT count(*) FROM turn_evnets_gemo").fetchone() == (2,)
    conn.execute("DELETE FROM turn_events WHERE detect_id=%s", (detect,))
    assert conn.execute("SELECT count(*) FROM turn_evnets_gemo").fetchone() == (0,)


def test_empty_events_and_completed_snapshot_guard(connection):
    import psycopg

    conn = connection
    detect, _ = seed(conn)
    with conn.transaction():
        # The same immutability guard used by Detect's other result tables.
        post = dict(POST_DDL_STATEMENTS)
        conn.execute(post["guard_detect_artifact_mutation_function"])
        conn.execute(post["detect_artifact_mutation_triggers"])
        assert materialize_turn_event_geom(conn, str(detect)) == 2
        conn.execute("UPDATE detect_runs SET status='completed' WHERE id=%s", (detect,))
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"), conn.transaction():
            conn.execute("UPDATE turn_evnets_gemo SET score=0 WHERE detect_id=%s", (detect,))
    other, _ = seed(conn)
    conn.execute("DELETE FROM turn_events WHERE detect_id=%s", (other,))
    assert materialize_turn_event_geom(conn, str(other)) == 0
