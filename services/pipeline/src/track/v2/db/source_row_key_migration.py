"""Transactional track.v2 schema v7 to v8 source-key type migration."""

from __future__ import annotations

from typing import Any

from .schema import SCHEMA_COMPONENT
from .source_table import quote_table_name


_TARGET_VERSION = 8
_PREVIOUS_VERSION = 7
_TABLES = ("excluded_points", "kept_trajectory_points")
_INTEGER_ID_TYPES = {"int2", "int4", "int8"}


def _invalid_key_count_sql(table: str) -> str:
    """Build a static preflight query for canonical signed PostgreSQL BIGINT values."""
    if table not in _TABLES:
        raise ValueError(f"unsupported source-key table: {table}")
    return f"""
        WITH normalized AS (
            SELECT
                CASE WHEN source_row_key ~ '^(0|[1-9][0-9]*|-[1-9][0-9]*)$'
                     THEN COALESCE(
                         NULLIF(ltrim(regexp_replace(source_row_key, '^-', ''), '0'), ''),
                         '0'
                     )
                     ELSE NULL
                END AS magnitude,
                left(source_row_key, 1) = '-' AS is_negative
              FROM {table}
        )
        SELECT count(*)
          FROM normalized
         WHERE magnitude IS NULL
            OR length(magnitude) > 19
            OR (
                length(magnitude) = 19
                AND magnitude COLLATE "C" >
                    (CASE WHEN is_negative
                          THEN '9223372036854775808'
                          ELSE '9223372036854775807'
                     END) COLLATE "C"
            )
    """


def _validate_run_source_id_types(cur: Any) -> None:
    """Ensure retained Runs still meet the v8 integer source contract."""
    cur.execute(
        "SELECT DISTINCT source_table FROM refine_runs ORDER BY source_table"
    )
    source_tables = [str(row[0]) for row in cur.fetchall()]
    for source_table in source_tables:
        try:
            quote_table_name(source_table)
        except ValueError as exc:
            raise RuntimeError(
                "source row key migration found a Refine Run with an invalid source table"
            ) from exc
        cur.execute(
            """
            SELECT type.typname
              FROM pg_class AS relation
              JOIN pg_attribute AS attribute
                ON attribute.attrelid = relation.oid
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
              JOIN pg_type AS type ON type.oid = attribute.atttypid
             WHERE relation.oid = to_regclass(%s)
               AND attribute.attname = 'id'
            """,
            (source_table,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                "source row key migration found a Refine Run whose source table "
                "is missing or has no id column"
            )
        key_type = str(row[0])
        if key_type not in _INTEGER_ID_TYPES:
            raise RuntimeError(
                "source row key migration requires integer-family source ids; "
                f"found {key_type!r} for a retained Refine Run"
            )


def migrate_source_row_key_v8(cur: Any) -> None:
    """Validate and convert Refine source keys, publishing v8 last.

    The caller owns the transaction. The version registry and all data that
    defines source-key compatibility are locked so no writer can add a value
    or Run between validation and ALTER.
    """
    cur.execute("SET LOCAL lock_timeout = '5s'")
    cur.execute("LOCK TABLE pipeline_schema_versions IN ACCESS EXCLUSIVE MODE")
    cur.execute(
        "SELECT version FROM pipeline_schema_versions WHERE component = %s",
        (SCHEMA_COMPONENT,),
    )
    row = cur.fetchone()
    if row is not None and int(row[0]) == _TARGET_VERSION:
        return
    if row is None or int(row[0]) != _PREVIOUS_VERSION:
        raise RuntimeError("source row key migration requires an existing schema v7 database")

    cur.execute("LOCK TABLE refine_runs IN SHARE MODE")
    cur.execute(
        "LOCK TABLE excluded_points, kept_trajectory_points IN ACCESS EXCLUSIVE MODE"
    )
    _validate_run_source_id_types(cur)

    for table in _TABLES:
        cur.execute(_invalid_key_count_sql(table))
        invalid_count = int(cur.fetchone()[0])
        if invalid_count:
            raise RuntimeError(
                f"cannot migrate {table}.source_row_key to BIGINT: "
                f"{invalid_count} value(s) are not canonical signed 64-bit integers"
            )

    for table in _TABLES:
        cur.execute(
            f"ALTER TABLE {table} ALTER COLUMN source_row_key TYPE BIGINT "
            "USING source_row_key::BIGINT"
        )
    cur.execute(
        "UPDATE pipeline_schema_versions SET version = %s WHERE component = %s",
        (_TARGET_VERSION, SCHEMA_COMPONENT),
    )
