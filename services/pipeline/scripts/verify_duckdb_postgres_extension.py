"""Verify the bundled DuckDB PostgreSQL extension without network access."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb


extension_path = Path(os.environ["DUCKDB_POSTGRES_EXTENSION_PATH"])
if not extension_path.is_absolute():
    raise RuntimeError("DUCKDB_POSTGRES_EXTENSION_PATH must be absolute")
if not extension_path.is_file():
    raise RuntimeError(f"bundled extension is missing: {extension_path}")

connection = duckdb.connect(
    config={
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
    }
)
escaped_path = str(extension_path).replace("'", "''")
connection.execute(f"LOAD '{escaped_path}'")
loaded = connection.execute(
    """
    SELECT loaded
    FROM duckdb_extensions()
    WHERE extension_name = 'postgres_scanner'
    """
).fetchone()[0]
postgres_functions = connection.execute(
    """
    SELECT count(*)
    FROM duckdb_functions()
    WHERE function_name IN ('postgres_scan', 'postgres_attach')
    """
).fetchone()[0]

assert loaded
assert postgres_functions >= 2
print(f"duckdb={duckdb.__version__} postgres_scanner=loaded functions={postgres_functions}")
