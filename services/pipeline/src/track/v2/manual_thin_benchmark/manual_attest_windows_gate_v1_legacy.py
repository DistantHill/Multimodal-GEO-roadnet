"""Create or verify the write-once attestation for legacy Windows Gate v1.

The original immutable manifest predates schema and full-table attestations.
This helper never rewrites it: it records the exact restored target state in a
separate write-once sidecar, bound to the original manifest and recovery audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_SHA256 = "A6AE0F98392ED1F0695588C8BCA1B8443D9068927AAC8D5F369341D4C5261F03"
INPUT_SNAPSHOT_SHA256 = "176e4df049dd55139602166c1d2a936368c7ce1c19f49448ceee105a17aa6bcc"
VERSION = "WINDOWS-GATE-V1-LEGACY-ATTESTATION-1"
TARGET_DATABASE = "track_test"
TARGET_TABLE = "trajectory_points"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_json_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _load_env(path: Path) -> dict[str, str]:
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


def _load_manifest(path: Path) -> dict[str, Any]:
    if _file_sha256(path).upper() != MANIFEST_SHA256:
        raise RuntimeError("frozen manifest SHA mismatch")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("input_snapshot_sha256") != INPUT_SNAPSHOT_SHA256:
        raise RuntimeError("frozen manifest input snapshot mismatch")
    return value


def _expected_real(manifest: dict[str, Any]) -> dict[str, Any]:
    real = manifest.get("real_db")
    if not isinstance(real, dict):
        raise RuntimeError("manifest lacks real_db")
    required = ("database", "table", "row_count", "extent", "stable_id_checksum")
    if any(key not in real for key in required):
        raise RuntimeError("manifest real_db is incomplete")
    if real["database"] != TARGET_DATABASE or real["table"] != TARGET_TABLE:
        raise RuntimeError("manifest does not describe Windows Gate v1")
    return real


def _load_restore_audit(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    audit = json.loads(path.read_text(encoding="utf-8"))
    if audit.get("status") not in {"RESTORED", "RESTORED_WITH_ANALYZE_WARNING"}:
        raise RuntimeError("restore audit does not attest a committed restore")
    backup = audit.get("backup")
    after = audit.get("target_after")
    if not isinstance(backup, dict) or not isinstance(after, dict):
        raise RuntimeError("restore audit lacks backup or target_after evidence")
    backup_path = Path(str(backup.get("path", "")))
    if not backup_path.is_file() or _file_sha256(backup_path) != backup.get("sha256"):
        raise RuntimeError("restore backup is missing or SHA-256 does not match audit")
    if (
        after.get("row_count") != expected["row_count"]
        or after.get("extent") != expected["extent"]
        or after.get("source_row_key_checksum") != expected["stable_id_checksum"]
        or after.get("stable_true_count") != expected["row_count"]
    ):
        raise RuntimeError("restore audit target_after does not match frozen fingerprint")
    return audit


def _inspect_current(env: dict[str, str], expected: dict[str, Any]) -> dict[str, Any]:
    import psycopg
    from psycopg import sql

    with psycopg.connect(
        host=env["TRACK_PG_HOST"], port=env["TRACK_PG_PORT"],
        dbname=TARGET_DATABASE, user="postgres", password=env["TRACK_PG_PASSWORD"],
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
        options="-c default_transaction_read_only=on",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            if str(cur.fetchone()[0]).lower() != "on":
                raise RuntimeError("attestation inspection is not read-only")
            cur.execute(
                "SELECT ordinal_position, column_name, data_type, udt_name, is_nullable "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=%s ORDER BY ordinal_position", (TARGET_TABLE,),
            )
            schema = [
                {"ordinal_position": int(row[0]), "column_name": str(row[1]),
                 "data_type": str(row[2]), "udt_name": str(row[3]),
                 "is_nullable": str(row[4])}
                for row in cur
            ]
            columns = {item["column_name"] for item in schema}
            if not {"lon", "lat", "source_row_key", "stable"}.issubset(columns):
                raise RuntimeError("restored target lacks required provenance columns")
            cur.execute(
                sql.SQL(
                    "SELECT count(*), min(lon), min(lat), max(lon), max(lat), "
                    "md5(string_agg(source_row_key::text, ',' ORDER BY source_row_key::text)), "
                    "sum(CASE WHEN stable THEN 1 ELSE 0 END) FROM {}"
                ).format(sql.Identifier(TARGET_TABLE))
            )
            count, min_lon, min_lat, max_lon, max_lat, checksum, stable_true = cur.fetchone()
            fingerprint = {
                "row_count": int(count),
                "extent": [float(min_lon), float(min_lat), float(max_lon), float(max_lat)],
                "stable_id_checksum": checksum,
                "stable_true_count": int(stable_true),
            }
            if (
                fingerprint["row_count"] != expected["row_count"]
                or fingerprint["extent"] != expected["extent"]
                or fingerprint["stable_id_checksum"] != expected["stable_id_checksum"]
                or fingerprint["stable_true_count"] != expected["row_count"]
            ):
                raise RuntimeError("current target does not match frozen legacy fingerprint")
            digest = hashlib.sha256()
            query = sql.SQL(
                "SELECT to_jsonb(row_value)::text FROM {} AS row_value "
                "ORDER BY source_row_key::text"
            ).format(sql.Identifier(TARGET_TABLE))
            with conn.cursor(name="windows_gate_v1_legacy_full_hash") as stream:
                stream.itersize = 10_000
                stream.execute(query)
                for (payload,) in stream:
                    digest.update(str(payload).encode("utf-8"))
                    digest.update(b"\n")
    return {
        "database": TARGET_DATABASE,
        "table": TARGET_TABLE,
        "fingerprint": fingerprint,
        "schema": schema,
        "schema_sha256": _stable_json_sha(schema),
        "full_table_sha256": digest.hexdigest(),
        "read_only": True,
    }


def _build_sidecar(manifest_path: Path, restore_path: Path, env_path: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    expected = _expected_real(manifest)
    restore = _load_restore_audit(restore_path, expected)
    target = _inspect_current(_load_env(env_path), expected)
    return {
        "version": VERSION,
        "manifest": {
            "path": str(manifest_path), "sha256": MANIFEST_SHA256,
            "input_snapshot_sha256": INPUT_SNAPSHOT_SHA256,
        },
        "restore": {
            "path": str(restore_path), "sha256": _file_sha256(restore_path),
            "backup": restore["backup"],
        },
        "target": target,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write-once legacy Windows Gate v1 attestation.")
    parser.add_argument("--manifest", type=Path, default=Path("generated/perf_windows/gate_manifest.json"))
    parser.add_argument("--restore-audit", type=Path, default=Path("generated/perf_windows/manual_restore_windows_gate_v1_apply.json"))
    parser.add_argument("--env-file", type=Path, default=Path(r"D:\Ai_Projects\track\track\v2\.env"))
    parser.add_argument("--output", type=Path, default=Path("generated/perf_windows/windows_gate_v1_legacy_attestation.json"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    sidecar = _build_sidecar(args.manifest, args.restore_audit, args.env_file)
    if args.verify:
        if not args.output.is_file():
            raise SystemExit("legacy attestation missing (fail closed)")
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        if existing != sidecar:
            raise SystemExit("legacy attestation verification mismatch (fail closed)")
        print(json.dumps({"status": "VERIFIED", "output": str(args.output)}, sort_keys=True))
        return 0
    if args.output.exists():
        raise SystemExit("legacy attestation already exists (write once)")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "CREATED", "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
