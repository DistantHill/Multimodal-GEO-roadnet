"""Opt-in PostgreSQL checks for schema v5 artifact registration."""

from __future__ import annotations

import hashlib
import os
import re
from uuid import UUID, uuid4

import pytest

from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.db.artifact_migration import migrate_artifacts_v5
from track.v2.db.completion_migration import migrate_completion_v6
from track.v2.db.detect_completion_migration import migrate_completion_v7
from track.v2.db.artifact_registry import (
    ArtifactDescriptor,
    append_deleted,
    canonical_json,
    find_detect_artifact,
    find_refine_artifact,
    insert_present,
)
from track.v2.db.pipeline_store import PostgresPipelineStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)

_ARTIFACT_OBJECTS = {
    "pipeline_artifacts",
    "pipeline_artifact_events",
    "uq_pipeline_artifacts_refine_run",
    "uq_pipeline_artifacts_detect_run",
    "guard_pipeline_artifact_immutability_function",
    "pipeline_artifact_immutability_triggers",
    "validate_pipeline_artifact_event_function",
    "pipeline_artifact_event_insert_trigger",
    "validate_refine_artifact_completion_function",
    "refine_artifact_completion_trigger",
    "validate_detect_artifact_completion_function",
    "detect_artifact_completion_trigger",
}


def _postgres_ddl(ddl: str) -> str:
    """Resolve only PostGIS-owned names outside the isolated search path."""
    return re.sub(
        r"\b(geometry|ST_[a-z_]+)\s*\(", r"public.\1(", ddl, flags=re.I,
    )


def _install_v4(conn) -> None:
    for name, ddl in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
        if name not in _ARTIFACT_OBJECTS:
            conn.execute(_postgres_ddl(ddl))
    conn.execute(
        "INSERT INTO pipeline_schema_versions(component, version) VALUES ('track.v2', 4)"
    )


@pytest.fixture
def v4_connection():
    import psycopg
    from psycopg import sql

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            schema = "artifact_test_" + uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
            )
            _install_v4(conn)
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def migrated_connection(v4_connection):
    with v4_connection.cursor() as cur:
        migrate_artifacts_v5(cur)
    return v4_connection


def _manifest() -> dict[str, object]:
    fields = [{"name": "lon", "type": "float64", "nullable": False}]
    fingerprint = hashlib.sha256(canonical_json(fields).encode()).hexdigest()
    return {
        "version": 1,
        "tables": [{
            "name": "kept_points",
            "schema": fields,
            "schema_fingerprint": fingerprint,
            "row_count": 2,
            "files": [{
                "relative_path": "kept/part-0.parquet",
                "byte_count": 20,
                "sha256": "a" * 64,
            }],
        }],
    }


def _add_runs(conn) -> tuple[UUID, UUID]:
    refine_id, detect_id = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO refine_runs(id, source_table, status) VALUES (%s, 'source.points', 'running')",
        (refine_id,),
    )
    conn.execute(
        "INSERT INTO detect_runs(id, refine_id, status) VALUES (%s, %s, 'running')",
        (detect_id, refine_id),
    )
    return refine_id, detect_id


def _descriptor(stage: str, owner_id: UUID, *, artifact_id: UUID | None = None):
    owner = {"refine_id": owner_id} if stage == "refine" else {"detect_id": owner_id}
    return ArtifactDescriptor.create(
        id=artifact_id or uuid4(),
        stage=stage,
        relative_path=f"{stage}/{owner_id}",
        source_fingerprint={"source": "fixture", "rows": 2},
        manifest=_manifest(),
        **owner,
    )


