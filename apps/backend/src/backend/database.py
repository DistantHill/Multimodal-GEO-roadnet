from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

import psycopg
from track.v2.db.schema import SCHEMA_VERSION, require_current_schema

from .config import BackendConfig


_config: BackendConfig | None = None


def configure_database(config: BackendConfig) -> None:
    global _config
    _config = config


def _require_config() -> BackendConfig:
    if _config is None:
        raise RuntimeError("Backend database configuration has not been initialized")
    return _config


def postgres_connection_settings() -> dict[str, Any]:
    """Return the shared PostgreSQL connection settings for the Backend."""
    postgres = _require_config().postgres
    return {"host": postgres.host, "port": postgres.port,
            "dbname": postgres.database, "user": postgres.user,
            "password": postgres.password,
            "connect_timeout": postgres.connect_timeout_seconds}


def verify_runtime_schema() -> None:
    """Open one startup connection and enforce the Pipeline schema contract."""
    with psycopg.connect(**postgres_connection_settings()) as connection:
        require_current_schema(connection)


def get_read_connection() -> Iterator[Any]:
    with open_read_connection() as connection:
        yield connection


@contextmanager
def open_read_connection(database_name: str | None = None) -> Iterator[Any]:
    """Yield a database connection whose transaction cannot write.

    Map, Replay, and Clip are deliberately read-only Backend projections over
    persisted Pipeline artifacts.  A rollback in ``finally`` also guarantees
    that a client disconnect never leaves an open transaction behind.
    """
    settings = postgres_connection_settings()
    if database_name is not None:
        settings["dbname"] = database_name
    connection = psycopg.connect(**settings)
    try:
        connection.execute("SET TRANSACTION READ ONLY")
        statement_timeout_ms = _require_config().postgres.statement_timeout_ms
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
