from __future__ import annotations

import os

import pytest

from track.v2.db.migrate_source_row_key import migrate_source_row_key_v8


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"),
    reason="ROADNET_TEST_DSN is not set",
)


def _install_v7_fixture(conn) -> None:
    conn.execute("SET LOCAL search_path TO pg_temp, pg_catalog")
    conn.execute("""
        CREATE TEMP TABLE pipeline_schema_versions (
            component text PRIMARY KEY,
            version integer NOT NULL
        );
        INSERT INTO pipeline_schema_versions VALUES ('track.v2', 7);
        CREATE TEMP TABLE refine_runs (source_table text NOT NULL);
        CREATE TEMP TABLE excluded_points (source_row_key text NOT NULL);
        CREATE TEMP TABLE kept_trajectory_points (source_row_key text NOT NULL);
    """)


def test_v7_source_keys_convert_to_bigint_and_publish_v8_last() -> None:
    import psycopg

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            _install_v7_fixture(conn)
            conn.execute("CREATE TEMP TABLE source_fixture (id bigint PRIMARY KEY)")
            conn.execute("INSERT INTO refine_runs VALUES ('source_fixture')")
            conn.execute(
                "INSERT INTO excluded_points VALUES (%s), (%s)",
                ("-9223372036854775808", "7"),
            )
            conn.execute(
                "INSERT INTO kept_trajectory_points VALUES (%s)",
                ("9223372036854775807",),
            )
            with conn.cursor() as cur:
                migrate_source_row_key_v8(cur)

            assert conn.execute(
                "SELECT version FROM pipeline_schema_versions WHERE component='track.v2'"
            ).fetchone() == (8,)
            assert conn.execute(
                "SELECT source_row_key FROM excluded_points ORDER BY source_row_key"
            ).fetchall() == [(-9223372036854775808,), (7,)]
            assert conn.execute(
                "SELECT source_row_key FROM kept_trajectory_points"
            ).fetchone() == (9223372036854775807,)
            assert conn.execute(
                "SELECT pg_typeof(source_row_key)::text FROM kept_trajectory_points"
            ).fetchone() == ("bigint",)
        finally:
            conn.rollback()


def test_v7_migration_rejects_retained_run_with_non_integer_source_id() -> None:
    import psycopg

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            _install_v7_fixture(conn)
            conn.execute("CREATE TEMP TABLE source_fixture (id uuid PRIMARY KEY)")
            conn.execute("INSERT INTO refine_runs VALUES ('source_fixture')")
            conn.execute("INSERT INTO excluded_points VALUES ('1')")
            with pytest.raises(RuntimeError, match="integer-family source ids"):
                with conn.transaction():
                    with conn.cursor() as cur:
                        migrate_source_row_key_v8(cur)
            assert conn.execute(
                "SELECT version FROM pipeline_schema_versions WHERE component='track.v2'"
            ).fetchone() == (7,)
            assert conn.execute(
                "SELECT pg_typeof(source_row_key)::text FROM excluded_points"
            ).fetchone() == ("text",)
        finally:
            conn.rollback()


@pytest.mark.parametrize(
    "bad_key",
    (
        "9223372036854775808",
        "-9223372036854775809",
        "not-an-integer",
        "+7",
        "007",
        "-0",
    ),
)
def test_invalid_source_key_refuses_migration_without_altering_schema(bad_key: str) -> None:
    import psycopg

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            _install_v7_fixture(conn)
            conn.execute("INSERT INTO excluded_points VALUES (%s)", (bad_key,))
            conn.execute("INSERT INTO kept_trajectory_points VALUES ('1')")
            with pytest.raises(RuntimeError, match="not canonical signed 64-bit integers"):
                with conn.transaction():
                    with conn.cursor() as cur:
                        migrate_source_row_key_v8(cur)

            assert conn.execute(
                "SELECT version FROM pipeline_schema_versions WHERE component='track.v2'"
            ).fetchone() == (7,)
            assert conn.execute(
                "SELECT pg_typeof(source_row_key)::text FROM excluded_points"
            ).fetchone() == ("text",)
            assert conn.execute(
                "SELECT pg_typeof(source_row_key)::text FROM kept_trajectory_points"
            ).fetchone() == ("text",)
        finally:
            conn.rollback()
