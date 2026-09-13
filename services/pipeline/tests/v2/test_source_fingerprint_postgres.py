"""Opt-in fingerprint checks using only transaction-local PostgreSQL tables."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from typing import Any

import pytest

from track.v2.db.source_table import SourceTableContract, calculate_source_fingerprint


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)

_CONTRACT = SourceTableContract(
    source_table="pg_temp.fingerprint_source",
    quoted_table='"pg_temp"."fingerprint_source"',
    key_sql_type="bigint",
    has_extra=True,
)


@pytest.fixture
def fingerprint_connection() -> Iterator[Any]:
    import psycopg

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        try:
            # No public schema fallback: even an accidental missing temp table
            # must fail instead of reading or modifying a persistent table.
            conn.execute("SET LOCAL search_path TO pg_temp, pg_catalog")
            conn.execute("""
                CREATE TEMP TABLE fingerprint_source (
                    id bigint PRIMARY KEY,
                    vehicle_id text NOT NULL,
                    timestamp double precision,
                    lon double precision,
                    lat double precision,
                    heading double precision,
                    speed double precision,
                    extra jsonb
                ) ON COMMIT DROP;
                CREATE TEMP TABLE kept_trajectory_points (
                    clean_id text NOT NULL,
                    source_row_key text COLLATE "C" NOT NULL
                ) ON COMMIT DROP;
                CREATE TEMP TABLE excluded_points (
                    clean_id text NOT NULL,
                    source_row_key text COLLATE "C" NOT NULL
                ) ON COMMIT DROP;
                INSERT INTO pg_temp.fingerprint_source VALUES
                    (2, '车辆甲', 2, 121.5, 31, NULL, 0, '{"z":1,"a":"中文"}'),
                    (10, 'truck', 10, 120, 30, 90, 2, NULL),
                    (99, 'unreferenced', 99, 100, 20, 0, 1, '{}');
                INSERT INTO pg_temp.kept_trajectory_points VALUES
                    ('clean-1', '2'), ('clean-1', '2'), ('other-clean', '99');
                INSERT INTO pg_temp.excluded_points VALUES
                    ('clean-1', '2'), ('clean-1', '10')
            """)
            yield conn
        finally:
            # Explicit caller rollback also covers failed assertions and makes
            # teardown independent of the connection context manager's commit.
            conn.rollback()


def _expected_fingerprint() -> dict[str, object]:
    # Independent literal v1 encoding: text-key order is "10", then "2".
    # Duplicate references must count once; another Clean's row is excluded.
    records = (
        '{"extra":{},"heading":90.0,"lat":30.0,"lon":120.0,'
        '"source_row_key":"10","speed":2.0,"timestamp":10.0,"vehicle_id":"truck"}',
        '{"extra":{"a":"中文","z":1},"heading":null,"lat":31.0,"lon":121.5,'
        '"source_row_key":"2","speed":0.0,"timestamp":2.0,"vehicle_id":"车辆甲"}',
    )
    digest = hashlib.sha256()
    for record in records:
        encoded = record.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {"version": 1, "row_count": 2, "sha256": digest.hexdigest()}


def _assert_caller_transaction_is_intact(conn: Any) -> None:
    from psycopg.pq import TransactionStatus

    # Both an internal commit and a rollback would remove these temp tables.
    assert conn.info.transaction_status == TransactionStatus.INTRANS
    assert conn.execute("SELECT count(*) FROM pg_temp.fingerprint_source").fetchone() == (3,)
    assert conn.execute(
        "SELECT count(*) FROM pg_temp.kept_trajectory_points"
    ).fetchone() == (3,)
    assert conn.execute(
        "SELECT count(*) FROM pg_catalog.pg_cursors WHERE name LIKE 'source_fingerprint_%'"
    ).fetchone() == (0,)


def test_postgres_fingerprint_v1_scope_fetch_size_and_cursor_lifetime(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    first = calculate_source_fingerprint(conn, "clean-1", _CONTRACT, fetch_size=1)
    _assert_caller_transaction_is_intact(conn)
    second = calculate_source_fingerprint(conn, "clean-1", _CONTRACT, fetch_size=2)
    _assert_caller_transaction_is_intact(conn)
    assert first == second == _expected_fingerprint()

    conn.execute("UPDATE pg_temp.fingerprint_source SET speed = 123 WHERE id = 99")
    assert calculate_source_fingerprint(conn, "clean-1", _CONTRACT, fetch_size=1) == first
    _assert_caller_transaction_is_intact(conn)
    # Also check that the function did not undo the caller's pending change.
    assert conn.execute(
        "SELECT speed FROM pg_temp.fingerprint_source WHERE id = 99"
    ).fetchone() == (123.0,)

    conn.execute("UPDATE pg_temp.fingerprint_source SET speed = 7 WHERE id = 10")
    changed = calculate_source_fingerprint(conn, "clean-1", _CONTRACT, fetch_size=2)
    assert changed["version"] == 1
    assert changed["row_count"] == 2
    assert changed["sha256"] != first["sha256"]
    _assert_caller_transaction_is_intact(conn)


def test_postgres_missing_reference_closes_cursor_without_aborting_transaction(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    conn.execute(
        "INSERT INTO pg_temp.excluded_points VALUES ('clean-1', '404')"
    )
    with pytest.raises(ValueError, match="references missing source row '404'"):
        calculate_source_fingerprint(conn, "clean-1", _CONTRACT, fetch_size=1)
    _assert_caller_transaction_is_intact(conn)
    assert conn.execute(
        "SELECT count(*) FROM pg_temp.excluded_points WHERE source_row_key = '404'"
    ).fetchone() == (1,)

    # Recovery is the caller's choice; a Python validation error must not leave
    # PostgreSQL INERROR or silently roll back earlier work in this transaction.
    conn.execute("DELETE FROM pg_temp.excluded_points WHERE source_row_key = '404'")
    recovered = calculate_source_fingerprint(conn, "clean-1", _CONTRACT, fetch_size=2)
    assert recovered == _expected_fingerprint()
    _assert_caller_transaction_is_intact(conn)
