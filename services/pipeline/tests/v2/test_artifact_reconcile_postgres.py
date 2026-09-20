"""Opt-in end-to-end reconciliation checks against PostgreSQL v5 guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import pytest

from track.v2.artifact_reconcile import apply_reconcile_plan, build_reconcile_plan
from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)

_DDL = {
    "refine_runs", "detect_runs", "pipeline_artifacts", "pipeline_artifact_events",
}
_POST_DDL = {
    "guard_pipeline_artifact_immutability_function",
    "pipeline_artifact_immutability_triggers",
    "validate_pipeline_artifact_event_function",
    "pipeline_artifact_event_insert_trigger",
}
_INDEXES = {"uq_pipeline_artifacts_refine_run", "uq_pipeline_artifacts_detect_run"}


@pytest.fixture
def artifact_connection():
    import psycopg
    from psycopg import sql

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            schema = "artifact_gc_test_" + uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
            )
            for name, ddl in DDL_STATEMENTS:
                if name in _DDL:
                    conn.execute(ddl)
            for name, ddl in POST_DDL_STATEMENTS:
                if name in _POST_DDL:
                    conn.execute(ddl)
            for name, ddl in INDEX_STATEMENTS:
                if name in _INDEXES:
                    conn.execute(ddl)
            yield conn
        finally:
            conn.rollback()


def _dataset(root: Path, run_id, then: datetime) -> Path:
    path = root / "refine" / str(run_id)
    path.mkdir(parents=True)
    part = path / "part.parquet"
    part.write_bytes(b"PAR1")
    stamp = then.timestamp()
    os.utime(part, (stamp, stamp))
    os.utime(path, (stamp, stamp))
    part.chmod(0o444)
    path.chmod(0o555)
    return path


def _registered(conn, root: Path, status: str, then: datetime):
    run_id, artifact_id = uuid4(), uuid4()
    relative = f"refine/{run_id}"
    conn.execute(
        "INSERT INTO refine_runs(id, source_table, status, started_at, finished_at) "
        "VALUES (%s, 'source.points', %s, %s, %s)",
        (run_id, status, then, then),
    )
    conn.execute(
        "INSERT INTO pipeline_artifacts("
        "id, stage, refine_id, relative_path, manifest_version, manifest, "
        "manifest_sha256, source_fingerprint, row_count, byte_count, created_at) "
        "VALUES (%s, 'refine', %s, %s, 1, '{}', %s, '{}', 0, 0, %s)",
        (artifact_id, run_id, relative, "a" * 64, then),
    )
    conn.execute(
        "INSERT INTO pipeline_artifact_events(artifact_id, state, event_at) "
        "VALUES (%s, 'present', %s)",
        (artifact_id, then),
    )
    return artifact_id, relative, _dataset(root, run_id, then)


def test_real_postgres_dry_plan_apply_and_completed_protection(
    artifact_connection, tmp_path,
):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=8)
    failed_id, failed_relative, failed_path = _registered(
        artifact_connection, tmp_path, "failed", old,
    )
    _completed_id, completed_relative, completed_path = _registered(
        artifact_connection, tmp_path, "completed", old,
    )
    with artifact_connection.cursor() as cur:
        before = cur.execute("SELECT count(*) FROM pipeline_artifact_events").fetchone()[0]
        plan = build_reconcile_plan(cur, tmp_path.resolve(), now, 7)
        after_plan = cur.execute("SELECT count(*) FROM pipeline_artifact_events").fetchone()[0]
        by_path = {item.relative_path: item for item in plan.actions}
        assert before == after_plan
        assert by_path[failed_relative].eligible and not by_path[failed_relative].protected
        assert not by_path[completed_relative].eligible and by_path[completed_relative].protected

        result = apply_reconcile_plan(
            cur, tmp_path.resolve(), plan, reason="artifact reconcile PostgreSQL test",
        )
        assert result.appended_events == 1
        assert not failed_path.exists() and completed_path.exists()
        assert cur.execute(
            "SELECT state FROM pipeline_artifact_events WHERE artifact_id = %s ORDER BY id DESC LIMIT 1",
            (failed_id,),
        ).fetchone() == ("deleted",)

        second = build_reconcile_plan(cur, tmp_path.resolve(), now, 7)
        again = apply_reconcile_plan(
            cur, tmp_path.resolve(), second, reason="artifact reconcile PostgreSQL test",
        )
        assert again.appended_events == 0 and again.removed_final == ()
