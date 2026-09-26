from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from itertools import islice
from typing import Any

import pytest

from track.v2.db import source_table
from track.v2.db.source_table import (
    SourceTableContract,
    calculate_input_fingerprint,
    calculate_source_fingerprint,
    quote_table_name,
    source_order_query,
)


def test_source_table_ids_are_limited_to_integer_types() -> None:
    assert set(source_table._KEY_TYPES) == {"int2", "int4", "int8"}


def test_source_table_name_is_schema_qualified_and_safely_quoted() -> None:
    assert quote_table_name("public.day01") == '"public"."day01"'
    assert quote_table_name("trajectory_points") == '"trajectory_points"'


@pytest.mark.parametrize(
    "value",
    ("", "public.", "public.day-01", "public.day01;DROP TABLE refine_runs"),
)
def test_source_table_name_rejects_non_identifiers(value: str) -> None:
    with pytest.raises(ValueError, match="invalid source table name"):
        quote_table_name(value)


def test_source_scan_uses_text_key_as_deterministic_tie_breaker() -> None:
    contract = SourceTableContract(
        source_table="public.day01",
        quoted_table='"public"."day01"',
        key_sql_type="bigint",
        has_extra=True,
    )
    assert source_order_query(contract) == (
        'SELECT * FROM "public"."day01" '
        "ORDER BY vehicle_id, timestamp, id::text"
    )


class _StreamingCursor:
    """Require named, bounded iteration; never permit a client-side result."""

    def __init__(self, rows: Iterable[tuple[Any, ...]]) -> None:
        self.rows = iter(rows)
        self.itersize = 0
        self.closed = False
        self.query = ""
        self.params: tuple[str, ...] = ()
        self.batch_sizes: list[int] = []
        self.execute_error: Exception | None = None
        self.connection = None
        self.planned_fraction = None

    def __enter__(self) -> _StreamingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def execute(self, query: str, params: tuple[str, ...]) -> None:
        assert self.itersize > 0, "set the fetch bound before executing"
        self.query = query
        self.params = params
        self.planned_fraction = self.connection.fraction
        if self.execute_error is not None:
            raise self.execute_error

    def fetchall(self) -> None:
        raise AssertionError("fingerprinting must not materialize the result")

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        assert self.itersize > 0
        while batch := tuple(islice(self.rows, self.itersize)):
            self.batch_sizes.append(len(batch))
            yield from batch


class _StreamingConnection:
    def __init__(self, rows: Iterable[tuple[Any, ...]]) -> None:
        self.stream = _StreamingCursor(rows)
        self.stream.connection = self
        self.fraction = "0.37"
        self.names: list[str] = []

    def cursor(self, *, name: str | None = None):
        if name is None:
            return _SettingCursor(self)
        assert name, "fingerprinting must use a server-side named cursor"
        self.names.append(name)
        return self.stream

    @contextmanager
    def transaction(self):
        previous = self.fraction
        try:
            yield
        except BaseException:
            self.fraction = previous
            raise

    def commit(self) -> None:
        raise AssertionError("the caller owns the transaction")

    def rollback(self) -> None:
        raise AssertionError("the caller owns the transaction")


class _SettingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=()):
        if query.startswith("SET LOCAL cursor_tuple_fraction"):
            self.connection.fraction = "1"
        elif "set_config" in query:
            self.connection.fraction = params[0]
        else:
            assert query == "SELECT current_setting('cursor_tuple_fraction')"

    def fetchone(self):
        return (self.connection.fraction,)


def _contract(*, has_extra: bool = True) -> SourceTableContract:
    return SourceTableContract("public.day01", '"public"."day01"', "bigint", has_extra)


def _rows() -> list[tuple[Any, ...]]:
    return [
        (
            "10", True, "车辆甲", datetime(2026, 9, 13, tzinfo=timezone.utc),
            Decimal("121.50"), 31.0, None, 0.0, {"z": 1, "a": "中文"},
        ),
        ("2", True, 42, None, -0.0, 32.5, 180.0, 1, None),
        ("3", True, "truck", "t", 120, 30, 90, 2, ["not", "a", "dict"]),
    ]


