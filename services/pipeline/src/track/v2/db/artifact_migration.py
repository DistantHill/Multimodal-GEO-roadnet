"""Transactional, additive track.v2 schema v4 to v5 migration."""

from __future__ import annotations

from typing import Any

from .schema import SCHEMA_COMPONENT


ARTIFACT_SCHEMA_VERSION = 5


_ARTIFACT_TABLES = {"pipeline_artifacts", "pipeline_artifact_events"}
_ARTIFACT_INDEXES = {
    "uq_pipeline_artifacts_refine_run",
    "uq_pipeline_artifacts_detect_run",
}
_ARTIFACT_GUARDS = {
    "guard_pipeline_artifact_immutability_function",
    "pipeline_artifact_immutability_triggers",
    "validate_pipeline_artifact_event_function",
    "pipeline_artifact_event_insert_trigger",
}


def migrate_artifacts_v5(cur: Any) -> None:
    """Add the artifact registry and publish v5 last in the caller transaction."""
    from ..cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS

    cur.execute("LOCK TABLE pipeline_schema_versions IN ACCESS EXCLUSIVE MODE")
    cur.execute(
        "SELECT version FROM pipeline_schema_versions WHERE component = %s",
        (SCHEMA_COMPONENT,),
    )
    row = cur.fetchone()
    if row is not None and int(row[0]) == ARTIFACT_SCHEMA_VERSION:
        return
    if row is None or int(row[0]) != 4:
        raise RuntimeError("--migrate-artifacts requires an existing schema v4 database")

    for name, ddl in DDL_STATEMENTS:
        if name in _ARTIFACT_TABLES:
            cur.execute(ddl)
    for name, ddl in POST_DDL_STATEMENTS:
        if name in _ARTIFACT_GUARDS:
            cur.execute(ddl)
    for name, ddl in INDEX_STATEMENTS:
        if name in _ARTIFACT_INDEXES:
            cur.execute(ddl)
    cur.execute(
        "UPDATE pipeline_schema_versions SET version = %s WHERE component = %s",
        (ARTIFACT_SCHEMA_VERSION, SCHEMA_COMPONENT),
    )
