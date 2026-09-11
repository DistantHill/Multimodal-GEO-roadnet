from __future__ import annotations

import pytest

from track.v2.db.source_table import (
    SourceTableContract,
    quote_table_name,
    source_order_query,
)


def test_source_table_name_is_schema_qualified_and_safely_quoted() -> None:
    assert quote_table_name("public.day01") == '"public"."day01"'
    assert quote_table_name("trajectory_points") == '"trajectory_points"'


@pytest.mark.parametrize(
    "value",
    ("", "public.", "public.day-01", "public.day01;DROP TABLE clean_runs"),
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
