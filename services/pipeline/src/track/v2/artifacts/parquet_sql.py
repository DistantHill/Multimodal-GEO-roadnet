"""SQL literal quoting used by Parquet readers and exporters."""

from __future__ import annotations


def quote_sql_literal(value: str) -> str:
    """Return a single-quoted SQL string literal with embedded quotes escaped."""
    return "'" + value.replace("'", "''") + "'"
