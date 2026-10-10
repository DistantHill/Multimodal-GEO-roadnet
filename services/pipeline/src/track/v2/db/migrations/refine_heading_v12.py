"""Add nullable calculated headings without rewriting historical Refine results."""
from __future__ import annotations
from typing import Any
from ..schema import SCHEMA_COMPONENT


def migrate_refine_heading_v12(cur: Any) -> None:
    cur.execute("SELECT version FROM pipeline_schema_versions WHERE component=%s FOR UPDATE", (SCHEMA_COMPONENT,))
    row = cur.fetchone()
    if row is None or int(row[0]) != 11:
        raise RuntimeError("Refine heading migration requires track.v2 schema v11")
    cur.execute("ALTER TABLE kept_trajectory_points ADD COLUMN heading_cal DOUBLE PRECISION CHECK (heading_cal >= 0 AND heading_cal < 360)")
    cur.execute("ALTER TABLE injected_points ADD COLUMN heading_cal DOUBLE PRECISION CHECK (heading_cal >= 0 AND heading_cal < 360)")
    cur.execute("UPDATE pipeline_schema_versions SET version=12 WHERE component=%s", (SCHEMA_COMPONENT,))
