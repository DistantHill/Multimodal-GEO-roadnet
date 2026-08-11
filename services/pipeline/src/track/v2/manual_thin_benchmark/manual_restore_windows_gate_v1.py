"""Safely restore the frozen Windows Gate v1 source selection.

The default mode is deliberately read-only.  ``--apply`` is an explicit,
destructive opt-in: it takes a verified full ``track_test`` backup before it
changes the target, then replaces the target in one PostgreSQL transaction.

The source selection is intentionally identical to ``manual_bench_seed_1m``:
``postgres.public.day01 ORDER BY vehicle_id, timestamp LIMIT 1000000``.
No connection string, password, or environment contents are included in the
JSON audit artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


BASELINE_COMMIT = "c12a06abe3262501f5bb20713c4280aa9a99f34e"
SOURCE_DATABASE = "postgres"
SOURCE_SCHEMA = "public"
SOURCE_TABLE = "day01"
TARGET_DATABASE = "track_test"
TARGET_SCHEMA = "public"
TARGET_TABLE = "trajectory_points"
ROW_LIMIT = 1_000_000
ADVISORY_LOCK_KEY = 0x57494E31  # "WIN1"

COPY_COLUMNS = (
    "source_id",
    "source_row_key",
    "stable",
    "is_active",
    "vehicle_id",
    "timestamp",
    "lon",
    "lat",
    "heading",
    "speed",
    "extra",
)
REQUIRED_TARGET_COLUMNS = ("source_id", "source_row_key", "stable")


@dataclass(frozen=True)
class Fingerprint:
    row_count: int | None
    extent: list[float | None] | None
    source_row_key_checksum: str | None
    stable_true_count: int | None
    columns: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_env(path: Path) -> dict[str, str]:
    """Load the project DB settings without ever logging their values."""
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    required = {"TRACK_PG_HOST", "TRACK_PG_PORT", "TRACK_PG_USER", "TRACK_PG_PASSWORD"}
    missing = sorted(required - set(result))
    if missing:
        raise RuntimeError(f"environment file misses required PostgreSQL keys: {missing}")
    return result


def _connect(env: dict[str, str], database: str, *, read_only: bool):
    import psycopg

    options = "-c default_transaction_read_only=on" if read_only else None
    return psycopg.connect(
        host=env["TRACK_PG_HOST"],
        port=env["TRACK_PG_PORT"],
        dbname=database,
        user="postgres",
        password=env["TRACK_PG_PASSWORD"],
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
        options=options,
    )


def _table_columns(conn: Any, schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        return [str(row[0]) for row in cur]


def _fingerprint(conn: Any, schema: str, table: str) -> Fingerprint:
    """Return the exact legacy fingerprint, or an explicit missing-key shape."""
    from psycopg import sql

    columns = _table_columns(conn, schema, table)
    if not {"lon", "lat"}.issubset(columns):
        return Fingerprint(None, None, None, None, columns)
    identifier = sql.Identifier(schema, table)
    with conn.cursor() as cur:
        if "source_row_key" not in columns:
            cur.execute(
                sql.SQL("SELECT count(*), min(lon), min(lat), max(lon), max(lat) FROM {}")
                .format(identifier)
            )
            count, min_lon, min_lat, max_lon, max_lat = cur.fetchone()
            return Fingerprint(
                int(count),
                [float(min_lon), float(min_lat), float(max_lon), float(max_lat)],
                None,
                None,
                columns,
            )
        stable_sql = sql.SQL("sum(CASE WHEN stable THEN 1 ELSE 0 END)") if "stable" in columns else sql.SQL("NULL")
        cur.execute(
            sql.SQL(
                "SELECT count(*), min(lon), min(lat), max(lon), max(lat), "
                "md5(string_agg(source_row_key::text, ',' ORDER BY source_row_key::text)), {} "
                "FROM {}"
            ).format(stable_sql, identifier)
        )
        count, min_lon, min_lat, max_lon, max_lat, checksum, stable_true = cur.fetchone()
    return Fingerprint(
        int(count),
        [float(min_lon), float(min_lat), float(max_lon), float(max_lat)],
        str(checksum) if checksum is not None else None,
        int(stable_true) if stable_true is not None else None,
        columns,
    )


def _expected_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    real = manifest.get("real_db")
    if not isinstance(real, dict):
        raise RuntimeError("frozen manifest lacks real_db")
    required = ("row_count", "extent", "stable_id_checksum", "database", "table")
    missing = [key for key in required if key not in real]
    if missing:
        raise RuntimeError(f"frozen manifest lacks required real_db fields: {missing}")
    if real["database"] != TARGET_DATABASE or real["table"] != TARGET_TABLE:
        raise RuntimeError("frozen manifest does not describe the Windows Gate v1 target")
    return real


def _matches_expected(fingerprint: Fingerprint, expected: dict[str, Any]) -> bool:
    return (
        fingerprint.row_count == expected["row_count"]
        and fingerprint.extent == expected["extent"]
        and fingerprint.source_row_key_checksum == expected["stable_id_checksum"]
        and fingerprint.stable_true_count == expected["row_count"]
        and set(REQUIRED_TARGET_COLUMNS).issubset(fingerprint.columns)
    )


def _source_rows(conn: Any) -> Iterator[tuple[Any, ...]]:
    """Yield the frozen source selection without materializing it in Python."""
    from psycopg import sql

    columns = sql.SQL(", ").join(sql.Identifier(column) for column in COPY_COLUMNS)
    query = sql.SQL(
        "SELECT {} FROM {}.{} ORDER BY vehicle_id, timestamp LIMIT %s"
    ).format(columns, sql.Identifier(SOURCE_SCHEMA), sql.Identifier(SOURCE_TABLE))
    with conn.cursor(name="windows_gate_v1_source") as cur:
        cur.itersize = 10_000
        cur.execute(query, (ROW_LIMIT,))
        yield from cur


def _jsonb_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Adapt JSONB through psycopg, never by hand-built COPY text."""
    from psycopg.types.json import Jsonb

    values = list(row)
    values[-1] = Jsonb(values[-1])
    return tuple(values)


