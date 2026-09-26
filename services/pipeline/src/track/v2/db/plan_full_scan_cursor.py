"""Scoped planning for server-side cursors whose results are fully consumed."""
from __future__ import annotations

from typing import Any


def declare_full_scan_cursor(
    conn: Any,
    cursor: Any,
    query: str,
    params: Any,
    *,
    discourage_nested_loop: bool = False,
) -> None:
    """Plan the complete scan, restoring the caller's setting before FETCH.

    Reading the current setting first opens an implicit transaction on a fresh
    non-autocommit connection. The nested transaction therefore is a savepoint:
    a successful declaration does not commit and close the non-holdable cursor,
    and an error restores settings without discarding earlier caller writes.
    FETCH uses the already planned portal after the setting has been restored.
    """
    with conn.cursor() as settings:
        if discourage_nested_loop:
            settings.execute(
                "SELECT current_setting('cursor_tuple_fraction'), "
                "current_setting('enable_nestloop')"
            )
            previous_fraction, previous_nested_loop = settings.fetchone()
        else:
            settings.execute("SELECT current_setting('cursor_tuple_fraction')")
            previous_fraction = settings.fetchone()[0]
        with conn.transaction():
            settings.execute("SET LOCAL cursor_tuple_fraction = 1.0")
            if discourage_nested_loop:
                settings.execute("SET LOCAL enable_nestloop = off")
            cursor.execute(query, params)
            if discourage_nested_loop:
                settings.execute(
                    "SELECT set_config('cursor_tuple_fraction', %s, true), "
                    "set_config('enable_nestloop', %s, true)",
                    (previous_fraction, previous_nested_loop),
                )
            else:
                settings.execute(
                    "SELECT set_config('cursor_tuple_fraction', %s, true)",
                    (previous_fraction,),
                )