def test_real_v4_migration_installs_all_objects_is_idempotent_and_rolls_back(
    v4_connection,
) -> None:
    import psycopg

    conn = v4_connection
    with pytest.raises(RuntimeError, match="rollback probe"):
        with conn.transaction():
            with conn.cursor() as cur:
                migrate_artifacts_v5(cur)
            raise RuntimeError("rollback probe")
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (4,)
    assert conn.execute("SELECT to_regclass('pipeline_artifacts')").fetchone() == (None,)

    with conn.cursor() as cur:
        migrate_artifacts_v5(cur)
        migrate_artifacts_v5(cur)
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (5,)
    for relation in (
        "pipeline_artifacts",
        "pipeline_artifact_events",
        "uq_pipeline_artifacts_refine_run",
        "uq_pipeline_artifacts_detect_run",
    ):
        assert conn.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone() == (
            True,
        )
    for function in (
        "guard_pipeline_artifact_immutability()",
        "validate_pipeline_artifact_event()",
    ):
        assert conn.execute("SELECT to_regprocedure(%s) IS NOT NULL", (function,)).fetchone() == (
            True,
        )
    assert conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conrelid = 'pipeline_artifacts'::regclass "
        "AND conname = 'pipeline_artifacts_stage_check')"
    ).fetchone() == (True,)
    triggers = conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
        "('pipeline_artifacts'::regclass, 'pipeline_artifact_events'::regclass) "
        "AND NOT tgisinternal"
    ).fetchall()
    assert {row[0] for row in triggers} == {
        "guard_pipeline_artifacts_mutation",
        "guard_pipeline_artifact_events_mutation",
        "validate_pipeline_artifact_event_insert",
    }
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute(
                "INSERT INTO pipeline_artifacts "
                "(id, stage, relative_path, manifest_version, manifest, manifest_sha256, "
                "source_fingerprint, row_count, byte_count) VALUES "
                "(%s, 'refine', 'x', 1, '{}', %s, '{}', 0, 0)",
                (uuid4(), "a" * 64),
            )


def test_registry_api_enforces_run_uniqueness_isolation_and_atomic_rollback(
    migrated_connection,
) -> None:
    import psycopg

    conn = migrated_connection
    refine_id, detect_id = _add_runs(conn)
    refine = _descriptor("refine", refine_id)
    detect = _descriptor("detect", detect_id)
    with conn.cursor() as cur:
        insert_present(cur, refine)
        insert_present(cur, detect)
        assert find_refine_artifact(cur, refine_id).state == "present"
        append_deleted(cur, refine.id, "reader latest-event probe")
        latest = find_refine_artifact(cur, refine_id)
        assert latest.descriptor == refine
        assert latest.state == "deleted"
    assert conn.execute(
        "SELECT stage, refine_id, detect_id FROM pipeline_artifacts ORDER BY stage"
    ).fetchall() == [("detect", None, detect_id), ("refine", refine_id, None)]

    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                insert_present(cur, _descriptor("refine", refine_id))
    other_refine, _ = _add_runs(conn)
    with conn.cursor() as cur:
        insert_present(cur, _descriptor("refine", other_refine))
    assert conn.execute("SELECT count(*) FROM pipeline_artifacts").fetchone() == (3,)

    rollback_refine, _ = _add_runs(conn)
    rolled_back = _descriptor("refine", rollback_refine, artifact_id=uuid4())
    with pytest.raises(RuntimeError, match="rollback probe"):
        with conn.transaction():
            with conn.cursor() as cur:
                insert_present(cur, rolled_back)
            raise RuntimeError("rollback probe")
    assert conn.execute(
        "SELECT count(*) FROM pipeline_artifacts WHERE id = %s", (rolled_back.id,),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT count(*) FROM pipeline_artifact_events WHERE artifact_id = %s",
        (rolled_back.id,),
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "column,value",
    [
        ("relative_path", "/absolute"),
        ("manifest", "[]"),
        ("manifest_sha256", "BAD"),
        ("source_fingerprint", "[]"),
        ("row_count", -1),
        ("byte_count", -1),
    ],
)
def test_database_rejects_invalid_artifact_fields(
    migrated_connection, column: str, value: object,
) -> None:
    import psycopg

    conn = migrated_connection
    refine_id, _ = _add_runs(conn)
    descriptor = _descriptor("refine", refine_id)
    values: dict[str, object] = {
        "relative_path": descriptor.relative_path,
        "manifest": descriptor.manifest_json,
        "manifest_sha256": descriptor.manifest_sha256,
        "source_fingerprint": descriptor.source_fingerprint_json,
        "row_count": descriptor.row_count,
        "byte_count": descriptor.byte_count,
    }
    values[column] = value
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute(
                "INSERT INTO pipeline_artifacts "
                "(id, stage, refine_id, relative_path, manifest_version, manifest, "
                "manifest_sha256, source_fingerprint, row_count, byte_count) "
                "VALUES (%s, 'refine', %s, %s, 1, %s::jsonb, %s, %s::jsonb, %s, %s)",
                (descriptor.id, refine_id, values["relative_path"], values["manifest"],
                 values["manifest_sha256"], values["source_fingerprint"],
                 values["row_count"], values["byte_count"]),
            )