def _ensure_target_columns(cur: Any) -> None:
    cur.execute("ALTER TABLE public.trajectory_points ADD COLUMN IF NOT EXISTS source_id TEXT")
    cur.execute("ALTER TABLE public.trajectory_points ADD COLUMN IF NOT EXISTS source_row_key TEXT")
    cur.execute("ALTER TABLE public.trajectory_points ADD COLUMN IF NOT EXISTS stable BOOLEAN")


def _ensure_unique_source_constraint(cur: Any) -> None:
    cur.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'public.trajectory_points'::regclass "
        "AND contype IN ('p', 'u') AND pg_get_constraintdef(oid) "
        "IN ('PRIMARY KEY (source_id, source_row_key)', 'UNIQUE (source_id, source_row_key)')"
    )
    if cur.fetchone() is None:
        cur.execute(
            "ALTER TABLE public.trajectory_points "
            "ADD CONSTRAINT uq_trajectory_points_source_ref UNIQUE (source_id, source_row_key)"
        )


def _restore_sequence(cur: Any) -> None:
    cur.execute(
        "SELECT setval(pg_get_serial_sequence('public.trajectory_points', 'id'), "
        "COALESCE((SELECT max(id) FROM public.trajectory_points), 1), true)"
    )


def _backup_target(env: dict[str, str], output: Path) -> dict[str, Any]:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("pg_dump is required for --apply and was not found on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing backup: {output}")
    command = [
        pg_dump,
        "-Fc",
        "-h", env["TRACK_PG_HOST"],
        "-p", env["TRACK_PG_PORT"],
        "-U", "postgres",
        "-f", str(output),
        TARGET_DATABASE,
    ]
    dump_env = os.environ.copy()
    dump_env["PGPASSWORD"] = env["TRACK_PG_PASSWORD"]
    completed = subprocess.run(command, env=dump_env, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError("pg_dump failed before target mutation")
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("pg_dump reported success but no non-empty backup was created")
    digest_state = hashlib.sha256()
    with output.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest_state.update(block)
    digest = digest_state.hexdigest()
    return {"path": str(output), "sha256": digest, "bytes": output.stat().st_size}


def _apply_restore(
    env: dict[str, str], expected: dict[str, Any], backup_path: Path, audit: dict[str, Any],
) -> tuple[Fingerprint, str | None]:
    """Back up and replace target while one transaction holds the write fence.

    ``SHARE`` blocks writers but permits pg_dump's ``ACCESS SHARE`` readers.
    The transaction retains that fence while the external backup runs, then
    upgrades to ``ACCESS EXCLUSIVE`` before any target mutation.
    """
    with _connect(env, SOURCE_DATABASE, read_only=True) as source_conn:
        source_fingerprint = _fingerprint_source_selection(source_conn)
        if not _matches_expected(source_fingerprint, expected):
            raise RuntimeError("source selection no longer matches the frozen Windows Gate v1 fingerprint")
        with _connect(env, TARGET_DATABASE, read_only=False) as target_conn:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
                    cur.execute("LOCK TABLE public.trajectory_points IN SHARE MODE")
                    locked_target_before = _fingerprint(target_conn, TARGET_SCHEMA, TARGET_TABLE)
                    audit["locked_target_before"] = locked_target_before.as_dict()
                    backup = _backup_target(env, backup_path)
                    audit["backup"] = backup
                    cur.execute("LOCK TABLE public.trajectory_points IN ACCESS EXCLUSIVE MODE")
                    try:
                        _ensure_target_columns(cur)
                        cur.execute("TRUNCATE public.trajectory_points RESTART IDENTITY")
                        from psycopg import sql
                        columns = sql.SQL(", ").join(sql.Identifier(column) for column in COPY_COLUMNS)
                        copy_sql = sql.SQL("COPY public.trajectory_points ({}) FROM STDIN").format(columns)
                        with cur.copy(copy_sql) as copy:
                            for row in _source_rows(source_conn):
                                copy.write_row(_jsonb_row(row))
                        cur.execute("ALTER TABLE public.trajectory_points ALTER COLUMN source_id SET NOT NULL")
                        cur.execute("ALTER TABLE public.trajectory_points ALTER COLUMN source_row_key SET NOT NULL")
                        cur.execute("ALTER TABLE public.trajectory_points ALTER COLUMN stable SET NOT NULL")
                        _ensure_unique_source_constraint(cur)
                        _restore_sequence(cur)
                        after = _fingerprint(target_conn, TARGET_SCHEMA, TARGET_TABLE)
                        if not _matches_expected(after, expected):
                            raise RuntimeError("transactional restore fingerprint mismatch; rolling back")
                    except Exception:
                        # The enclosing transaction context rolls back every DDL/COPY change.
                        raise
            analyze_warning: str | None = None
            with target_conn.cursor() as cur:
                try:
                    cur.execute("ANALYZE public.trajectory_points")
                    target_conn.commit()
                except Exception as exc:
                    # Restore already committed above; only ANALYZE's new transaction rolls back.
                    target_conn.rollback()
                    analyze_warning = f"{type(exc).__name__}: {exc}"
    with _connect(env, TARGET_DATABASE, read_only=True) as verify_conn:
        verified = _fingerprint(verify_conn, TARGET_SCHEMA, TARGET_TABLE)
    if not _matches_expected(verified, expected):
        raise RuntimeError("post-commit verification fingerprint mismatch")
    return verified, analyze_warning


def _fingerprint_source_selection(conn: Any) -> Fingerprint:
    """Fingerprint exactly the ``manual_bench_seed_1m`` ordered LIMIT contract."""
    from psycopg import sql

    source = sql.Identifier(SOURCE_SCHEMA, SOURCE_TABLE)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "WITH seed AS (SELECT source_row_key, stable, lon, lat FROM {} "
                "ORDER BY vehicle_id, timestamp LIMIT %s) "
                "SELECT count(*), min(lon), min(lat), max(lon), max(lat), "
                "md5(string_agg(source_row_key::text, ',' ORDER BY source_row_key::text)), "
                "sum(CASE WHEN stable THEN 1 ELSE 0 END) FROM seed"
            ).format(source),
            (ROW_LIMIT,),
        )
        count, min_lon, min_lat, max_lon, max_lat, checksum, stable_true = cur.fetchone()
    return Fingerprint(
        int(count),
        [float(min_lon), float(min_lat), float(max_lon), float(max_lat)],
        str(checksum) if checksum is not None else None,
        int(stable_true) if stable_true is not None else None,
        list(COPY_COLUMNS),
    )