def _legacy_expected() -> dict[str, object]:
    # Literal canonical v1 records lock encoding/order independently of the new
    # cursor implementation, including Unicode, decimals and non-dict extras.
    canonical_rows = (
        '{"extra":{"a":"中文","z":1},"heading":null,"lat":31.0,'
        '"lon":"121.50","source_row_key":"10","speed":0.0,'
        '"timestamp":"2026-09-13 00:00:00+00:00","vehicle_id":"车辆甲"}',
        '{"extra":{},"heading":180.0,"lat":32.5,"lon":-0.0,'
        '"source_row_key":"2","speed":1,"timestamp":null,"vehicle_id":"42"}',
        '{"extra":{},"heading":90,"lat":30,"lon":120,"source_row_key":"3",'
        '"speed":2,"timestamp":"t","vehicle_id":"truck"}',
    )
    digest = hashlib.sha256()
    for canonical in canonical_rows:
        encoded = canonical.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {"version": 1, "row_count": 3, "sha256": digest.hexdigest()}


@pytest.mark.parametrize("fetch_size", (1, 2, 10_000))
def test_fingerprint_preserves_v1_digest_with_bounded_named_cursor(fetch_size: int) -> None:
    conn = _StreamingConnection(_rows())

    actual = calculate_source_fingerprint(conn, "refine-1", _contract(), fetch_size=fetch_size)

    assert actual == _legacy_expected()
    assert conn.stream.closed
    assert conn.fraction == "0.37"
    assert max(conn.stream.batch_sizes) <= fetch_size
    assert sum(conn.stream.batch_sizes) == 3
    assert conn.stream.params == ("refine-1", "refine-1")
    query = " ".join(conn.stream.query.split())
    assert "SELECT source_row_key FROM kept_trajectory_points WHERE refine_id = %s UNION" in query
    assert "SELECT source_row_key FROM excluded_points WHERE refine_id = %s" in query
    assert 'LEFT JOIN "public"."day01" AS source' in query
    assert "source.id = referenced.source_row_key" in query
    assert query.endswith("ORDER BY referenced.source_row_key::text")


def test_fingerprint_defaults_to_bounded_fetch_and_unique_cursor_names() -> None:
    conn = _StreamingConnection([])
    first = calculate_source_fingerprint(conn, "refine-1", _contract())
    second = calculate_source_fingerprint(conn, "refine-1", _contract())
    assert first == second == {
        "version": 1, "row_count": 0, "sha256": hashlib.sha256().hexdigest(),
    }
    assert conn.stream.itersize == 10_000
    assert len(set(conn.names)) == 2
    assert all(name.startswith("source_fingerprint_") and len(name) <= 63 for name in conn.names)


