"""Transactional, data-preserving schema v3 → v4 Refine rename."""

from __future__ import annotations

from typing import Any

from psycopg import sql

from ..schema import SCHEMA_COMPONENT


REFINE_SCHEMA_VERSION = 4
_LATER_SCHEMA_OBJECTS = {
    "guard_pipeline_artifact_immutability_function",
    "pipeline_artifact_immutability_triggers",
    "validate_pipeline_artifact_event_function",
    "pipeline_artifact_event_insert_trigger",
    "uq_pipeline_artifacts_refine_run",
    "uq_pipeline_artifacts_detect_run",
    "validate_refine_artifact_completion_function",
    "refine_artifact_completion_trigger",
    "validate_detect_artifact_completion_function",
    "detect_artifact_completion_trigger",
}


def _refine_name(name: str) -> str:
    return name.replace("cleaned", "refined").replace("clean", "refine")


def migrate_refine(cur: Any) -> None:
    """Rename v3 objects and rebuild guards within the caller's transaction."""
    from ...cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS

    cur.execute("LOCK TABLE pipeline_schema_versions IN ACCESS EXCLUSIVE MODE")
    cur.execute(
        "SELECT version FROM pipeline_schema_versions WHERE component = %s",
        (SCHEMA_COMPONENT,),
    )
    row = cur.fetchone()
    if row is not None and int(row[0]) == REFINE_SCHEMA_VERSION:
        return
    if row is None or int(row[0]) != 3:
        raise RuntimeError("--migrate-refine requires an existing schema v3 database")

    cur.execute("ALTER TABLE clean_runs RENAME TO refine_runs")
    for table in ("detect_runs", "excluded_points", "kept_trajectory_points", "injected_points"):
        cur.execute(sql.SQL("ALTER TABLE {} RENAME COLUMN clean_id TO refine_id").format(
            sql.Identifier(table),
        ))
    cur.execute(
        "ALTER TABLE resolved_subtrack_points "
        "RENAME COLUMN cleaned_sequence_in_subtrack TO refined_sequence_in_subtrack"
    )

    tables = [name for name, _ddl in DDL_STATEMENTS]
    # Renaming constraints also renames their backing indexes. Query the
    # remaining standalone indexes afterwards so they are not renamed twice.
    cur.execute(
        "SELECT rel.relname, con.conname FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
        "WHERE ns.nspname = current_schema() AND rel.relname = ANY(%s) "
        "AND con.conname LIKE '%%clean%%'",
        (tables,),
    )
    for table, name in cur.fetchall():
        cur.execute(sql.SQL("ALTER TABLE {} RENAME CONSTRAINT {} TO {}").format(
            sql.Identifier(table), sql.Identifier(name),
            sql.Identifier(_refine_name(name)),
        ))
    cur.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = ANY(%s) AND indexname LIKE '%%clean%%'",
        (tables,),
    )
    for (name,) in cur.fetchall():
        cur.execute(sql.SQL("ALTER INDEX {} RENAME TO {}").format(
            sql.Identifier(name), sql.Identifier(_refine_name(name)),
        ))
    cur.execute(
        "SELECT rel.relname, trg.tgname FROM pg_trigger trg "
        "JOIN pg_class rel ON rel.oid = trg.tgrelid "
        "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
        "WHERE ns.nspname = current_schema() AND rel.relname = ANY(%s) "
        "AND NOT trg.tgisinternal AND trg.tgname LIKE '%%clean%%'",
        (tables,),
    )
    for table, name in cur.fetchall():
        cur.execute(sql.SQL("ALTER TRIGGER {} ON {} RENAME TO {}").format(
            sql.Identifier(name), sql.Identifier(table),
            sql.Identifier(_refine_name(name)),
        ))
    # Earlier v3 installations predate the artifact mutation guard. Look only
    # in the target schema; a same-named function elsewhere is not ours.
    cur.execute(
        "SELECT ns.nspname FROM pg_proc proc "
        "JOIN pg_namespace ns ON ns.oid = proc.pronamespace "
        "WHERE ns.nspname = current_schema() "
        "AND proc.proname = 'guard_clean_artifact_mutation' "
        "AND proc.pronargs = 0"
    )
    legacy_guard = cur.fetchone()
    if legacy_guard is not None:
        cur.execute(sql.SQL("ALTER FUNCTION {}.{}() RENAME TO {}").format(
            sql.Identifier(legacy_guard[0]),
            sql.Identifier("guard_clean_artifact_mutation"),
            sql.Identifier("guard_refine_artifact_mutation"),
        ))
    # PostgreSQL rewrites FK/constraint dependencies, but not PL/pgSQL source.
    # Reinstall all authoritative guards before publishing version 4.
    for name, ddl in (*POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
        if name not in _LATER_SCHEMA_OBJECTS:
            cur.execute(ddl)
    cur.execute(
        "UPDATE pipeline_schema_versions SET version = %s WHERE component = %s",
        (REFINE_SCHEMA_VERSION, SCHEMA_COMPONENT),
    )
