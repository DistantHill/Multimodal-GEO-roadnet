"""Opt-in fingerprint checks using only transaction-local PostgreSQL tables."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from collections.abc import Iterator
from typing import Any

import pytest

from track.v2.db.source_table import (
    SourceTableContract, calculate_input_fingerprint, calculate_source_fingerprint,
)


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
                    refine_id text NOT NULL,
                    source_row_key text COLLATE "C" NOT NULL
                ) ON COMMIT DROP;
                CREATE TEMP TABLE excluded_points (
                    refine_id text NOT NULL,
                    source_row_key text COLLATE "C" NOT NULL
                ) ON COMMIT DROP;
                INSERT INTO pg_temp.fingerprint_source VALUES
                    (2, '车辆甲', 2, 121.5, 31, NULL, 0, '{"z":1,"a":"中文"}'),
                    (10, 'truck', 10, 120, 30, 90, 2, NULL),
                    (99, 'unreferenced', 99, 100, 20, 0, 1, '{}');
                INSERT INTO pg_temp.kept_trajectory_points VALUES
                    ('refine-1', '2'), ('refine-1', '2'), ('other-refine', '99');
                INSERT INTO pg_temp.excluded_points VALUES
                    ('refine-1', '2'), ('refine-1', '10')
            """)
            yield conn
        finally:
            # Explicit caller rollback also covers failed assertions and makes
            # teardown independent of the connection context manager's commit.
            conn.rollback()


def _expected_fingerprint() -> dict[str, object]:
    # Independent literal v1 encoding: text-key order is "10", then "2".
    # Duplicate references must count once; another Refine's row is excluded.
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
        "SELECT count(*) FROM pg_catalog.pg_cursors WHERE name LIKE 'source_fingerprint_%' "
        "OR name LIKE 'input_fingerprint_%'"
    ).fetchone() == (0,)


