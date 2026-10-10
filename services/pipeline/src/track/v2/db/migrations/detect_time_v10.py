"""Preserve Refine artifacts while allowing time-selected Detect completion."""
from __future__ import annotations
from typing import Any
from ..schema import SCHEMA_COMPONENT


def migrate_detect_time_v10(cur: Any) -> None:
    from ...cli.init_db import POST_DDL_STATEMENTS
    cur.execute("SELECT version FROM pipeline_schema_versions WHERE component=%s FOR UPDATE", (SCHEMA_COMPONENT,))
    row = cur.fetchone()
    if row is None or int(row[0]) != 9:
        raise RuntimeError("Detect time selection migration requires track.v2 schema v9")
    for name, ddl in POST_DDL_STATEMENTS:
        if name == "validate_detect_completion_function":
            cur.execute(ddl)
    cur.execute("UPDATE pipeline_schema_versions SET version=10 WHERE component=%s", (SCHEMA_COMPONENT,))
