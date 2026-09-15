"""Scoped planning for server-side cursors whose results are fully consumed."""
from __future__ import annotations

from typing import Any


def declare_full_scan_cursor(
    conn: Any,
    cursor: Any,
    query: str,
    params: Any,
) -> None:
    """Plan the complete scan, restoring the caller's setting before FETCH.

    Reading the current setting first opens an implicit transaction on a fresh
    non-autocommit connection. The nested transaction therefore is a savepoint:
    a successful declaration does not commit and close the non-holdable cursor,
    and an error restores settings without discarding earlier caller writes.
    FETCH uses the already planned portal after the setting has been restored.
    """
    with conn.cursor() as settings:
        settings.execute("SELECT current_setting('cursor_tuple_fraction')")
        previous = settings.fetchone()[0]
        with conn.transaction():
            settings.execute("SET LOCAL cursor_tuple_fraction = 1.0")
            cursor.execute(query, params)
            settings.execute(
                "SELECT set_config('cursor_tuple_fraction', %s, true)", (previous,),
            )
