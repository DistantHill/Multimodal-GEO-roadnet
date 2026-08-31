from __future__ import annotations

from typing import Any

import pytest

from backend import database


class FakeConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append((sql, params))

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_read_connection_is_timed_read_only_and_closes_after_handler_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(database.psycopg, "connect", lambda **kwargs: connection)
    monkeypatch.setenv("MAP_STATEMENT_TIMEOUT_MS", "12000")
    dependency = database.get_read_connection()

    assert next(dependency) is connection
    with pytest.raises(RuntimeError, match="handler failed"):
        dependency.throw(RuntimeError("handler failed"))

    assert connection.executions == [
        ("SET TRANSACTION READ ONLY", ()),
        ("SELECT set_config('statement_timeout', %s, true)", ("12000ms",)),
    ]
    assert connection.rolled_back is True
    assert connection.closed is True


def test_postgres_settings_have_a_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    settings = database.postgres_connection_settings()

    assert settings["connect_timeout"] == 5