def _write_audit(path: Path, audit: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_env_path() -> Path:
    return Path(r"D:\Ai_Projects\track\track\v2\.env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only preflight or guarded restore for Windows Gate v1.")
    parser.add_argument("--apply", action="store_true", help="Take a full backup and then restore the frozen 1m target.")
    parser.add_argument("--manifest", type=Path, default=Path("generated/perf_windows/gate_manifest.json"))
    parser.add_argument("--env-file", type=Path, default=_default_env_path())
    parser.add_argument("--output", type=Path, default=Path("generated/perf_windows/manual_restore_windows_gate_v1.json"))
    parser.add_argument("--backup-dir", type=Path, default=Path("generated/perf_windows/backups"))
    args = parser.parse_args(argv)

    audit: dict[str, Any] = {
        "action": "apply" if args.apply else "preflight",
        "at_utc": datetime.now(UTC).isoformat(),
        "command_parameters": {
            "apply": args.apply,
            "manifest": str(args.manifest),
            "source_database": SOURCE_DATABASE,
            "source_schema": SOURCE_SCHEMA,
            "source_table": SOURCE_TABLE,
            "target_database": TARGET_DATABASE,
            "target_schema": TARGET_SCHEMA,
            "target_table": TARGET_TABLE,
            "row_limit": ROW_LIMIT,
        },
    }
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        expected = _expected_from_manifest(manifest)
        env = _load_env(args.env_file)
        with _connect(env, SOURCE_DATABASE, read_only=True) as source_conn:
            source = _fingerprint_source_selection(source_conn)
        with _connect(env, TARGET_DATABASE, read_only=True) as target_conn:
            target_before = _fingerprint(target_conn, TARGET_SCHEMA, TARGET_TABLE)
        audit.update({
            "expected": expected,
            "source_fingerprint": source.as_dict(),
            "target_before": target_before.as_dict(),
            "source_matches_frozen": _matches_expected(source, expected),
            "target_already_matches": _matches_expected(target_before, expected),
        })
        if not audit["source_matches_frozen"]:
            raise RuntimeError("source selection does not match frozen gate; refusing --apply")
        if not args.apply or audit["target_already_matches"]:
            audit["status"] = "NO_OP" if audit["target_already_matches"] else "PREFLIGHT_OK"
            _write_audit(args.output, audit)
            print(json.dumps({"status": audit["status"], "output": str(args.output)}, ensure_ascii=False))
            return 0

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = args.backup_dir / f"track_test-before-windows-gate-v1-{stamp}.dump"
        target_after, analyze_warning = _apply_restore(
            env, expected, backup_path, audit,
        )
        audit.update({
            "target_after": target_after.as_dict(),
            "status": "RESTORED_WITH_ANALYZE_WARNING" if analyze_warning else "RESTORED",
        })
        if analyze_warning:
            audit["analyze_warning"] = analyze_warning
        _write_audit(args.output, audit)
        print(json.dumps({"status": audit["status"], "output": str(args.output)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        audit.update({"status": "FAILED", "error_type": type(exc).__name__, "error": str(exc)})
        _write_audit(args.output, audit)
        print(json.dumps({"status": "FAILED", "output": str(args.output), "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
