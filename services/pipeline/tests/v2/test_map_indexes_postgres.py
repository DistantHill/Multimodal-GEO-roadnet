"""Map indexes can be added idempotently to populated current-schema tables."""

import os

import psycopg
from psycopg import sql
import pytest

from track.v2.cli.init_db import INDEX_STATEMENTS


pytestmark = pytest.mark.skipif(not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")


@pytest.mark.parametrize("table,index,geometry", [
    ("turn_events_geom", "idx_turn_events_geom_geom", "POINT(120 30)"),
    ("centerlines", "idx_centerlines_geom", "LINESTRING(120 30,121 31)"),
])
def test_map_spatial_index_preserves_rows_and_can_be_applied_twice(table, index, geometry):
    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as connection:
        connection.execute("SET LOCAL search_path TO pg_temp, public, pg_catalog")
        connection.execute(sql.SQL("CREATE TEMP TABLE {} (id bigint PRIMARY KEY, geom geometry NOT NULL)").format(sql.Identifier(table)))
        connection.execute(sql.SQL("INSERT INTO {} VALUES (1, ST_GeomFromText(%s,4326))").format(sql.Identifier(table)), (geometry,))
        before = connection.execute(sql.SQL("SELECT id,ST_AsEWKB(geom) FROM {}").format(sql.Identifier(table))).fetchall()
        for _ in range(2):
            connection.execute(dict(INDEX_STATEMENTS)[index])
            definition = connection.execute("""
                SELECT am.amname, i.indisvalid, pg_get_indexdef(i.indexrelid, 1, true)
                  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                  JOIN pg_am am ON am.oid = c.relam
                 WHERE c.relnamespace = pg_my_temp_schema() AND c.relname = %s
            """, (index,)).fetchone()
            assert definition == ("gist", True, "geom")
            assert connection.execute(sql.SQL("SELECT id,ST_AsEWKB(geom) FROM {}").format(sql.Identifier(table))).fetchall() == before
        connection.rollback()
