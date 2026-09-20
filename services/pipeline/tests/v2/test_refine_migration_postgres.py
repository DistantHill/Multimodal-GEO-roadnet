"""Opt-in v3 migration checks in a transaction-local, uniquely named schema."""

import os
import re
from uuid import uuid4

import pytest

from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.db.refine_migration import migrate_refine

pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


@pytest.fixture(params=[True, False], ids=["with-legacy-guard", "without-legacy-guard"])
def legacy_connection(request):
    import psycopg
    from psycopg import sql

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            schema = "test_refine_" + uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            # No public fallback: unqualified DROP/ALTER can only resolve our
            # own objects. Only PostGIS types/functions are explicitly public.
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            for _name, ddl in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
                if _name in (
                    "validate_refine_artifact_completion_function",
                    "refine_artifact_completion_trigger",
                ):
                    continue
                if not request.param and _name in (
                    "guard_refine_artifact_mutation_function",
                    "refine_artifact_mutation_triggers",
                ):
                    continue
                # v3 and v4 differ only in these persisted names.
                legacy = ddl.replace("refined", "cleaned").replace("refine", "clean").replace("Refine", "Clean")
                legacy = re.sub(r"\b(geometry|ST_[a-z_]+)\s*\(", r"public.\1(", legacy, flags=re.I)
                conn.execute(legacy)
            conn.execute("INSERT INTO pipeline_schema_versions(component, version) VALUES ('track.v2', 3)")
            yield conn
        finally:
            # Rolls back schema creation as well as all fixture data.
            conn.rollback()


def test_migration_preserves_data_and_completed_artifact_guards(legacy_connection):
    import psycopg

    conn = legacy_connection
    run_id = uuid4()
    conn.execute(
        "INSERT INTO clean_runs(id, source_table, status) "
        "VALUES (%s, 'public.example', 'running')", (run_id,),
    )
    conn.execute(
        "INSERT INTO excluded_points(clean_id, source_row_key, reason) VALUES (%s, '7', 'stationary')",
        (run_id,),
    )
    conn.execute("UPDATE clean_runs SET status = 'completed', finished_at = now() WHERE id = %s", (run_id,))
    with conn.cursor() as cur:
        migrate_refine(cur)
        migrate_refine(cur)  # Repeat migration is a no-op.
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (4,)
    assert conn.execute("SELECT id, status FROM refine_runs").fetchone() == (run_id, "completed")
    assert conn.execute("SELECT refine_id, source_row_key FROM excluded_points").fetchone() == (run_id, "7")
    assert conn.execute("SELECT to_regclass('clean_runs')").fetchone() == (None,)
    assert conn.execute(
        "SELECT to_regprocedure('guard_refine_artifact_mutation()') IS NOT NULL"
    ).fetchone() == (True,)
    with pytest.raises(psycopg.Error, match="immutable"):
        with conn.transaction():
            conn.execute("DELETE FROM excluded_points WHERE refine_id = %s", (run_id,))


def test_failed_migration_rolls_back_names_and_version(legacy_connection):
    conn = legacy_connection
    with pytest.raises(RuntimeError, match="rollback probe"):
        with conn.transaction():
            with conn.cursor() as cur:
                migrate_refine(cur)
            raise RuntimeError("rollback probe")
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (3,)
    assert conn.execute("SELECT to_regclass('refine_runs')").fetchone() == (None,)
    assert conn.execute("SELECT to_regclass('clean_runs') IS NOT NULL").fetchone() == (True,)
