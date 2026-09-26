"""Add persisted Refine XY without rebuilding historical geometry or source data."""
from __future__ import annotations
from typing import Any
from ..schema import SCHEMA_COMPONENT


def migrate_kept_xy_v9(cur: Any) -> None:
    cur.execute('SELECT version FROM pipeline_schema_versions WHERE component = %s FOR UPDATE', (SCHEMA_COMPONENT,))
    row = cur.fetchone()
    if row is None or int(row[0]) != 8:
        raise RuntimeError('kept XY migration requires track.v2 schema v8')
    # Existing derived rows remain NULL and cannot be exported as new authority.
    # New Refine writes supply both columns; no source mutation or backfill occurs.
    cur.execute('ALTER TABLE kept_trajectory_points ADD COLUMN x DOUBLE PRECISION, ADD COLUMN y DOUBLE PRECISION')
    cur.execute('UPDATE pipeline_schema_versions SET version = 9 WHERE component = %s', (SCHEMA_COMPONENT,))