def test_fingerprint_hashes_each_batch_before_reading_more(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_rows = 0
    fetch_size = 7
    original_dumps = source_table.json.dumps

    def counted_dumps(*args: Any, **kwargs: Any) -> str:
        nonlocal encoded_rows
        encoded_rows += 1
        return original_dumps(*args, **kwargs)

    def guarded_rows() -> Iterator[tuple[Any, ...]]:
        for index in range(103):
            assert index - encoded_rows < fetch_size, "rows were materialized before hashing"
            yield (str(index), True, "truck", "t", 1, 2, 3, 4, {})

    monkeypatch.setattr(source_table.json, "dumps", counted_dumps)
    conn = _StreamingConnection(guarded_rows())
    actual = calculate_source_fingerprint(conn, "refine-1", _contract(), fetch_size=fetch_size)
    assert actual["row_count"] == encoded_rows == 103
    assert max(conn.stream.batch_sizes) == fetch_size


@pytest.mark.parametrize("has_extra", (True, False))
def test_fingerprint_keeps_optional_extra_projection(has_extra: bool) -> None:
    conn = _StreamingConnection([])
    calculate_source_fingerprint(conn, "refine-1", _contract(has_extra=has_extra))
    assert ("source.extra" in conn.stream.query) is has_extra
    assert ("'{}'::jsonb" in conn.stream.query) is not has_extra


def test_fingerprint_missing_source_row_closes_cursor() -> None:
    conn = _StreamingConnection([("gone", False)])
    with pytest.raises(ValueError, match="Refine Run refine-1 references missing source row 'gone'"):
        calculate_source_fingerprint(conn, "refine-1", _contract())
    assert conn.stream.closed
    assert conn.fraction == "0.37"


@pytest.mark.parametrize("failure_at", ("execute", "fetch", "encode"))
def test_fingerprint_failure_closes_cursor_without_transaction_changes(failure_at: str) -> None:
    def failing_rows() -> Iterator[tuple[Any, ...]]:
        yield _rows()[0]
        raise RuntimeError("fetch failed")

    class Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("encode failed")

    if failure_at == "fetch":
        rows = failing_rows()
    elif failure_at == "encode":
        rows = iter([("1", True, "truck", Unserializable(), 1, 2, 3, 4, {})])
    else:
        rows = iter([])
    conn = _StreamingConnection(rows)
    if failure_at == "execute":
        conn.stream.execute_error = RuntimeError("execute failed")
    with pytest.raises(RuntimeError, match=f"{failure_at} failed"):
        calculate_source_fingerprint(conn, "refine-1", _contract(), fetch_size=1)
    assert conn.stream.closed
    assert conn.fraction == "0.37"


@pytest.mark.parametrize("fetch_size", (0, -1, True, False, 1.5, "10", None))
def test_fingerprint_rejects_invalid_fetch_size_before_opening_cursor(fetch_size: Any) -> None:
    conn = _StreamingConnection([])
    with pytest.raises(ValueError, match="fetch_size must be a positive integer"):
        calculate_source_fingerprint(conn, "refine-1", _contract(), fetch_size=fetch_size)
    assert not conn.names


def _input_expected() -> dict[str, object]:
    # Independent literal header + records; never derive expected bytes by
    # invoking the implementation's shared canonical encoder.
    records = (
        '{"key_sql_type":"bigint","scope":"full_input",'
        '"source_table":"public.day01","version":2}',
        '{"extra":{"a":"中文","z":1},"heading":null,"lat":31.0,'
        '"lon":"121.50","source_row_key":"10","speed":0.0,'
        '"timestamp":"2026-09-13 00:00:00+00:00","vehicle_id":"车辆甲"}',
        '{"extra":{},"heading":180.0,"lat":32.5,"lon":-0.0,'
        '"source_row_key":"2","speed":1,"timestamp":null,"vehicle_id":"42"}',
        '{"extra":{},"heading":90,"lat":30,"lon":120,"source_row_key":"3",'
        '"speed":2,"timestamp":"t","vehicle_id":"truck"}',
    )
    digest = hashlib.sha256()
    for record in records:
        encoded = record.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {
        "version": 2, "scope": "full_input", "source_table": "public.day01",
        "key_sql_type": "bigint", "row_count": 3, "sha256": digest.hexdigest(),
    }


@pytest.mark.parametrize("fetch_size", (1, 2, 10_000))
def test_input_fingerprint_has_separate_scope_header_and_explicit_c_order(fetch_size: int) -> None:
    conn = _StreamingConnection(_rows())
    actual = calculate_input_fingerprint(conn, _contract(), fetch_size=fetch_size)
    assert actual == _input_expected()
    assert actual["sha256"] != _legacy_expected()["sha256"]
    assert conn.stream.closed
    assert conn.fraction == "0.37"
    assert max(conn.stream.batch_sizes) <= fetch_size
    assert sum(conn.stream.batch_sizes) == 3
    query = " ".join(conn.stream.query.split())
    assert 'FROM "public"."day01" AS source' in query
    assert query.endswith('ORDER BY source.id::text COLLATE "C"')
    assert "kept_trajectory_points" not in query
    assert "excluded_points" not in query
    assert "JOIN" not in query
    assert "WHERE" not in query
    assert conn.stream.params == ()


def test_empty_input_is_valid_and_cursor_names_are_unique() -> None:
    conn = _StreamingConnection([])
    first = calculate_input_fingerprint(conn, _contract())
    assert calculate_input_fingerprint(conn, _contract()) == first
    assert first["row_count"] == 0
    assert first["sha256"] != hashlib.sha256().hexdigest()
    assert conn.stream.itersize == 10_000
    assert len(set(conn.names)) == 2
    assert all(name.startswith("input_fingerprint_") and len(name) <= 63 for name in conn.names)


def test_input_identity_and_key_type_are_bound_into_digest() -> None:
    contracts = [
        _contract(),
        SourceTableContract("public.other", '"public"."other"', "bigint", True),
        SourceTableContract("public.day01", '"public"."day01"', "text", True),
    ]
    fingerprints = [calculate_input_fingerprint(_StreamingConnection(_rows()), contract)
                    for contract in contracts]
    assert len({fingerprint["sha256"] for fingerprint in fingerprints}) == 3


def test_input_hashes_each_batch_before_reading_more(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_rows = 0
    original_encode = source_table._fingerprint_row

    def counted_encode(row: tuple[Any, ...], **kwargs: Any) -> bytes:
        nonlocal encoded_rows
        encoded_rows += 1
        return original_encode(row, **kwargs)

    def guarded_rows() -> Iterator[tuple[Any, ...]]:
        for index in range(103):
            assert index - encoded_rows < 7
            yield (str(index), True, "truck", "t", 1, 2, 3, 4, {})

    monkeypatch.setattr(source_table, "_fingerprint_row", counted_encode)
    conn = _StreamingConnection(guarded_rows())
    assert calculate_input_fingerprint(conn, _contract(), fetch_size=7)["row_count"] == 103
    assert encoded_rows == 103


@pytest.mark.parametrize("has_extra", (True, False))
def test_input_fingerprint_optional_extra_defaults_to_existing_representation(has_extra: bool) -> None:
    conn = _StreamingConnection([])
    calculate_input_fingerprint(conn, _contract(has_extra=has_extra))
    assert ("source.extra" in conn.stream.query) is has_extra
    assert ("'{}'::jsonb" in conn.stream.query) is not has_extra


@pytest.mark.parametrize("failure_at", ("execute", "fetch", "encode"))
def test_input_failure_closes_cursor_and_preserves_caller_transaction(failure_at: str) -> None:
    def failing_rows() -> Iterator[tuple[Any, ...]]:
        yield _rows()[0]
        raise RuntimeError("fetch failed")

    class Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("encode failed")

    rows = failing_rows() if failure_at == "fetch" else iter([])
    if failure_at == "encode":
        rows = iter([("1", True, "truck", Unserializable(), 1, 2, 3, 4, {})])
    conn = _StreamingConnection(rows)
    if failure_at == "execute":
        conn.stream.execute_error = RuntimeError("execute failed")
    with pytest.raises(RuntimeError, match=f"{failure_at} failed"):
        calculate_input_fingerprint(conn, _contract(), fetch_size=1)
    assert conn.stream.closed
    assert conn.fraction == "0.37"


@pytest.mark.parametrize("fetch_size", (0, -1, True, False, 1.5, "10", None))
def test_input_rejects_invalid_fetch_size_without_opening_cursor(fetch_size: Any) -> None:
    conn = _StreamingConnection([])
    with pytest.raises(ValueError, match="fetch_size must be a positive integer"):
        calculate_input_fingerprint(conn, _contract(), fetch_size=fetch_size)
    assert not conn.names


def test_input_distinguishes_null_vehicle_from_literal_none_without_changing_v1() -> None:
    null_row = ("1", True, None, 1.0, 120.0, 30.0, 0.0, 1.0, {})
    text_row = ("1", True, "None", 1.0, 120.0, 30.0, 0.0, 1.0, {})
    null_fingerprint = calculate_input_fingerprint(_StreamingConnection([null_row]), _contract())
    text_fingerprint = calculate_input_fingerprint(_StreamingConnection([text_row]), _contract())
    assert null_fingerprint["sha256"] != text_fingerprint["sha256"]
    # Preserve the old replay fingerprint's representation, even its known
    # normalization; the new scope/version is the corrected recovery boundary.
    assert calculate_source_fingerprint(_StreamingConnection([null_row]), "refine", _contract()) == (
        calculate_source_fingerprint(_StreamingConnection([text_row]), "refine", _contract()))


def test_both_fingerprints_preserve_callers_cursor_planning():
    referenced = _StreamingConnection(_rows())
    calculate_source_fingerprint(referenced, "refine-1", _contract())
    assert referenced.stream.planned_fraction == "0.37"
    assert referenced.fraction == "0.37"
    full_input = _StreamingConnection(_rows())
    calculate_input_fingerprint(full_input, _contract())
    assert full_input.stream.planned_fraction == "0.37"
    assert full_input.fraction == "0.37"
