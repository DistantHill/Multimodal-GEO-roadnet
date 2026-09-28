"""DuckDB temp quota recovery keeps committed inputs and replays only failed SQL."""
from __future__ import annotations

import duckdb
import pytest

from track.v2.candidates.temp_quota import TempQuota, is_temp_quota_exhausted


def _connection(tmp_path):
    connection = duckdb.connect(config={
        "memory_limit": "16MB", "threads": "1",
        "temp_directory": str(tmp_path / "temp"), "max_temp_directory_size": "0B",
    })
    connection.execute("CREATE TEMP TABLE src AS SELECT i, md5(i::VARCHAR) AS value FROM range(120000) t(i)")
    connection.execute("SET max_temp_directory_size='0B'")
    return connection


def test_quota_retry_replays_failed_sql_only(tmp_path):
    connection = _connection(tmp_path)
    events = []
    quota = TempQuota(connection, "0B", "cross-grid", limits=("0B", "64MB"), emit=events.append)
    attempts = 0
    def attempt():
        nonlocal attempts
        attempts += 1
        connection.execute("BEGIN")
        try:
            connection.execute("CREATE TEMP TABLE result AS SELECT * FROM src ORDER BY value DESC")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    try:
        quota.run("sort", attempt)
        assert attempts == 2
        assert connection.execute("SELECT count(*) FROM src").fetchone()[0] == 120000
        assert connection.execute("SELECT count(*) FROM result").fetchone()[0] == 120000
        assert len(events) == 1 and "old=0B new=64MB" in events[0]
    finally:
        connection.close()


def test_quota_ceiling_and_other_errors_do_not_retry(tmp_path):
    connection = _connection(tmp_path)
    events = []
    quota = TempQuota(connection, "0B", "cross-grid", limits=("0B",), emit=events.append)
    attempts = 0
    def attempt():
        nonlocal attempts
        attempts += 1
        connection.execute("CREATE TEMP TABLE result AS SELECT * FROM src ORDER BY value DESC")
    try:
        with pytest.raises(duckdb.OutOfMemoryException) as caught:
            quota.run("sort", attempt)
        assert is_temp_quota_exhausted(caught.value)
        assert attempts == 1 and events == []
        with pytest.raises(ValueError, match="other"):
            quota.run("sort", lambda: (_ for _ in ()).throw(ValueError("other")))
        assert events == []
    finally:
        connection.close()
