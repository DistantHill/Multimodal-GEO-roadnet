"""Freeze the immutable inputs for PERF-WINDOWS-STRICT-001.

This helper deliberately has no project import: it can record the database and
deterministic dense-fixture fingerprints before the benchmark child imports the
pipeline.  It performs SELECT-only database access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEED = 20260810


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _write_manifest_once(output: Path, manifest: dict[str, Any]) -> None:
    """Create a gate manifest exactly once; never replace frozen evidence."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def dense_fixture() -> dict[str, Any]:
    """Return the deterministic, crossing-heavy fixture input description.

    Coordinates intentionally cover negative values, AABB edges, long and
    zero-length segments, and 80/100 degree heading boundaries.  Candidate
    equivalence is captured by the benchmark snapshot, not by this descriptor.
    """
    segments: list[dict[str, Any]] = []
    headings = (0.0, 80.0, 90.0, 100.0)
    for lane in range(96):
        offset = lane - 48
        segments.append({"vehicle": f"h{lane:03}", "a": [-250.0, float(offset)], "b": [250.0, float(offset)], "heading": headings[lane % 4]})
        segments.append({"vehicle": f"v{lane:03}", "a": [float(offset), -250.0], "b": [float(offset), 250.0], "heading": 90.0})
    segments.extend([
        {"vehicle": "edge", "a": [-250.0, -250.0], "b": [250.0, 250.0], "heading": 45.0},
        {"vehicle": "long", "a": [-1000.0, 0.0], "b": [1000.0, 0.0], "heading": 0.0},
        {"vehicle": "zero", "a": [0.0, 0.0], "b": [0.0, 0.0], "heading": 0.0},
    ])
    return {"seed": SEED, "origin": [0.0, 0.0], "segments": segments}


def db_fingerprint(env_file: Path, database: str) -> dict[str, Any]:
    import psycopg
    from psycopg import sql

    env = dict(
        line.split("=", 1)
        for line in (item.strip() for item in env_file.read_text(encoding="utf-8").splitlines())
        if line and not line.startswith("#") and "=" in line
    )
    with psycopg.connect(
        host=env["TRACK_PG_HOST"], port=env["TRACK_PG_PORT"], dbname=database,
        user=env["TRACK_PG_USER"], password=env["TRACK_PG_PASSWORD"],
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
        options="-c default_transaction_read_only=on",
    ) as conn, conn.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        if str(cur.fetchone()[0]).lower() != "on":
            raise RuntimeError("gate fingerprint connection is not read-only")
        cur.execute(
            "SELECT ordinal_position, column_name, data_type, udt_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY ordinal_position",
            ("trajectory_points",),
        )
        schema = [
            {
                "ordinal_position": int(row[0]),
                "column_name": str(row[1]),
                "data_type": str(row[2]),
                "udt_name": str(row[3]),
                "is_nullable": str(row[4]),
            }
            for row in cur
        ]
        columns = {item["column_name"] for item in schema}
        required = {"lon", "lat", "source_row_key"}
        if not required.issubset(columns):
            raise RuntimeError(
                f"trajectory_points missing frozen-key columns: {sorted(required - columns)}"
            )
        cur.execute(
            "SELECT count(*), min(lon), max(lon), min(lat), max(lat), "
            "md5(string_agg(source_row_key::text, ',' ORDER BY source_row_key::text)) "
            "FROM trajectory_points"
        )
        count, min_lon, max_lon, min_lat, max_lat, stable_id_checksum = cur.fetchone()
        full_digest = hashlib.sha256()
        query = sql.SQL(
            "SELECT to_jsonb(row_value)::text FROM {} AS row_value "
            "ORDER BY source_row_key::text"
        ).format(sql.Identifier("trajectory_points"))
        with conn.cursor(name="perf_windows_gate_full_hash") as stream:
            stream.itersize = 10_000
            stream.execute(query)
            for (payload,) in stream:
                full_digest.update(str(payload).encode("utf-8"))
                full_digest.update(b"\n")
    return {
        "available": True, "database": database, "table": "trajectory_points",
        "row_count": int(count),
        "extent": [float(min_lon), float(min_lat), float(max_lon), float(max_lat)],
        "stable_id_checksum": stable_id_checksum, "read_only_query": True,
        "schema": schema, "schema_sha256": _digest(schema),
        "full_table_sha256": full_digest.hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--database", default="track_test")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"frozen gate manifest already exists: {args.output}")
    fixture = dense_fixture()
    manifest: dict[str, Any] = {
        "gate": "windows_gate_v1", "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "dense_fixture": fixture, "dense_fixture_sha256": _digest(fixture),
        "config": {"spatial_window_size": [2000.0, 2000.0], "spatial_window_overlap": 0.15},
    }
    try:
        manifest["real_db"] = db_fingerprint(args.env_file, args.database)
    except Exception as exc:  # Manifest must record an unavailable DB, never substitute one.
        manifest["real_db"] = {"available": False, "error_type": type(exc).__name__, "error": str(exc)}
    manifest["input_snapshot_sha256"] = _digest({k: v for k, v in manifest.items() if k != "frozen_at_utc"})
    _write_manifest_once(args.output, manifest)
    print(args.output)
    print("INPUT_SNAPSHOT_SHA256", manifest["input_snapshot_sha256"])
    print("REAL_DB_AVAILABLE", manifest["real_db"]["available"])


if __name__ == "__main__":
    main()
