from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg


def postgres_connection_settings() -> dict[str, Any]:
    """Return the shared PostgreSQL connection settings for the Backend."""
    return {
        "host": os.getenv("POSTGRES_HOST", "postgres"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DB", "roadnet"),
        "user": os.getenv("POSTGRES_USER", "roadnet"),
        "password": os.getenv("POSTGRES_PASSWORD", "change-me"),
        "connect_timeout": 5,
    }


def get_read_connection() -> Iterator[Any]:
    """Yield a database connection whose transaction cannot write.

    Map, Replay, and Clip are deliberately read-only Backend projections over
    persisted Pipeline artifacts.  A rollback in ``finally`` also guarantees
    that a client disconnect never leaves an open transaction behind.
    """
    connection = psycopg.connect(**postgres_connection_settings())
    try:
        connection.execute("SET TRANSACTION READ ONLY")
        requested_timeout = int(os.getenv("MAP_STATEMENT_TIMEOUT_MS", "10000"))
        statement_timeout_ms = min(60_000, max(100, requested_timeout))
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{statement_timeout_ms}ms",),
        )
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
