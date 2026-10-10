"""Non-destructive v11 upgrade and persisted NULL/finite heading behavior."""
import os

import pytest

from track.v2.db.migrations.refine_heading_v12 import migrate_refine_heading_v12


def test_migration_requires_v11_before_altering_tables():
    class Cursor:
        def __init__(self):
            self.calls = []
        def execute(self, query, params=None):
            self.calls.append(query)
        def fetchone(self):
            return (10,)
    cursor = Cursor()
    with pytest.raises(RuntimeError, match="v11"):
        migrate_refine_heading_v12(cursor)
    assert len(cursor.calls) == 1


@pytest.mark.skipif(not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")
def test_migration_preserves_old_rows_and_rejects_invalid_new_headings():
    import psycopg
    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as connection:
        connection.execute("SET LOCAL search_path TO pg_temp, public, pg_catalog")
        connection.execute("""
            CREATE TEMP TABLE pipeline_schema_versions(component text PRIMARY KEY, version integer);
            INSERT INTO pipeline_schema_versions VALUES ('track.v2', 11);
            CREATE TEMP TABLE kept_trajectory_points(id bigint PRIMARY KEY);
            CREATE TEMP TABLE injected_points(id bigint PRIMARY KEY);
            INSERT INTO kept_trajectory_points VALUES (1);
            INSERT INTO injected_points VALUES (2)
        """)
        with connection.cursor() as cursor:
            migrate_refine_heading_v12(cursor)
        assert connection.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (12,)
        assert connection.execute("SELECT id, heading_cal FROM kept_trajectory_points").fetchone() == (1, None)
        assert connection.execute("SELECT id, heading_cal FROM injected_points").fetchone() == (2, None)
        connection.execute("INSERT INTO kept_trajectory_points VALUES (3, 90), (4, NULL)")
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute("INSERT INTO kept_trajectory_points VALUES (5, 360)")
        connection.rollback()
