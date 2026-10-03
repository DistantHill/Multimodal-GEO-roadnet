"""Reset results in an isolated schema while preserving source rows and sequence."""
from __future__ import annotations

from contextlib import nullcontext
import os
from uuid import uuid4

import pytest

from track.v2.cli import init_db

pytestmark = pytest.mark.skipif(
    not os.getenv("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


def test_reset_preserves_trajectory_data_and_recreates_results(monkeypatch):
    import psycopg
    from psycopg import sql

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            schema = "test_init_reset_" + uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))

            class Connection:
                # Keep init_db commits inside the test's rollback transaction.
                def cursor(self):
                    return conn.cursor()

                def commit(self):
                    pass

            monkeypatch.setattr(init_db, "pg_connect", lambda _: nullcontext(Connection()))
            init_db.create_tables(None)
            source_id = conn.execute("""
                INSERT INTO trajectory_points(vehicle_id,timestamp,lon,lat)
                VALUES ('source',1,120,30) RETURNING id
            """).fetchone()[0]
            source_before = conn.execute("SELECT * FROM trajectory_points").fetchall()
            relation_before = conn.execute("SELECT 'trajectory_points'::regclass::oid").fetchone()
            conn.execute("""
                INSERT INTO refine_runs(id,source_table,status)
                VALUES (%s,'trajectory_points','running')
            """, (uuid4(),))
            conn.execute("CREATE TABLE clean_runs (id integer)")

            for _ in range(2):
                init_db.reset_tables(None)
                assert conn.execute("SELECT * FROM trajectory_points").fetchall() == source_before
                assert conn.execute("SELECT 'trajectory_points'::regclass::oid").fetchone() == relation_before
                assert conn.execute("SELECT to_regclass('clean_runs')").fetchone() == (None,)
                init_db.create_tables(None)
                assert conn.execute("SELECT count(*) FROM refine_runs").fetchone() == (0,)
                for name, _ in init_db.DDL_STATEMENTS:
                    assert conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None
            next_id = conn.execute("""
                INSERT INTO trajectory_points(vehicle_id,timestamp,lon,lat)
                VALUES ('next',2,121,31) RETURNING id
            """).fetchone()[0]
            assert next_id == source_id + 1
        finally:
            conn.rollback()
