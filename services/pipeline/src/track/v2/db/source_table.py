"""Validation and immutable-source fingerprints for schema-v3 source tables."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


_REQUIRED_COLUMNS = {
    "id",
    "vehicle_id",
    "timestamp",
    "lon",
    "lat",
    "heading",
    "speed",
    "geom",
}

_KEY_TYPES = {
    "int2": "smallint",
    "int4": "integer",
    "int8": "bigint",
    "uuid": "uuid",
    "text": "text",
    "varchar": "varchar",
}


@dataclass(frozen=True, slots=True)
class SourceTableContract:
    source_table: str
    quoted_table: str
    key_sql_type: str
    has_extra: bool


def quote_table_name(value: str) -> str:
    """Quote a validated optionally schema-qualified table identifier."""
    parts = value.split(".")
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None
        for part in parts
    ):
        raise ValueError(f"invalid source table name: {value!r}")
    return ".".join(f'"{part}"' for part in parts)


def validate_source_table(conn: Any, source_table: str) -> SourceTableContract:
    """Validate the v3 PostgreSQL source contract and return safe SQL metadata."""
    quoted = quote_table_name(source_table)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (source_table,))
        relation = cur.fetchone()[0]
        if relation is None:
            raise ValueError(f"source table does not exist: {source_table}")

        cur.execute(
            """
            SELECT attribute.attname, type.typname, attribute.attnotnull
              FROM pg_attribute AS attribute
              JOIN pg_type AS type ON type.oid = attribute.atttypid
             WHERE attribute.attrelid = %s::regclass
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
            """,
            (source_table,),
        )
        columns = {
            str(name): (str(type_name), bool(not_null))
            for name, type_name, not_null in cur.fetchall()
        }
        missing = sorted(_REQUIRED_COLUMNS - columns.keys())
        if missing:
            raise ValueError(
                f"source table {source_table} is missing columns: "
                + ", ".join(missing)
            )

        key_type, key_not_null = columns["id"]
        if key_type not in _KEY_TYPES:
            raise ValueError(
                f"source table id type {key_type!r} is unsupported; expected "
                "an integer family, UUID, TEXT, or VARCHAR"
            )
        if not key_not_null:
            raise ValueError("source table id must be NOT NULL")
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM pg_index AS index
                  JOIN pg_attribute AS attribute
                    ON attribute.attrelid = index.indrelid
                   AND attribute.attnum = index.indkey[0]
                 WHERE index.indrelid = %s::regclass
                   AND index.indisunique
                   AND index.indisvalid
                   AND index.indisready
                   AND index.indpred IS NULL
                   AND index.indexprs IS NULL
                   AND index.indnkeyatts = 1
                   AND attribute.attname = 'id'
            )
            """,
            (source_table,),
        )
        if not bool(cur.fetchone()[0]):
            raise ValueError("source table id must have a single-column UNIQUE index")

        if columns["geom"][0] != "geometry":
            raise ValueError("source table geom must be geometry(Point, 4326)")
        cur.execute(
            """
            SELECT postgis_typmod_type(attribute.atttypmod),
                   postgis_typmod_srid(attribute.atttypmod)
              FROM pg_attribute AS attribute
             WHERE attribute.attrelid = %s::regclass
               AND attribute.attname = 'geom'
            """,
            (source_table,),
        )
        geometry_type, srid = cur.fetchone()
        if str(geometry_type).upper() != "POINT" or int(srid) != 4326:
            raise ValueError("source table geom must be geometry(Point, 4326)")

    return SourceTableContract(
        source_table=source_table,
        quoted_table=quoted,
        key_sql_type=_KEY_TYPES[key_type],
        has_extra="extra" in columns,
    )


def source_order_query(contract: SourceTableContract) -> str:
    """Return the only supported Pipeline source scan order."""
    return (
        f"SELECT * FROM {contract.quoted_table} "
        "ORDER BY vehicle_id, timestamp, id::text"
    )


def calculate_source_fingerprint(
    conn: Any,
    clean_id: str,
    contract: SourceTableContract,
) -> dict[str, object]:
    """Hash exactly the source rows referenced by kept/excluded Clean output."""
    extra_sql = "source.extra" if contract.has_extra else "'{}'::jsonb"
    query = f"""
        WITH referenced(source_row_key) AS (
            SELECT source_row_key FROM kept_trajectory_points WHERE clean_id = %s
            UNION
            SELECT source_row_key FROM excluded_points WHERE clean_id = %s
        )
        SELECT referenced.source_row_key,
               source.id IS NOT NULL,
               source.vehicle_id,
               source.timestamp,
               source.lon,
               source.lat,
               source.heading,
               source.speed,
               {extra_sql}
          FROM referenced
          LEFT JOIN {contract.quoted_table} AS source
            ON source.id = referenced.source_row_key::{contract.key_sql_type}
         ORDER BY referenced.source_row_key
    """
    digest = hashlib.sha256()
    row_count = 0
    with conn.cursor() as cur:
        cur.execute(query, (clean_id, clean_id))
        for row in cur:
            if not bool(row[1]):
                raise ValueError(
                    f"Clean Run {clean_id} references missing source row {row[0]!r}"
                )
            canonical = {
                "source_row_key": str(row[0]),
                "vehicle_id": str(row[2]),
                "timestamp": row[3],
                "lon": row[4],
                "lat": row[5],
                "heading": row[6],
                "speed": row[7],
                "extra": row[8] if isinstance(row[8], dict) else {},
            }
            encoded = json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            row_count += 1
    return {
        "version": 1,
        "row_count": row_count,
        "sha256": digest.hexdigest(),
    }


def verify_clean_source_fingerprint(conn: Any, clean_id: str) -> None:
    """Reject replay when any referenced immutable source row has drifted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, source_fingerprint, status "
            "FROM clean_runs WHERE id = %s",
            (clean_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Clean Run: {clean_id}")
    if str(row[2]) != "completed":
        raise ValueError(f"Clean Run {clean_id} is not completed")
    expected = row[1] if isinstance(row[1], dict) else json.loads(str(row[1]))
    contract = validate_source_table(conn, str(row[0]))
    actual = calculate_source_fingerprint(conn, clean_id, contract)
    if actual != expected:
        raise ValueError(
            f"source rows for Clean Run {clean_id} changed; replay is refused"
        )
