"""Runtime guard for the destructive track.v2 schema-v9 API."""

from __future__ import annotations

from typing import Any


SCHEMA_COMPONENT = "track.v2"
SCHEMA_VERSION = 9


def require_current_schema(conn: Any) -> None:
    """Fail before work unless the current track.v2 schema is installed."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('pipeline_schema_versions')")
        if cur.fetchone()[0] is None:
            raise RuntimeError(
                "track.v2 schema metadata is missing; run init_db --reset"
            )
        cur.execute(
            "SELECT version FROM pipeline_schema_versions WHERE component = %s",
            (SCHEMA_COMPONENT,),
        )
        row = cur.fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION:
        actual = "missing" if row is None else f"v{int(row[0])}"
        raise RuntimeError(
            f"track.v2 schema mismatch: expected v{SCHEMA_VERSION}, got {actual}; "
            "for v4 through v8 run init-db --migrate-artifacts; "
            "v3 and older schemas require --reset"
        )