def test_owner_constraints_foreign_keys_and_registry_immutability(migrated_connection) -> None:
    import psycopg

    conn = migrated_connection
    refine_id, detect_id = _add_runs(conn)
    artifact = _descriptor("refine", refine_id)
    with conn.cursor() as cur:
        insert_present(cur, artifact)
    invalid_base = (
        uuid4(), "refine", refine_id, detect_id, "bad/owner", "a" * 64,
    )
    with pytest.raises(psycopg.errors.CheckViolation) as owner_error:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pipeline_artifacts "
                "(id, stage, refine_id, detect_id, relative_path, manifest_version, manifest, "
                "manifest_sha256, source_fingerprint, row_count, byte_count) "
                "VALUES (%s, %s, %s, %s, %s, 1, '{}', %s, '{}', 0, 0)", invalid_base,
            )
    assert owner_error.value.diag.constraint_name == "pipeline_artifacts_owner_stage_check"
    with pytest.raises(psycopg.errors.CheckViolation) as stage_error:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pipeline_artifacts "
                "(id, stage, refine_id, relative_path, manifest_version, manifest, "
                "manifest_sha256, source_fingerprint, row_count, byte_count) "
                "VALUES (%s, 'clip', %s, 'bad/stage', 1, '{}', %s, '{}', 0, 0)",
                (uuid4(), refine_id, "a" * 64),
            )
    assert stage_error.value.diag.constraint_name in {
        "pipeline_artifacts_stage_check", "pipeline_artifacts_owner_stage_check",
    }
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            conn.execute("SET LOCAL track_v2.allow_completed_run_delete = 'on'")
            conn.execute("DELETE FROM refine_runs WHERE id = %s", (refine_id,))
    for statement in (
        "UPDATE pipeline_artifacts SET relative_path = 'changed' WHERE id = %s",
        "DELETE FROM pipeline_artifacts WHERE id = %s",
        "UPDATE pipeline_artifact_events SET event_at = now() WHERE artifact_id = %s",
        "DELETE FROM pipeline_artifact_events WHERE artifact_id = %s",
    ):
        with pytest.raises(psycopg.Error, match="immutable"):
            with conn.transaction():
                conn.execute(statement, (artifact.id,))


def test_event_lifecycle_rejects_missing_present_duplicates_and_resurrection(
    migrated_connection,
) -> None:
    import psycopg

    conn = migrated_connection
    refine_id, _ = _add_runs(conn)
    artifact = _descriptor("refine", refine_id)
    # Metadata without its first event is possible only inside the caller-owned transaction.
    conn.execute(
        "INSERT INTO pipeline_artifacts "
        "(id, stage, refine_id, relative_path, manifest_version, manifest, manifest_sha256, "
        "source_fingerprint, row_count, byte_count) "
        "VALUES (%s, 'refine', %s, %s, 1, %s::jsonb, %s, %s::jsonb, %s, %s)",
        (artifact.id, refine_id, artifact.relative_path, artifact.manifest_json,
         artifact.manifest_sha256, artifact.source_fingerprint_json,
         artifact.row_count, artifact.byte_count),
    )
    with pytest.raises(psycopg.Error, match="first artifact event must be present"):
        with conn.transaction():
            append_deleted(conn.cursor(), artifact.id, "never published")
    conn.execute(
        "INSERT INTO pipeline_artifact_events(artifact_id, state) VALUES (%s, 'present')",
        (artifact.id,),
    )
    with pytest.raises(psycopg.Error, match="only transition to deleted"):
        with conn.transaction():
            conn.execute(
                "INSERT INTO pipeline_artifact_events(artifact_id, state) VALUES (%s, 'present')",
                (artifact.id,),
            )
    with conn.cursor() as cur:
        append_deleted(cur, artifact.id, "retention expired")
    for state, reason in (("deleted", "again"), ("present", None)):
        with pytest.raises(psycopg.Error, match="cannot receive more events"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO pipeline_artifact_events(artifact_id, state, reason) "
                    "VALUES (%s, %s, %s)", (artifact.id, state, reason),
                )


