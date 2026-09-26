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


class ScriptedCursor:
    def __init__(self, responses: list[tuple[Any, ...] | None]) -> None:
        self.responses = responses
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "ScriptedCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.responses.pop(0)


class ScriptedConnection:
    def __init__(self, responses: list[tuple[Any, ...] | None]) -> None:
        self.cursor_instance = ScriptedCursor(responses)

    def cursor(self) -> ScriptedCursor:
        return self.cursor_instance


def test_read_connection_is_timed_read_only_and_closes_after_handler_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(database.psycopg, "connect", lambda **kwargs: connection)
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


def test_postgres_settings_have_a_connect_timeout() -> None:
    settings = database.postgres_connection_settings()

    assert settings["connect_timeout"] == 5


def test_target_database_connection_does_not_mutate_default(monkeypatch):
    calls = []
    connection = FakeConnection()
    def connect(**kwargs):
        calls.append(kwargs)
        return connection
    monkeypatch.setattr(database.psycopg, "connect", connect)
    with database.open_read_connection("other' database"):
        pass
    assert calls[0]["dbname"] == "other' database"
    assert database.postgres_connection_settings()["dbname"] == "roadnet"
    assert connection.closed and connection.rolled_back


def test_schema_guard_accepts_only_current_track_v2_schema() -> None:
    assert database.SCHEMA_VERSION == 9
    current = ScriptedConnection([("pipeline_schema_versions",), (9,)])
    database.require_current_schema(current)
    assert current.cursor_instance.executions[1][1] == ("track.v2",)

    stale = ScriptedConnection([("pipeline_schema_versions",), (2,)])
    with pytest.raises(RuntimeError, match="expected v9, got v2"):
        database.require_current_schema(stale)

    missing = ScriptedConnection([(None,)])
    with pytest.raises(RuntimeError, match="schema metadata is missing"):
        database.require_current_schema(missing)
