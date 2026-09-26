"""Validation and immutable-source fingerprints for schema-v4 source tables."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


from ..heartbeat import observed_operation, heartbeat_progress

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
                "smallint, integer, or bigint"
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


def _fingerprint_encode(value: object) -> bytes:
    """Keep the original v1 JSON representation, including its extra fallback."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _fingerprint_row(row: tuple[Any, ...], *, preserve_vehicle_null: bool = False) -> bytes:
    return _fingerprint_encode({
        "source_row_key": str(row[0]),
        "vehicle_id": None if preserve_vehicle_null and row[2] is None else str(row[2]),
        "timestamp": row[3],
        "lon": row[4],
        "lat": row[5],
        "heading": row[6],
        "speed": row[7],
        "extra": row[8] if isinstance(row[8], dict) else {},
    })


@observed_operation("计算输入数据指纹")
def calculate_input_fingerprint(
    conn: Any,
    contract: SourceTableContract,
    *,
    fetch_size: int = 10_000,
) -> dict[str, object]:
    """Hash all source rows, including rows not referenced by any Refine Run.

    Version 2 with scope ``full_input`` is separate from the referenced-row v1
    digest. Its domain header binds the table name, key type, version and scope.
    Rows use the v1 canonical fields (id, vehicle_id, timestamp, lon, lat, heading,
    speed and optional extra), except SQL NULL vehicle IDs remain JSON null rather
    than colliding with the literal string "None". This is not a digest of geom
    or arbitrary columns.
    Text-cast keys use explicit PostgreSQL C collation, independent of the default
    locale. Equal content under a different source identifier is a different input.

    The caller supplies a validated contract and owns the transaction; no commit,
    rollback, source lock or immutable snapshot is imposed. The source must stay
    sealed between fingerprinting and computation. The fetch bound counts rows,
    not bytes or PostgreSQL execution memory, and each row is encoded separately.
    """
    if type(fetch_size) is not int or fetch_size <= 0:
        raise ValueError("fetch_size must be a positive integer")
    metadata: dict[str, object] = {
        "version": 2,
        "scope": "full_input",
        "source_table": contract.source_table,
        "key_sql_type": contract.key_sql_type,
    }
    header = _fingerprint_encode(metadata)
    digest = hashlib.sha256()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    extra_sql = "source.extra" if contract.has_extra else "'{}'::jsonb"
    query = f"""
        SELECT source.id::text,
               source.id IS NOT NULL,
               source.vehicle_id,
               source.timestamp,
               source.lon,
               source.lat,
               source.heading,
               source.speed,
               {extra_sql}
          FROM {contract.quoted_table} AS source
         ORDER BY source.id::text COLLATE "C"
    """
    row_count = 0
    with conn.cursor(name=f"input_fingerprint_{uuid4().hex}") as cur:
        cur.itersize = fetch_size
        cur.execute(query, ())
        for row in cur:
            encoded = _fingerprint_row(row, preserve_vehicle_null=True)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            row_count += 1
            if row_count % fetch_size == 0:
                heartbeat_progress(row_count)
    return {**metadata, "row_count": row_count, "sha256": digest.hexdigest()}


@observed_operation("校验源数据指纹")
def calculate_source_fingerprint(
    conn: Any,
    refine_id: str,
    contract: SourceTableContract,
    *,
    fetch_size: int = 10_000,
) -> dict[str, object]:
    """Hash referenced Refine source rows using bounded server-side reads.

    This is not a fingerprint of the entire input table. The caller owns the
    transaction, which must support a non-holdable named cursor; this function
    neither commits nor rolls back. ``fetch_size`` bounds rows per fetch, not
    bytes per row or PostgreSQL's query execution memory.
    """
    if type(fetch_size) is not int or fetch_size <= 0:
        raise ValueError("fetch_size must be a positive integer")
    extra_sql = "source.extra" if contract.has_extra else "'{}'::jsonb"
    query = f"""
        WITH referenced(source_row_key) AS (
            SELECT source_row_key FROM kept_trajectory_points WHERE refine_id = %s
            UNION
            SELECT source_row_key FROM excluded_points WHERE refine_id = %s
        )
        SELECT referenced.source_row_key::text,
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
            ON source.id = referenced.source_row_key
         ORDER BY referenced.source_row_key::text
    """
    digest = hashlib.sha256()
    row_count = 0
    with conn.cursor(name=f"source_fingerprint_{uuid4().hex}") as cur:
        cur.itersize = fetch_size
        cur.execute(query, (refine_id, refine_id))
        for row in cur:
            if not bool(row[1]):
                raise ValueError(
                    f"Refine Run {refine_id} references missing source row {row[0]!r}"
                )
            encoded = _fingerprint_row(row)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            row_count += 1
            if row_count % fetch_size == 0:
                heartbeat_progress(row_count)
    return {
        "version": 1,
        "row_count": row_count,
        "sha256": digest.hexdigest(),
    }


def verify_refine_source_fingerprint(conn: Any, refine_id: str) -> None:
    """Reject replay when any referenced immutable source row has drifted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_table, source_fingerprint, status "
            "FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Refine Run: {refine_id}")
    if str(row[2]) != "completed":
        raise ValueError(f"Refine Run {refine_id} is not completed")
    expected = row[1] if isinstance(row[1], dict) else json.loads(str(row[1]))
    contract = validate_source_table(conn, str(row[0]))
    actual = calculate_source_fingerprint(conn, refine_id, contract)
    if actual != expected:
        raise ValueError(
            f"source rows for Refine Run {refine_id} changed; replay is refused"
        )