def test_event_trigger_serializes_on_artifact_row_lock() -> None:
    import psycopg
    from psycopg import sql

    schema = "artifact_lock_" + uuid4().hex
    dsn = os.environ["ROADNET_TEST_DSN"]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with admin.transaction():
                admin.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)
                ))
                _install_v4(admin)
                with admin.cursor() as cur:
                    migrate_artifacts_v5(cur)
                refine_id, _ = _add_runs(admin)
                artifact = _descriptor("refine", refine_id)
                with admin.cursor() as cur:
                    insert_present(cur, artifact)

            with psycopg.connect(dsn) as holder, psycopg.connect(dsn) as contender:
                for conn in (holder, contender):
                    conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(
                        sql.Identifier(schema)
                    ))
                holder.execute(
                    "SELECT 1 FROM pipeline_artifacts WHERE id = %s FOR UPDATE", (artifact.id,),
                )
                contender.execute("SET lock_timeout TO '200ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    with contender.transaction():
                        with contender.cursor() as cur:
                            append_deleted(cur, artifact.id, "locked probe")
                holder.rollback()
                with contender.cursor() as cur:
                    append_deleted(cur, artifact.id, "after lock release")
                contender.commit()
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_v6_refine_completion_requires_latest_present_artifact(
    migrated_connection,
) -> None:
    import psycopg

    conn = migrated_connection
    legacy_id, _ = _add_runs(conn)
    conn.execute(
        "UPDATE refine_runs SET status='completed', finished_at=now() WHERE id=%s",
        (legacy_id,),
    )

    with pytest.raises(RuntimeError, match="rollback probe"):
        with conn.transaction():
            with conn.cursor() as cur:
                migrate_completion_v6(cur)
            raise RuntimeError("rollback probe")
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (5,)
    assert conn.execute(
        "SELECT to_regprocedure('validate_refine_artifact_completion()')"
    ).fetchone() == (None,)

    with conn.cursor() as cur:
        migrate_completion_v6(cur)
        migrate_completion_v6(cur)
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (6,)
    assert conn.execute(
        "SELECT status FROM refine_runs WHERE id=%s", (legacy_id,),
    ).fetchone() == ("completed",)

    def finish(refine_id: UUID, status: str = "completed") -> None:
        conn.execute(
            "UPDATE refine_runs SET status=%s, finished_at=now() WHERE id=%s",
            (status, refine_id),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

    missing_id, _ = _add_runs(conn)
    with pytest.raises(psycopg.Error, match="requires exactly one present"):
        with conn.transaction():
            finish(missing_id)

    metadata_id, _ = _add_runs(conn)
    metadata = _descriptor("refine", metadata_id)
    conn.execute(
        "INSERT INTO pipeline_artifacts "
        "(id, stage, refine_id, relative_path, manifest_version, manifest, "
        "manifest_sha256, source_fingerprint, row_count, byte_count) VALUES "
        "(%s, 'refine', %s, %s, 1, %s::jsonb, %s, %s::jsonb, %s, %s)",
        (metadata.id, metadata_id, metadata.relative_path, metadata.manifest_json,
         metadata.manifest_sha256, metadata.source_fingerprint_json,
         metadata.row_count, metadata.byte_count),
    )
    with pytest.raises(psycopg.Error, match="requires exactly one present"):
        with conn.transaction():
            finish(metadata_id)

    present_id, _ = _add_runs(conn)
    with conn.transaction():
        with conn.cursor() as cur:
            insert_present(cur, _descriptor("refine", present_id))
        finish(present_id)
    assert conn.execute(
        "SELECT status FROM refine_runs WHERE id=%s", (present_id,),
    ).fetchone() == ("completed",)

    deleted_id, _ = _add_runs(conn)
    deleted = _descriptor("refine", deleted_id)
    with conn.cursor() as cur:
        insert_present(cur, deleted)
        append_deleted(cur, deleted.id, "fixture deletion")
    with pytest.raises(psycopg.Error, match="requires exactly one present"):
        with conn.transaction():
            finish(deleted_id)

    failed_id, _ = _add_runs(conn)
    finish(failed_id, "failed")
    assert conn.execute(
        "SELECT status FROM refine_runs WHERE id=%s", (failed_id,),
    ).fetchone() == ("failed",)


def test_refine_completion_cannot_observe_uncommitted_artifact() -> None:
    import psycopg
    from psycopg import sql

    schema = "completion_race_" + uuid4().hex
    dsn = os.environ["ROADNET_TEST_DSN"]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with admin.transaction():
                admin.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)
                ))
                for _name, ddl in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
                    admin.execute(_postgres_ddl(ddl))
                admin.execute(
                    "INSERT INTO pipeline_schema_versions(component, version) "
                    "VALUES ('track.v2', 6)"
                )
                refine_id = uuid4()
                admin.execute(
                    "INSERT INTO refine_runs(id, source_table, status) "
                    "VALUES (%s, 'source.points', 'running')", (refine_id,),
                )

            with psycopg.connect(dsn) as publisher, psycopg.connect(dsn) as completer:
                for conn in (publisher, completer):
                    conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(
                        sql.Identifier(schema)
                    ))
                with publisher.cursor() as cur:
                    insert_present(cur, _descriptor("refine", refine_id))
                with pytest.raises(psycopg.Error, match="requires exactly one present"):
                    completer.execute(
                        "UPDATE refine_runs SET status='completed', finished_at=now() "
                        "WHERE id=%s", (refine_id,),
                    )
                    completer.execute("SET CONSTRAINTS ALL IMMEDIATE")
                publisher.rollback()
                completer.rollback()
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_v7_detect_completion_requires_same_transaction_present_artifact(
    migrated_connection,
) -> None:
    import psycopg

    conn = migrated_connection
    with conn.cursor() as cur:
        migrate_completion_v6(cur)
        migrate_completion_v7(cur)
    assert conn.execute("SELECT version FROM pipeline_schema_versions").fetchone() == (7,)

    refine_id, missing_id = _add_runs(conn)
    with conn.cursor() as cur:
        insert_present(cur, _descriptor("refine", refine_id))
    conn.execute(
        "UPDATE refine_runs SET status='completed', finished_at=now() WHERE id=%s",
        (refine_id,),
    )
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with pytest.raises(psycopg.Error, match="requires exactly one present Detect artifact"):
        with conn.transaction():
            conn.execute(
                "UPDATE detect_runs SET status='completed', finished_at=now() WHERE id=%s",
                (missing_id,),
            )
    assert conn.execute(
        "SELECT status FROM detect_runs WHERE id=%s", (missing_id,),
    ).fetchone() == ("running",)

    _refine, success_id = _add_runs(conn)
    conn.execute(
        "UPDATE detect_runs SET refine_id=%s WHERE id=%s", (refine_id, success_id),
    )
    descriptor = _descriptor("detect", success_id)
    with conn.transaction():
        PostgresPipelineStore(conn).complete_detect_with_artifact(
            str(success_id), descriptor,
        )
    assert find_detect_artifact(conn.cursor(), success_id).state == "present"
    assert conn.execute(
        "SELECT status FROM detect_runs WHERE id=%s", (success_id,),
    ).fetchone() == ("completed",)
    with pytest.raises(ValueError, match="does not belong"):
        PostgresPipelineStore(conn).complete_detect_with_artifact(
            str(missing_id), _descriptor("detect", success_id),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            with conn.cursor() as cur:
                insert_present(cur, _descriptor("detect", success_id))

    _refine, deleted_id = _add_runs(conn)
    conn.execute(
        "UPDATE detect_runs SET refine_id=%s WHERE id=%s", (refine_id, deleted_id),
    )
    deleted = _descriptor("detect", deleted_id)
    with conn.cursor() as cur:
        insert_present(cur, deleted)
        append_deleted(cur, deleted.id, "non-present probe")
    with pytest.raises(psycopg.Error, match="requires exactly one present Detect artifact"):
        with conn.transaction():
            conn.execute(
                "UPDATE detect_runs SET status='completed', finished_at=now() WHERE id=%s",
                (deleted_id,),
            )

    _refine, rollback_id = _add_runs(conn)
    conn.execute(
        "UPDATE detect_runs SET refine_id=%s WHERE id=%s", (refine_id, rollback_id),
    )
    rolled_back = _descriptor("detect", rollback_id)
    with pytest.raises(RuntimeError, match="rollback probe"):
        with conn.transaction():
            PostgresPipelineStore(conn).complete_detect_with_artifact(
                str(rollback_id), rolled_back,
            )
            raise RuntimeError("rollback probe")
    assert conn.execute(
        "SELECT status FROM detect_runs WHERE id=%s", (rollback_id,),
    ).fetchone() == ("running",)
    assert conn.execute(
        "SELECT count(*) FROM pipeline_artifacts WHERE detect_id=%s", (rollback_id,),
    ).fetchone() == (0,)
