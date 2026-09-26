"""Transactional track.v2 schema v5 to v6 Refine completion migration."""

from __future__ import annotations

from typing import Any

from ..schema import SCHEMA_COMPONENT


_TARGET_VERSION = 6


_COMPLETION_GUARDS = {
    "validate_refine_artifact_completion_function",
    "refine_artifact_completion_trigger",
}


def migrate_completion_v6(cur: Any) -> None:
    """Install the Refine completion guard and publish v6 last."""
    from ...cli.init_db import POST_DDL_STATEMENTS

    cur.execute("LOCK TABLE pipeline_schema_versions IN ACCESS EXCLUSIVE MODE")
    cur.execute(
        "SELECT version FROM pipeline_schema_versions WHERE component = %s",
        (SCHEMA_COMPONENT,),
    )
    row = cur.fetchone()
    if row is not None and int(row[0]) == _TARGET_VERSION:
        return
    if row is None or int(row[0]) != 5:
        raise RuntimeError("Refine completion migration requires an existing schema v5 database")

    for name, ddl in POST_DDL_STATEMENTS:
        if name in _COMPLETION_GUARDS:
            cur.execute(ddl)
    cur.execute(
        "UPDATE pipeline_schema_versions SET version = %s WHERE component = %s",
        (_TARGET_VERSION, SCHEMA_COMPONENT),
    )