def test_postgres_fingerprint_v1_scope_fetch_size_and_cursor_lifetime(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    first = calculate_source_fingerprint(conn, "refine-1", _CONTRACT, fetch_size=1)
    _assert_caller_transaction_is_intact(conn)
    second = calculate_source_fingerprint(conn, "refine-1", _CONTRACT, fetch_size=2)
    _assert_caller_transaction_is_intact(conn)
    assert first == second == _expected_fingerprint()

    conn.execute("UPDATE pg_temp.fingerprint_source SET speed = 123 WHERE id = 99")
    assert calculate_source_fingerprint(conn, "refine-1", _CONTRACT, fetch_size=1) == first
    _assert_caller_transaction_is_intact(conn)
    # Also check that the function did not undo the caller's pending change.
    assert conn.execute(
        "SELECT speed FROM pg_temp.fingerprint_source WHERE id = 99"
    ).fetchone() == (123.0,)

    conn.execute("UPDATE pg_temp.fingerprint_source SET speed = 7 WHERE id = 10")
    changed = calculate_source_fingerprint(conn, "refine-1", _CONTRACT, fetch_size=2)
    assert changed["version"] == 1
    assert changed["row_count"] == 2
    assert changed["sha256"] != first["sha256"]
    _assert_caller_transaction_is_intact(conn)


def test_postgres_missing_reference_closes_cursor_without_aborting_transaction(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    conn.execute(
        "INSERT INTO pg_temp.excluded_points VALUES ('refine-1', '404')"
    )
    with pytest.raises(ValueError, match="references missing source row '404'"):
        calculate_source_fingerprint(conn, "refine-1", _CONTRACT, fetch_size=1)
    _assert_caller_transaction_is_intact(conn)
    assert conn.execute(
        "SELECT count(*) FROM pg_temp.excluded_points WHERE source_row_key = '404'"
    ).fetchone() == (1,)

    # Recovery is the caller's choice; a Python validation error must not leave
    # PostgreSQL INERROR or silently roll back earlier work in this transaction.
    conn.execute("DELETE FROM pg_temp.excluded_points WHERE source_row_key = '404'")
    recovered = calculate_source_fingerprint(conn, "refine-1", _CONTRACT, fetch_size=2)
    assert recovered == _expected_fingerprint()
    _assert_caller_transaction_is_intact(conn)


def test_postgres_input_fingerprint_includes_unreferenced_rows_and_preserves_v1(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    first = calculate_input_fingerprint(conn, _CONTRACT, fetch_size=1)
    assert first == calculate_input_fingerprint(conn, _CONTRACT, fetch_size=2)
    assert first["version"] == 2
    assert first["scope"] == "full_input"
    assert first["source_table"] == _CONTRACT.source_table
    assert first["key_sql_type"] == "bigint"
    assert first["row_count"] == 3
    _assert_caller_transaction_is_intact(conn)
    assert calculate_source_fingerprint(conn, "refine-1", _CONTRACT) == _expected_fingerprint()

    conn.execute("UPDATE pg_temp.fingerprint_source SET speed = 123 WHERE id = 99")
    changed = calculate_input_fingerprint(conn, _CONTRACT, fetch_size=1)
    assert changed["sha256"] != first["sha256"]
    assert changed["row_count"] == 3
    assert calculate_source_fingerprint(conn, "refine-1", _CONTRACT) == _expected_fingerprint()
    _assert_caller_transaction_is_intact(conn)
    assert conn.execute(
        "SELECT speed FROM pg_temp.fingerprint_source WHERE id = 99"
    ).fetchone() == (123.0,)


def test_postgres_input_fingerprint_detects_append_delete_and_empty_source(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    first = calculate_input_fingerprint(conn, _CONTRACT)
    conn.execute("INSERT INTO pg_temp.fingerprint_source SELECT 100, vehicle_id, timestamp, "
                 "lon, lat, heading, speed, extra FROM pg_temp.fingerprint_source WHERE id = 99")
    appended = calculate_input_fingerprint(conn, _CONTRACT, fetch_size=1)
    assert appended["row_count"] == 4
    assert appended["sha256"] != first["sha256"]
    conn.execute("DELETE FROM pg_temp.fingerprint_source WHERE id = 100")
    assert calculate_input_fingerprint(conn, _CONTRACT) == first
    _assert_caller_transaction_is_intact(conn)

    conn.execute("DELETE FROM pg_temp.fingerprint_source")
    empty = calculate_input_fingerprint(conn, _CONTRACT, fetch_size=2)
    assert empty["row_count"] == 0
    assert empty["sha256"] != first["sha256"]
    assert empty == calculate_input_fingerprint(conn, _CONTRACT, fetch_size=1)
    from psycopg.pq import TransactionStatus
    assert conn.info.transaction_status == TransactionStatus.INTRANS
    assert conn.execute("SELECT count(*) FROM pg_temp.kept_trajectory_points").fetchone() == (3,)


def test_postgres_input_text_keys_have_c_order_and_optional_extra(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    conn.execute("CREATE TEMP TABLE fingerprint_text_source ON COMMIT DROP AS "
                 "SELECT id::text AS id, vehicle_id, timestamp, lon, lat, heading, speed "
                 "FROM pg_temp.fingerprint_source")
    contract = replace(_CONTRACT, source_table="pg_temp.fingerprint_text_source",
                       quoted_table='"pg_temp"."fingerprint_text_source"',
                       key_sql_type="text", has_extra=False)
    conn.execute("UPDATE pg_temp.fingerprint_text_source SET id = 'ä' WHERE id = '99'")
    first = calculate_input_fingerprint(conn, contract, fetch_size=1)
    assert first == calculate_input_fingerprint(conn, contract, fetch_size=2)

    # Bind exact row order independently: 10, 2, then UTF-8 ä under C collation.
    records = (
        '{"key_sql_type":"text","scope":"full_input",'
        '"source_table":"pg_temp.fingerprint_text_source","version":2}',
        '{"extra":{},"heading":90.0,"lat":30.0,"lon":120.0,'
        '"source_row_key":"10","speed":2.0,"timestamp":10.0,"vehicle_id":"truck"}',
        '{"extra":{},"heading":null,"lat":31.0,"lon":121.5,'
        '"source_row_key":"2","speed":0.0,"timestamp":2.0,"vehicle_id":"车辆甲"}',
        '{"extra":{},"heading":0.0,"lat":20.0,"lon":100.0,'
        '"source_row_key":"ä","speed":1.0,"timestamp":99.0,"vehicle_id":"unreferenced"}',
    )
    digest = hashlib.sha256()
    for record in records:
        encoded = record.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    assert first["sha256"] == digest.hexdigest()
    _assert_caller_transaction_is_intact(conn)


def test_postgres_input_null_vehicle_cannot_collide_with_valid_text(
    fingerprint_connection: Any,
) -> None:
    conn = fingerprint_connection
    conn.execute("ALTER TABLE pg_temp.fingerprint_source ALTER COLUMN vehicle_id DROP NOT NULL")
    conn.execute("UPDATE pg_temp.fingerprint_source SET vehicle_id = NULL WHERE id = 99")
    null_fingerprint = calculate_input_fingerprint(conn, _CONTRACT, fetch_size=1)
    conn.execute("UPDATE pg_temp.fingerprint_source SET vehicle_id = 'None' WHERE id = 99")
    text_fingerprint = calculate_input_fingerprint(conn, _CONTRACT, fetch_size=1)
    assert null_fingerprint["sha256"] != text_fingerprint["sha256"]
    assert calculate_source_fingerprint(conn, "refine-1", _CONTRACT) == _expected_fingerprint()
    _assert_caller_transaction_is_intact(conn)
