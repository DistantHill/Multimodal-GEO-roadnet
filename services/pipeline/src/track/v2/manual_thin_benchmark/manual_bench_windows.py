"""Strict archive-vs-worktree benchmark for cross-track spatial windows.

Run through uv, using the same interpreter for both children::

    uv run --no-project --python D:\\H_Software\\miniforge3\\envs\\torch\\python.exe python \
      track/v2/manual_thin_benchmark/manual_bench_windows.py \
      --baseline-commit c12a06abe3262501f5bb20713c4280aa9a99f34e

The parent creates a temporary ``git archive`` for baseline execution.  Each
child imports from exactly one source tree and returns a complete ordered
candidate payload snapshot plus Windows PeakWorkingSetSize.  The default dense
case performs 3 warm-ups and 11 alternating measured child runs per side.
Real-DB support is deliberately opt-in and SELECT-only; unavailable DBs are
reported as unavailable rather than silently substituted with a fixture.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from statistics import median
from typing import Any, Iterator


BASELINE = "c12a06abe3262501f5bb20713c4280aa9a99f34e"
SEED = 20260810
FROZEN_MANIFEST_SHA256 = "A6AE0F98392ED1F0695588C8BCA1B8443D9068927AAC8D5F369341D4C5261F03"
FROZEN_INPUT_SNAPSHOT_SHA256 = "176e4df049dd55139602166c1d2a936368c7ce1c19f49448ceee105a17aa6bcc"
DENSE_SPEED_FLOOR = 0.95
REAL_SPEED_FLOOR = 1.30
DENSE_RSS_RATIO_CEILING = 1.05
REAL_RSS_RATIO_CEILING = 0.70
RSS_POLICY_VERSION = "RSS-1-20260811"
DENSE_MIN_WARMUPS = 3
DENSE_MIN_RUNS = 11
REAL_MIN_WARMUPS = 1
REAL_MIN_RUNS = 7
REAL_LEDGER_MAX_BYTES = 1_000_000
REAL_SEMANTIC_SIDECAR_MAX_BYTES = 1_000_000
HARNESS_FILES = (
    "manual_bench_windows.py",
    "manual_bench_windows_floor.py",
    "manual_bench_windows_phase.py",
    "perf_windows_gate.py",
    "manual_attest_windows_gate_v1_legacy.py",
)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_real_gate(
    frozen: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, Any]:
    """Return a fail-closed formal-real validation result.

    Formal timing is permitted only when the immutable manifest attests the
    schema and full table content in addition to the historical row-count,
    extent and source-row-key checksum.  Missing attestations are failures,
    never invitations to re-freeze the changed database.
    """
    error_codes: list[str] = []
    if not frozen.get("available", False):
        error_codes.append("frozen_database_unavailable")
    if not frozen.get("schema_sha256"):
        error_codes.append("frozen_schema_sha256_missing")
    if not frozen.get("full_table_sha256"):
        error_codes.append("frozen_full_table_sha256_missing")
    if not observed.get("available", False):
        error_codes.append("database_unavailable")
    if not observed.get("read_only", False):
        error_codes.append("database_not_read_only")
    if observed.get("database") != frozen.get("database"):
        error_codes.append("database_mismatch")
    if observed.get("table") != frozen.get("table"):
        error_codes.append("table_mismatch")
    if observed.get("row_count") != frozen.get("row_count"):
        error_codes.append("row_count_mismatch")
    if observed.get("extent") != frozen.get("extent"):
        error_codes.append("extent_mismatch")
    columns = set(observed.get("columns") or ())
    if "source_row_key" not in columns:
        error_codes.append("source_row_key_missing")
    if observed.get("source_row_key_checksum") != frozen.get("stable_id_checksum"):
        error_codes.append("source_row_key_checksum_mismatch")
    if frozen.get("schema_sha256") and observed.get("schema_sha256") != frozen["schema_sha256"]:
        error_codes.append("schema_mismatch")
    if (
        frozen.get("full_table_sha256")
        and observed.get("full_table_sha256") != frozen["full_table_sha256"]
    ):
        error_codes.append("full_table_hash_mismatch")
    return {
        "formal_valid": not error_codes,
        "error_codes": error_codes,
        "frozen": frozen,
        "observed": observed,
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_frozen_manifest(path: Path) -> dict[str, Any]:
    actual_sha = _file_sha256(path).upper()
    if actual_sha != FROZEN_MANIFEST_SHA256:
        raise RuntimeError(
            f"frozen manifest SHA mismatch: {actual_sha} != {FROZEN_MANIFEST_SHA256}"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("input_snapshot_sha256") != FROZEN_INPUT_SNAPSHOT_SHA256:
        raise RuntimeError("frozen input_snapshot_sha256 mismatch")
    if _sha(manifest["dense_fixture"]) != manifest.get("dense_fixture_sha256"):
        raise RuntimeError("frozen dense_fixture_sha256 mismatch")
    return manifest


def _load_legacy_attestation(
    path: Path,
    pinned_sha256: str | None,
    frozen_real: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed unless a pinned, write-once sidecar exactly augments v1."""
    if frozen_real.get("schema_sha256") or frozen_real.get("full_table_sha256"):
        raise RuntimeError("legacy sidecar is allowed only for manifests missing both new attestations")
    if not path.is_file() or not pinned_sha256:
        raise RuntimeError("legacy attestation and its pinned SHA-256 are required (fail closed)")
    actual_sha = _file_sha256(path)
    if actual_sha.lower() != pinned_sha256.lower():
        raise RuntimeError("legacy attestation SHA-256 mismatch (fail closed)")
    value = json.loads(path.read_text(encoding="utf-8"))
    manifest = value.get("manifest") or {}
    if (
        value.get("version") != "WINDOWS-GATE-V1-LEGACY-ATTESTATION-1"
        or manifest.get("sha256") != FROZEN_MANIFEST_SHA256
        or manifest.get("input_snapshot_sha256") != FROZEN_INPUT_SNAPSHOT_SHA256
    ):
        raise RuntimeError("legacy attestation is not bound to the frozen manifest")
    restore = value.get("restore") or {}
    backup = restore.get("backup") or {}
    restore_path = Path(str(restore.get("path", "")))
    backup_path = Path(str(backup.get("path", "")))
    if (
        not restore_path.is_file()
        or _file_sha256(restore_path) != restore.get("sha256")
        or not backup_path.is_file()
        or _file_sha256(backup_path) != backup.get("sha256")
    ):
        raise RuntimeError("legacy attestation restore/backup binding mismatch (fail closed)")
    target = value.get("target") or {}
    fingerprint = target.get("fingerprint") or {}
    if (
        target.get("database") != frozen_real.get("database")
        or target.get("table") != frozen_real.get("table")
        or fingerprint.get("row_count") != frozen_real.get("row_count")
        or fingerprint.get("extent") != frozen_real.get("extent")
        or fingerprint.get("stable_id_checksum") != frozen_real.get("stable_id_checksum")
        or fingerprint.get("stable_true_count") != frozen_real.get("row_count")
        or not target.get("schema")
        or not target.get("schema_sha256")
        or not target.get("full_table_sha256")
    ):
        raise RuntimeError("legacy attestation target content mismatch (fail closed)")
    augmented = {
        **frozen_real,
        "schema": target["schema"],
        "schema_sha256": target["schema_sha256"],
        "full_table_sha256": target["full_table_sha256"],
    }
    return augmented, {
        "path": str(path), "sha256": actual_sha,
        "restore_audit_sha256": restore["sha256"],
        "backup_sha256": backup["sha256"],
        "version": value["version"],
    }


def _load_env_file(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (item.strip() for item in path.read_text(encoding="utf-8").splitlines())
        if line and not line.startswith("#") and "=" in line
    )


def _stream_full_table_sha256(conn: Any, table: str) -> str:
    """Hash canonical JSONB rows in source-row-key order using a named cursor."""
    from psycopg import sql

    digest = hashlib.sha256()
    query = sql.SQL(
        "SELECT to_jsonb(row_value)::text FROM {} AS row_value "
        "ORDER BY source_row_key::text"
    ).format(sql.Identifier(table))
    with conn.cursor(name="perf_windows_full_hash") as cursor:
        cursor.itersize = 10_000
        cursor.execute(query)
        for (payload,) in cursor:
            digest.update(str(payload).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _inspect_real_db(
    env_file: Path,
    frozen_real: dict[str, Any],
) -> dict[str, Any]:
    """Inspect the real workload with SELECT-only queries for formal gating."""
    import psycopg
    from psycopg import sql

    env = _load_env_file(env_file)
    observed: dict[str, Any] = {
        "available": False,
        "database": frozen_real.get("database"),
        "table": frozen_real.get("table"),
        "read_only": False,
        "full_hash_computed": False,
    }
    try:
        with psycopg.connect(
            host=env["TRACK_PG_HOST"],
            port=env["TRACK_PG_PORT"],
            dbname=frozen_real["database"],
            user=env["TRACK_PG_USER"],
            password=env["TRACK_PG_PASSWORD"],
            connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
            options="-c default_transaction_read_only=on",
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW transaction_read_only")
                observed["read_only"] = str(cursor.fetchone()[0]).lower() == "on"
                cursor.execute(
                    "SELECT ordinal_position, column_name, data_type, udt_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (frozen_real["table"],),
                )
                schema = [
                    {
                        "ordinal_position": int(row[0]),
                        "column_name": str(row[1]),
                        "data_type": str(row[2]),
                        "udt_name": str(row[3]),
                        "is_nullable": str(row[4]),
                    }
                    for row in cursor
                ]
                columns = [item["column_name"] for item in schema]
                observed.update(
                    {
                        "available": True,
                        "schema": schema,
                        "schema_sha256": _sha(schema),
                        "columns": columns,
                    }
                )
                required_extent = {"lon", "lat"}
                if required_extent.issubset(columns):
                    cursor.execute(
                        sql.SQL(
                            "SELECT count(*), min(lon), max(lon), min(lat), max(lat) FROM {}"
                        ).format(sql.Identifier(frozen_real["table"]))
                    )
                    count, min_lon, max_lon, min_lat, max_lat = cursor.fetchone()
                    observed["row_count"] = int(count)
                    observed["extent"] = [
                        float(min_lon) if min_lon is not None else None,
                        float(min_lat) if min_lat is not None else None,
                        float(max_lon) if max_lon is not None else None,
                        float(max_lat) if max_lat is not None else None,
                    ]
                if "source_row_key" in columns:
                    cursor.execute(
                        sql.SQL(
                            "SELECT md5(string_agg(source_row_key::text, ',' "
                            "ORDER BY source_row_key::text)) FROM {}"
                        ).format(sql.Identifier(frozen_real["table"]))
                    )
                    observed["source_row_key_checksum"] = cursor.fetchone()[0]
                else:
                    observed["source_row_key_checksum"] = None

            preliminary_matches = (
                frozen_real.get("full_table_sha256")
                and observed.get("row_count") == frozen_real.get("row_count")
                and observed.get("extent") == frozen_real.get("extent")
                and observed.get("schema_sha256") == frozen_real.get("schema_sha256")
                and observed.get("source_row_key_checksum")
                == frozen_real.get("stable_id_checksum")
            )
            if preliminary_matches:
                observed["full_table_sha256"] = _stream_full_table_sha256(
                    conn, frozen_real["table"]
                )
                observed["full_hash_computed"] = True
            else:
                observed["full_table_sha256"] = None
    except Exception as exc:
        observed["inspection_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    return observed


def _real_gate_preflight(
    manifest_path: Path,
    env_file: Path,
    output: Path,
    legacy_attestation_path: Path,
    legacy_attestation_sha256: str | None,
) -> dict[str, Any]:
    manifest = _load_frozen_manifest(manifest_path)
    frozen_real, legacy_binding = _load_legacy_attestation(
        legacy_attestation_path, legacy_attestation_sha256, manifest["real_db"],
    )
    observed = _inspect_real_db(env_file, frozen_real)
    validation = _validate_real_gate(frozen_real, observed)
    evidence = {
        "mode": "diagnostic_only" if not validation["formal_valid"] else "formal_eligible",
        "algorithm_called": False,
        "manifest_sha256": FROZEN_MANIFEST_SHA256,
        "input_snapshot_sha256": FROZEN_INPUT_SNAPSHOT_SHA256,
        "legacy_attestation": legacy_binding,
        "validation": validation,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def _peak_working_set() -> int:
    """Return Windows GetProcessMemoryInfo PeakWorkingSetSize in bytes."""
    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]
    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("Psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    if not ok:
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


class _ProgressRecorder:
    """Capture only deterministic progress fields; elapsed time is evidence-only."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def update(self, **values: Any) -> None:
        event = {
            key: values[key]
            for key in ("progress", "detail", "sub_detail", "stage")
            if key in values
        }
        # Time-throttled inner updates can occur at different pair indices when
        # an optimization succeeds.  Window-stage/progress events are the
        # deterministic public contract; each window's final pair state is
        # captured separately in ``progress_phase``.
        if event and ("stage" in event or "progress" in event):
            self.events.append(event)


class _NullProgress:
    """Suppress benchmark console output without retaining timed evidence."""

    def update(self, **_values: Any) -> None:
        return


@contextmanager
def _legacy_phase_capture(
    intersect2: Any,
    telemetry: dict[str, int | float],
) -> Iterator[None]:
    """Collect disjoint baseline phase samples through reversible seams.

    The frozen baseline predates the native telemetry keyword.  These wrappers
    are used only by the non-gating ``phase`` child.  Nested query/materialize
    time is subtracted explicitly; all other wrappers target disjoint calls.
    Python segment-loop/control time that has no baseline seam remains
    unattributed instead of being double counted.
    """

    originals: list[tuple[Any, str, Any]] = []

    def replace(obj: Any, name: str, wrapper: Any) -> None:
        originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, wrapper)

    original_build = intersect2._build_points_from_rows

    def build_points(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_build(*args, **kwargs)
        finally:
            telemetry["materialization_seconds"] = telemetry.get(
                "materialization_seconds", 0.0
            ) + (time.perf_counter() - started)

    replace(intersect2, "_build_points_from_rows", build_points)

    original_query = intersect2._query_active_points_in_window

    def query(*args: Any, **kwargs: Any) -> Any:
        materialization_before = float(telemetry.get("materialization_seconds", 0.0))
        started = time.perf_counter()
        try:
            return original_query(*args, **kwargs)
        finally:
            total = time.perf_counter() - started
            nested = float(telemetry.get("materialization_seconds", 0.0)) - materialization_before
            telemetry["query_seconds"] = telemetry.get("query_seconds", 0.0) + max(
                0.0, total - nested
            )

    replace(intersect2, "_query_active_points_in_window", query)

    def timed_wrapper(name: str, bucket: str) -> None:
        original = getattr(intersect2, name)

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                telemetry[bucket] = telemetry.get(bucket, 0.0) + (
                    time.perf_counter() - started
                )

        replace(intersect2, name, wrapped)

    timed_wrapper("_tracks_from_points", "group_sort_seconds")
    timed_wrapper("_to_xy", "segment_build_seconds")
    timed_wrapper("_batch_segment_intersections", "exact_seconds")
    timed_wrapper("_cross_track_candidate_key", "dedupe_seconds")

    original_candidate = intersect2.CrossTrackCandidate

    def candidate(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_candidate(*args, **kwargs)
        finally:
            telemetry["payload_seconds"] = telemetry.get("payload_seconds", 0.0) + (
                time.perf_counter() - started
            )

    replace(intersect2, "CrossTrackCandidate", candidate)

    # Baseline creates NumPy input arrays inside the vehicle-pair loop.  The
    # module holds the shared numpy object, so restore it before leaving.
    original_array = intersect2.np.array

    def array(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_array(*args, **kwargs)
        finally:
            telemetry["array_build_seconds"] = telemetry.get(
                "array_build_seconds", 0.0
            ) + (time.perf_counter() - started)

    replace(intersect2.np, "array", array)
    try:
        yield
    finally:
        for obj, name, original in reversed(originals):
            setattr(obj, name, original)


def _candidate_key(candidate: Any) -> list[str]:
    return [
        candidate.vehicle_id,
        candidate.from_source_id,
        candidate.from_source_row_key,
        candidate.to_source_id,
        candidate.to_source_row_key,
    ]


def _ordered_track_input(tracks: dict[str, list[Any]]) -> list[dict[str, Any]]:
    return [
        {
            "vehicle_id": vehicle_id,
            "points": [asdict(point) for point in tracks[vehicle_id]],
        }
        for vehicle_id in sorted(tracks)
    ]


def _candidate_semantics(candidates: list[Any]) -> dict[str, Any]:
    payload = [asdict(candidate) for candidate in candidates]
    return {
        "candidate_count": len(candidates),
        "candidate_stable_keys_ordered": [_candidate_key(candidate) for candidate in candidates],
        "candidate_payload_ordered": payload,
        "candidate_payload_sha256": _sha(payload),
    }


def _canonical_digest(values: Any) -> str:
    """Digest canonical ordered content without retaining a giant JSON array."""
    digest = hashlib.sha256()
    for value in values:
        digest.update(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _candidate_attestation(candidates: list[Any]) -> dict[str, Any]:
    return {
        "candidate_count": len(candidates),
        "candidate_payload_ordered_sha256": _canonical_digest(asdict(item) for item in candidates),
        "candidate_stable_keys_ordered_sha256": _canonical_digest(_candidate_key(item) for item in candidates),
    }


def _phase_summary(
    telemetry: dict[str, int | float],
    wall_seconds: float,
) -> dict[str, Any]:
    phase_names = (
        "query_seconds",
        "materialization_seconds",
        "group_sort_seconds",
        "segment_build_seconds",
        "array_build_seconds",
        "broad_seconds",
        "exact_seconds",
        "payload_seconds",
        "dedupe_seconds",
    )
    phases: dict[str, Any] = {}
    points = int(telemetry.get("queried_rows", 0))
    segment_pairs = int(telemetry.get("segment_pair_space", 0))
    windows = int(telemetry.get("windows_with_points", 0))
    for name in phase_names:
        seconds = float(telemetry.get(name, 0.0))
        phases[name.removesuffix("_seconds")] = {
            "seconds": seconds,
            "percent_of_wall": (100.0 * seconds / wall_seconds) if wall_seconds else 0.0,
            "per_window_seconds": (seconds / windows) if windows else None,
            "per_point_us": (seconds * 1_000_000.0 / points) if points else None,
            "per_segment_pair_ns": (
                seconds * 1_000_000_000.0 / segment_pairs if segment_pairs else None
            ),
        }
    measured = sum(item["seconds"] for item in phases.values())
    return {
        "phases": phases,
        "unattributed_seconds": max(0.0, wall_seconds - measured),
        "counters": {
            key: value
            for key, value in telemetry.items()
            if not key.endswith("_seconds")
        },
    }


def _point(vehicle: str, row: int, timestamp: float, lon: float, lat: float, heading: float):
    from track.v2.models import PointRef, TrajectoryPoint
    return TrajectoryPoint(
        point_ref=PointRef("windows_gate_v1", f"{vehicle}-{row}", stable=True),
        vehicle_id=vehicle, timestamp=timestamp, lon=lon, lat=lat, heading=heading, speed=10.0,
    )


def _dense_tracks() -> dict[str, list[Any]]:
    """Build the dense input exactly from the frozen gate manifest."""
    manifest_path = Path(os.environ["PERF_WINDOWS_GATE_MANIFEST"])
    manifest = _load_frozen_manifest(manifest_path)
    fixture = manifest["dense_fixture"]
    from track.v2._geometry import _to_lonlat
    origin_lon, origin_lat = 116.0, 39.0
    tracks: dict[str, list[Any]] = {}
    for row, segment in enumerate(fixture["segments"]):
        vehicle = segment["vehicle"]
        a_lon, a_lat = _to_lonlat(*segment["a"], origin_lon, origin_lat)
        b_lon, b_lat = _to_lonlat(*segment["b"], origin_lon, origin_lat)
        tracks[vehicle] = [
            _point(vehicle, 0, row * 2.0, a_lon, a_lat, segment["heading"]),
            _point(vehicle, 1, row * 2.0 + 1.0, b_lon, b_lat, segment["heading"]),
        ]
    return tracks


def _capture_real_windows(
    intersect2: Any,
    conn: Any,
    cfg: Any,
    extent: tuple[float, float, float, float],
    telemetry: dict[str, int | float] | None,
) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Capture complete per-window semantics in a non-gating child.

    Raw point/candidate objects are retained during the algorithm and converted
    to JSON only after the timed call.  This mode is deliberately separate from
    the measured children so evidence materialization cannot inflate the formal
    PeakWorkingSetSize.
    """
    raw_records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    original_query = intersect2._query_active_points_in_window
    original_extract = intersect2._extract_orthogonal_cross_track_candidates

    def query(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal current
        points = original_query(*args, **kwargs)
        window = args[1]
        current = {
            "window_index": len(raw_records) + 1,
            "window_object": window,
            "input_points": points,
            "pair_count": 0,
            "total_pairs": 0,
            "window_candidates": [],
        }
        raw_records.append(current)
        return points

    def extract(*args: Any, **kwargs: Any) -> tuple[list[Any], int, int]:
        if current is None:
            raise RuntimeError("cross-track extraction occurred before window query capture")
        candidates, pair_count, total_pairs = original_extract(*args, **kwargs)
        current["pair_count"] = pair_count
        current["total_pairs"] = total_pairs
        current["window_candidates"] = candidates
        return candidates, pair_count, total_pairs

    intersect2._query_active_points_in_window = query
    intersect2._extract_orthogonal_cross_track_candidates = extract
    recorder = _ProgressRecorder()
    try:
        kwargs: dict[str, Any] = {"live_status": recorder}
        if telemetry is not None and "telemetry" in inspect.signature(
            intersect2._extract_cross_track_windows
        ).parameters:
            kwargs["telemetry"] = telemetry
        candidates = intersect2._extract_cross_track_windows(
            conn, cfg, extent, extent[0], extent[1], **kwargs
        )
    finally:
        intersect2._query_active_points_in_window = original_query
        intersect2._extract_orthogonal_cross_track_candidates = original_extract

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    running_count = 0
    for raw in raw_records:
        points_payload = [asdict(point) for point in raw["input_points"]]
        candidate_semantics = _candidate_attestation(raw["window_candidates"])
        added = 0
        for candidate in raw["window_candidates"]:
            key = tuple(_candidate_key(candidate))
            if key not in seen:
                seen.add(key)
                running_count += 1
                added += 1
        records.append({
            "window_index": raw["window_index"],
            "window": asdict(raw["window_object"]),
            "input_points_sha256": _sha(points_payload),
            "input_point_count": len(points_payload),
            "pair_count": raw["pair_count"],
            "total_pairs": raw["total_pairs"],
            **{
                f"window_{key}": value
                for key, value in candidate_semantics.items()
            },
            "dedupe_added": added,
            "dedupe_removed": candidate_semantics["candidate_count"] - added,
            "final_candidate_count_after_window": running_count,
            "phase_sequence": ["query", "materialization", "window_done"]
            if not points_payload else [
                "query", "materialization", "group_sort", "segment_build",
                "array_build", "broad", "exact", "payload", "dedupe",
                "window_done",
            ],
            "progress_phase": {
                "phase": "cross_track_complete",
                "pair_count": raw["pair_count"],
                "total_pairs": raw["total_pairs"],
                "window_candidate_count": candidate_semantics["candidate_count"],
            },
        })
    return candidates, records, recorder.events


def _measure_real_windows(
    intersect2: Any,
    conn: Any,
    cfg: Any,
    extent: tuple[float, float, float, float],
    telemetry: dict[str, int | float] | None,
) -> tuple[list[Any], int, int]:
    """Run the real pipeline while retaining only aggregate pair semantics."""
    pair_count = 0
    total_pairs = 0
    original_extract = intersect2._extract_orthogonal_cross_track_candidates

    def extract(*args: Any, **kwargs: Any) -> tuple[list[Any], int, int]:
        nonlocal pair_count, total_pairs
        candidates, window_pairs, window_total = original_extract(*args, **kwargs)
        pair_count += window_pairs
        total_pairs += window_total
        return candidates, window_pairs, window_total

    intersect2._extract_orthogonal_cross_track_candidates = extract
    try:
        kwargs: dict[str, Any] = {"live_status": _NullProgress()}
        if telemetry is not None and "telemetry" in inspect.signature(
            intersect2._extract_cross_track_windows
        ).parameters:
            kwargs["telemetry"] = telemetry
        candidates = intersect2._extract_cross_track_windows(
            conn, cfg, extent, extent[0], extent[1], **kwargs
        )
    finally:
        intersect2._extract_orthogonal_cross_track_candidates = original_extract
    return candidates, pair_count, total_pairs


def _child_provenance(intersect2: Any) -> dict[str, Any]:
    import track.v2._geometry as geometry

    source_root = Path(os.environ["PERF_WINDOWS_EXPECTED_SOURCE_ROOT"]).resolve()
    intersect_path = Path(intersect2.__file__).resolve()
    geometry_path = Path(geometry.__file__).resolve()
    for path in (intersect_path, geometry_path):
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise RuntimeError(f"mixed source tree: {path} not under {source_root}") from exc
    manifest_path = Path(os.environ["PERF_WINDOWS_GATE_MANIFEST"])
    if _file_sha256(manifest_path).upper() != FROZEN_MANIFEST_SHA256:
        raise RuntimeError("child manifest SHA mismatch")
    if Path(sys.executable).resolve() != Path(os.environ["PERF_WINDOWS_EXECUTABLE"]).resolve():
        raise RuntimeError("child interpreter mismatch")
    expected_source_hashes = json.loads(os.environ["PERF_WINDOWS_SOURCE_HASHES"])
    actual_source_hashes = {
        "track/v2/intersect2.py": _file_sha256(intersect_path),
        "track/v2/_geometry.py": _file_sha256(geometry_path),
    }
    if actual_source_hashes != expected_source_hashes:
        raise RuntimeError(
            f"child source digest mismatch: {actual_source_hashes} != {expected_source_hashes}"
        )
    harness_files = json.loads(os.environ["PERF_WINDOWS_HARNESS_FILES"])
    actual_harness_files = {
        name: _file_sha256(Path(__file__).resolve().parent / name)
        for name in HARNESS_FILES
    }
    if actual_harness_files != harness_files:
        raise RuntimeError("child harness file digest mismatch")
    if _sha(actual_harness_files) != os.environ["PERF_WINDOWS_HARNESS_BUNDLE_SHA256"]:
        raise RuntimeError("child harness bundle mismatch")
    lock_path = Path(os.environ["PERF_WINDOWS_LOCK_PATH"])
    lock_sha256 = _file_sha256(lock_path)
    if lock_sha256 != os.environ["PERF_WINDOWS_LOCK_SHA256"]:
        raise RuntimeError("child runtime lock digest mismatch")
    return {
        "source_label": os.environ["PERF_WINDOWS_SOURCE_LABEL"],
        "source_root": str(source_root),
        "source_commit": os.environ["PERF_WINDOWS_SOURCE_COMMIT"],
        "intersect2_path": str(intersect_path),
        "intersect2_sha256": actual_source_hashes["track/v2/intersect2.py"],
        "geometry_path": str(geometry_path),
        "geometry_sha256": actual_source_hashes["track/v2/_geometry.py"],
        "dirty_diff_sha256": os.environ["PERF_WINDOWS_DIRTY_DIFF_SHA256"],
        "harness_bundle_sha256": os.environ["PERF_WINDOWS_HARNESS_BUNDLE_SHA256"],
        "harness_files": actual_harness_files,
        "lock_path": str(lock_path.resolve()),
        "lock_sha256": lock_sha256,
        "baseline_archive_sha256": os.environ["PERF_WINDOWS_ARCHIVE_SHA256"],
        "manifest_sha256": FROZEN_MANIFEST_SHA256,
        "input_snapshot_sha256": FROZEN_INPUT_SNAPSHOT_SHA256,
        "parameters": json.loads(os.environ["PERF_WINDOWS_PARAMETERS"]),
        "source_hashes": actual_source_hashes,
    }


def _child(case: str, run_kind: str) -> dict[str, Any]:
    from track.v2.intersect2 import (
        IntersectConfig,
        _extract_cross_track_windows,
        _extract_orthogonal_cross_track_candidates,
        _query_spatial_extent,
    )
    import track.v2.intersect2 as intersect2
    if run_kind not in {"measurement", "semantic", "phase"}:
        raise ValueError(f"unsupported child run kind: {run_kind}")
    collect_phase = run_kind == "phase"
    telemetry: dict[str, int | float] | None = {} if collect_phase else None
    native_telemetry = "telemetry" in inspect.signature(
        _extract_orthogonal_cross_track_candidates
    ).parameters
    phase_context = (
        nullcontext()
        if not collect_phase or native_telemetry
        else _legacy_phase_capture(intersect2, telemetry)
    )
    semantic_snapshot: dict[str, Any] | None = None
    progress_events: list[dict[str, Any]] = []
    with phase_context:
        if case == "dense":
            tracks = _dense_tracks()
            progress: Any = _ProgressRecorder() if run_kind == "semantic" else _NullProgress()
            kwargs: dict[str, Any] = {"live_status": progress}
            if collect_phase and native_telemetry:
                kwargs["telemetry"] = telemetry
            start = time.perf_counter()
            candidates, pair_count, total_pairs = _extract_orthogonal_cross_track_candidates(
                tracks, 116.0, 39.0, IntersectConfig(), **kwargs,
            )
            elapsed = time.perf_counter() - start
            peak_working_set = _peak_working_set()
            if telemetry is not None:
                telemetry["windows_with_points"] = 1
                telemetry["queried_rows"] = sum(len(track) for track in tracks.values())
                telemetry.setdefault("segment_pair_space", total_pairs)
            ordered_input = _ordered_track_input(tracks)
            input_snapshot = {
                "dense_fixture_sha256": "63ac47866f6ffdc8400c38246805bb96112a1b3a6bd5ae869aa2875ac5998261",
                "manifest_sha256": FROZEN_MANIFEST_SHA256,
            }
            if run_kind == "semantic":
                progress_events = progress.events
                window_records = [{
                    "window_index": 1,
                    "window": {"logical_dense_gate": True},
                    "input_tracks_ordered": ordered_input,
                    "input_tracks_sha256": _sha(ordered_input),
                    "pair_count": pair_count,
                    "total_pairs": total_pairs,
                    **{
                        f"window_{key}": value
                        for key, value in _candidate_semantics(candidates).items()
                    },
                    "phase_sequence": [
                        "input", "segment_build", "array_build", "broad", "exact",
                        "payload", "cross_track_complete",
                    ],
                    "progress_phase": {
                        "phase": "cross_track_complete",
                        "pair_count": pair_count,
                        "total_pairs": total_pairs,
                        "window_candidate_count": len(candidates),
                    },
                }]
        elif case == "real":
            from track.v2.manual_thin_benchmark._thin_bench_helpers import load_test_env
            import psycopg
            attestation_path = Path(os.environ["PERF_WINDOWS_REAL_ATTESTATION"])
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            legacy_attestation_path = Path(os.environ["PERF_WINDOWS_LEGACY_ATTESTATION"])
            if _file_sha256(legacy_attestation_path) != os.environ["PERF_WINDOWS_LEGACY_ATTESTATION_SHA256"]:
                raise RuntimeError("real benchmark child legacy-attestation pin mismatch")
            if not attestation["validation"]["formal_valid"]:
                raise RuntimeError("real benchmark child refused invalid formal attestation")
            if attestation["manifest_sha256"] != FROZEN_MANIFEST_SHA256:
                raise RuntimeError("real benchmark child manifest attestation mismatch")
            env = load_test_env(os.environ["PERF_WINDOWS_ENV_FILE"])
            frozen_real = attestation["validation"]["frozen"]
            conn = psycopg.connect(
                host=env["TRACK_PG_HOST"], port=env["TRACK_PG_PORT"],
                dbname=frozen_real["database"], user=env["TRACK_PG_USER"],
                password=env["TRACK_PG_PASSWORD"], connect_timeout=10,
                options="-c default_transaction_read_only=on",
            )
            try:
                cfg = IntersectConfig(
                    use_orthogonal_candidates=True,
                    db_table=frozen_real["table"],
                )
                extent = _query_spatial_extent(conn, table=cfg.db_table)
                if extent is None:
                    raise RuntimeError("real DB has no active-point extent")
                with conn.cursor() as cursor:
                    cursor.execute(psycopg.sql.SQL(
                        "SELECT count(*), min(lon), max(lon), min(lat), max(lat), "
                        "md5(string_agg(source_row_key::text, ',' ORDER BY source_row_key::text)) FROM {}"
                    ).format(psycopg.sql.Identifier(cfg.db_table)))
                    count, min_lon, max_lon, min_lat, max_lat, checksum = cursor.fetchone()
                if (
                    int(count) != frozen_real["row_count"]
                    or [float(min_lon), float(min_lat), float(max_lon), float(max_lat)] != frozen_real["extent"]
                    or checksum != frozen_real["stable_id_checksum"]
                ):
                    raise RuntimeError("real benchmark child lightweight database fingerprint drift")
                start = time.perf_counter()
                if run_kind == "semantic":
                    candidates, window_records, progress_events = _capture_real_windows(
                        intersect2, conn, cfg, extent, telemetry
                    )
                    pair_count = sum(record["pair_count"] for record in window_records)
                    total_pairs = sum(record["total_pairs"] for record in window_records)
                else:
                    candidates, pair_count, total_pairs = _measure_real_windows(
                        intersect2, conn, cfg, extent, telemetry
                    )
                elapsed = time.perf_counter() - start
                peak_working_set = _peak_working_set()
                input_snapshot = {
                    "extent": list(extent),
                    "database": frozen_real["database"],
                    "table": cfg.db_table,
                    "formal_attestation_sha256": _file_sha256(attestation_path),
                }
            finally:
                conn.close()
        else:
            raise ValueError(f"unsupported child case: {case}")

    candidate_proof = _candidate_attestation(candidates) if case == "real" else _candidate_semantics(candidates)
    result_snapshot = {
        "case": case, "seed": SEED, "pair_count": pair_count,
        "total_pairs": total_pairs, "input": input_snapshot, **candidate_proof,
    }
    if run_kind == "semantic":
        if case == "real":
            semantic_snapshot = {
                **result_snapshot,
                "window_count": len(window_records),
                "window_sequence_sha256": _canonical_digest(window_records),
                "window_pair_sequence_sha256": _canonical_digest({
                    "pair_count": record["pair_count"], "total_pairs": record["total_pairs"],
                } for record in window_records),
                "progress_sequence_sha256": _canonical_digest(progress_events),
            }
            semantic_snapshot["canonical_snapshot_sha256"] = _sha(semantic_snapshot)
        else:
            semantic_snapshot = {
                **result_snapshot,
                "window_sequence": window_records,
                "progress_events": progress_events,
            }
    phase_metrics: dict[str, Any]
    if telemetry is None:
        phase_metrics = {"status": "NOT_COLLECTED", "reason": "measurement_is_uninstrumented"}
    else:
        phase_metrics = _phase_summary(telemetry, elapsed)
        phase_metrics["collection_method"] = (
            "native_non_overlapping_telemetry"
            if native_telemetry else "legacy_reversible_disjoint_wrappers"
        )
    snapshot = semantic_snapshot if semantic_snapshot is not None else result_snapshot
    return {
        "run_kind": run_kind,
        "sys_executable": sys.executable,
        "python_version": sys.version,
        "wall_seconds": elapsed,
        "peak_working_set_bytes": peak_working_set,
        "snapshot": snapshot,
        "snapshot_sha256": _sha(snapshot),
        "result_snapshot": result_snapshot,
        "result_snapshot_sha256": _sha(result_snapshot),
        "phase_metrics": phase_metrics,
        "provenance": _child_provenance(intersect2),
    }


def _run_child(
    source_root: Path,
    runner: Path,
    case: str,
    run_kind: str,
    manifest_path: Path,
    env_file: Path,
    real_attestation: Path | None,
    expected: dict[str, Any],
) -> dict[str, Any]:
    # The runner intentionally stays in the current worktree: the archived
    # baseline predates this harness.  PYTHONPATH is the sole project import
    # root, so child pipeline imports still come exclusively from its side.
    command = [
        sys.executable, str(runner), "--child", "--case", case,
        "--run-kind", run_kind,
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)
    env["PERF_WINDOWS_GATE_MANIFEST"] = str(manifest_path)
    env["PERF_WINDOWS_ENV_FILE"] = str(env_file)
    env["PERF_WINDOWS_EXPECTED_SOURCE_ROOT"] = str(source_root)
    env["PERF_WINDOWS_EXECUTABLE"] = str(Path(sys.executable).resolve())
    env["PERF_WINDOWS_SOURCE_LABEL"] = expected["source_label"]
    env["PERF_WINDOWS_SOURCE_COMMIT"] = expected["source_commit"]
    env["PERF_WINDOWS_SOURCE_HASHES"] = json.dumps(
        expected["source_hashes"], sort_keys=True
    )
    env["PERF_WINDOWS_DIRTY_DIFF_SHA256"] = expected["dirty_diff_sha256"]
    env["PERF_WINDOWS_HARNESS_BUNDLE_SHA256"] = expected["harness_bundle_sha256"]
    env["PERF_WINDOWS_HARNESS_FILES"] = json.dumps(
        expected["harness_files"], sort_keys=True
    )
    env["PERF_WINDOWS_LOCK_PATH"] = expected["lock_path"]
    env["PERF_WINDOWS_LOCK_SHA256"] = expected["lock_sha256"]
    env["PERF_WINDOWS_ARCHIVE_SHA256"] = expected["baseline_archive_sha256"]
    env["PERF_WINDOWS_PARAMETERS"] = json.dumps(expected["parameters"], sort_keys=True)
    if real_attestation is not None:
        env["PERF_WINDOWS_REAL_ATTESTATION"] = str(real_attestation)
        env["PERF_WINDOWS_LEGACY_ATTESTATION"] = str(expected["parameters"]["legacy_attestation_path"])
        env["PERF_WINDOWS_LEGACY_ATTESTATION_SHA256"] = expected["parameters"]["legacy_attestation_sha256"]
    completed = subprocess.run(command, cwd=source_root, env=env, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(
            f"benchmark child failed ({completed.returncode}) for {source_root}:\n{completed.stderr}\n{completed.stdout}"
        )
    sample = json.loads(completed.stdout.splitlines()[-1])
    _validate_child_sample(sample, expected, run_kind)
    return sample


def _validate_child_sample(
    sample: dict[str, Any],
    expected: dict[str, Any],
    run_kind: str,
) -> None:
    """Fail closed on any interpreter/source/evidence contract drift."""
    errors: list[str] = []
    provenance = sample.get("provenance") or {}
    checks = {
        "run_kind": (sample.get("run_kind"), run_kind),
        "sys_executable": (
            str(Path(sample.get("sys_executable", "")).resolve()),
            str(Path(sys.executable).resolve()),
        ),
        "source_label": (provenance.get("source_label"), expected["source_label"]),
        "source_root": (
            str(Path(provenance.get("source_root", "")).resolve()),
            str(Path(expected["source_root"]).resolve()),
        ),
        "source_commit": (provenance.get("source_commit"), expected["source_commit"]),
        "source_hashes": (provenance.get("source_hashes"), expected["source_hashes"]),
        "dirty_diff_sha256": (
            provenance.get("dirty_diff_sha256"), expected["dirty_diff_sha256"]
        ),
        "harness_bundle_sha256": (
            provenance.get("harness_bundle_sha256"), expected["harness_bundle_sha256"]
        ),
        "harness_files": (provenance.get("harness_files"), expected["harness_files"]),
        "lock_sha256": (provenance.get("lock_sha256"), expected["lock_sha256"]),
        "baseline_archive_sha256": (
            provenance.get("baseline_archive_sha256"),
            expected["baseline_archive_sha256"],
        ),
        "manifest_sha256": (
            provenance.get("manifest_sha256"), FROZEN_MANIFEST_SHA256
        ),
        "input_snapshot_sha256": (
            provenance.get("input_snapshot_sha256"), FROZEN_INPUT_SNAPSHOT_SHA256
        ),
        "parameters": (provenance.get("parameters"), expected["parameters"]),
        "snapshot_sha256": (sample.get("snapshot_sha256"), _sha(sample.get("snapshot"))),
        "result_snapshot_sha256": (
            sample.get("result_snapshot_sha256"), _sha(sample.get("result_snapshot"))
        ),
    }
    for name, (actual, required) in checks.items():
        if actual != required:
            errors.append(f"{name}: {actual!r} != {required!r}")
    if errors:
        raise RuntimeError("child evidence validation failed: " + "; ".join(errors))


def _quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {"median": median(ordered), "p10": ordered[max(0, round((len(ordered) - 1) * .10))],
            "p90": ordered[min(len(ordered) - 1, round((len(ordered) - 1) * .90))]}


def _archive(worktree: Path, commit: str, destination: Path) -> str:
    destination.mkdir(parents=True, exist_ok=False)
    archive_path = destination / "archive.tar"
    with archive_path.open("wb") as output:
        subprocess.run(
            ["git", "-c", f"safe.directory={worktree}", "archive", commit],
            cwd=worktree, stdout=output, check=True,
        )
    archive_sha256 = _file_sha256(archive_path)
    shutil.unpack_archive(archive_path, destination, format="tar")
    archive_path.unlink()
    return archive_sha256


def _git_output(worktree: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={worktree}", *args],
        cwd=worktree, capture_output=True, check=True,
    )
    return completed.stdout


def _source_hashes(source_root: Path) -> dict[str, str]:
    return {
        relative: _file_sha256(source_root / relative)
        for relative in ("track/v2/intersect2.py", "track/v2/_geometry.py")
    }


def _harness_fingerprint(runner: Path) -> tuple[dict[str, str], str]:
    files = {
        name: _file_sha256(runner.parent / name)
        for name in HARNESS_FILES
    }
    return files, _sha(files)


def _phase_comparison(samples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    comparison: dict[str, Any] = {
        "diagnostic_only": True,
        "samples": {
            side: {
                "wall_seconds": sample["wall_seconds"],
                "phase_metrics": sample["phase_metrics"],
                "result_snapshot_sha256": sample["result_snapshot_sha256"],
                "source_label": sample["provenance"]["source_label"],
                "source_hashes": sample["provenance"]["source_hashes"],
            }
            for side, sample in samples.items()
        },
        "ratios": {},
    }
    baseline_phases = samples["baseline"]["phase_metrics"]["phases"]
    optimized_phases = samples["optimized"]["phase_metrics"]["phases"]
    for name in baseline_phases:
        baseline = baseline_phases[name]
        optimized = optimized_phases[name]
        comparison["ratios"][name] = {
            "baseline_seconds": baseline["seconds"],
            "optimized_seconds": optimized["seconds"],
            "baseline_over_optimized": (
                baseline["seconds"] / optimized["seconds"]
                if optimized["seconds"] > 0.0 else None
            ),
            "baseline_normalized": {
                key: baseline[key]
                for key in ("per_window_seconds", "per_point_us", "per_segment_pair_ns")
            },
            "optimized_normalized": {
                key: optimized[key]
                for key in ("per_window_seconds", "per_point_us", "per_segment_pair_ns")
            },
        }
    return comparison


def _compact_measurement_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Retain raw metrics/provenance while releasing repeated payload copies."""
    return {
        "run_kind": sample["run_kind"],
        "sys_executable": sample["sys_executable"],
        "python_version": sample["python_version"],
        "wall_seconds": sample["wall_seconds"],
        "peak_working_set_bytes": sample["peak_working_set_bytes"],
        "snapshot_sha256": sample["snapshot_sha256"],
        "result_snapshot_sha256": sample["result_snapshot_sha256"],
        "candidate_count": sample["result_snapshot"]["candidate_count"],
        "pair_count": sample["result_snapshot"]["pair_count"],
        "total_pairs": sample["result_snapshot"]["total_pairs"],
        "provenance": sample["provenance"],
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace a ledger/artifact; never expose a partial JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _compact_phase_sample(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_kind": sample["run_kind"], "wall_seconds": sample["wall_seconds"],
        "peak_working_set_bytes": sample["peak_working_set_bytes"],
        "snapshot_sha256": sample["snapshot_sha256"],
        "result_snapshot_sha256": sample["result_snapshot_sha256"],
        "phase_metrics": sample["phase_metrics"], "provenance": sample["provenance"],
    }


def _write_semantic_sidecar(path: Path, sample: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        raise RuntimeError("semantic sidecar already exists; refuse overwrite")
    payload = {
        "version": "PERF-WINDOWS-R7-SEMANTIC-ATTESTATION-2",
        "snapshot": sample["snapshot"], "result_snapshot": sample["result_snapshot"],
        "snapshot_sha256": sample["snapshot_sha256"],
        "result_snapshot_sha256": sample["result_snapshot_sha256"],
        "provenance": sample["provenance"],
    }
    _atomic_json(path, payload)
    size = path.stat().st_size
    if size > REAL_SEMANTIC_SIDECAR_MAX_BYTES:
        raise RuntimeError("semantic sidecar exceeds bounded evidence cap")
    return {"relative_path": path.name, "sha256": _file_sha256(path), "bytes": size,
            "snapshot_sha256": sample["snapshot_sha256"], "result_snapshot_sha256": sample["result_snapshot_sha256"],
            "candidate_count": sample["result_snapshot"]["candidate_count"], "pair_count": sample["result_snapshot"]["pair_count"],
            "total_pairs": sample["result_snapshot"]["total_pairs"], "provenance": sample["provenance"]}


def _load_semantic_sidecar(header: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    path = Path(header["ledger_root"]) / summary["relative_path"]
    if not path.is_file() or path.stat().st_size > REAL_SEMANTIC_SIDECAR_MAX_BYTES or _file_sha256(path) != summary["sha256"]:
        raise RuntimeError("semantic sidecar missing, oversized, or tampered (fail closed)")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != "PERF-WINDOWS-R7-SEMANTIC-ATTESTATION-2" or value.get("snapshot_sha256") != _sha(value.get("snapshot")) or value.get("result_snapshot_sha256") != _sha(value.get("result_snapshot")):
        raise RuntimeError("semantic sidecar payload hash mismatch (fail closed)")
    for key in ("snapshot_sha256", "result_snapshot_sha256", "provenance"):
        if value.get(key) != summary.get(key):
            raise RuntimeError("semantic sidecar summary mismatch (fail closed)")
    return value


def _real_plan(warmups: int, runs: int) -> list[dict[str, Any]]:
    plan = [
        {"stage": stage, "side": side, "side_index": 1, "run_kind": kind}
        for stage, kind in (("semantic", "semantic"), ("phase0", "phase"))
        for side in ("baseline", "optimized")
    ]
    for index in range(warmups):
        order = ("baseline", "optimized") if index % 2 == 0 else ("optimized", "baseline")
        plan.extend({"stage": "warmup", "side": side, "side_index": index + 1, "run_kind": "measurement"} for side in order)
    for index in range(runs):
        order = ("baseline", "optimized") if index % 2 == 0 else ("optimized", "baseline")
        plan.extend({"stage": "measured", "side": side, "side_index": index + 1, "run_kind": "measurement"} for side in order)
    return plan


def _ledger_digest(value: dict[str, Any]) -> str:
    copy = dict(value)
    copy.pop("ledger_sha256", None)
    return _sha(copy)


def _validate_real_ledger(ledger: dict[str, Any], header: dict[str, Any], plan: list[dict[str, Any]]) -> None:
    if ledger.get("version") != "PERF-WINDOWS-R7-REAL-LEDGER-3":
        raise RuntimeError("real ledger version mismatch (fail closed)")
    if ledger.get("header") != header or ledger.get("plan") != plan:
        raise RuntimeError("real ledger provenance or sample order mismatch (fail closed)")
    if ledger.get("ledger_sha256") != _ledger_digest(ledger):
        raise RuntimeError("real ledger checksum mismatch (fail closed)")
    entries = ledger.get("entries")
    if not isinstance(entries, list) or len(entries) > len(plan):
        raise RuntimeError("real ledger entries malformed (fail closed)")
    for sequence, entry in enumerate(entries, start=1):
        if entry.get("sequence") != sequence or entry.get("plan") != plan[sequence - 1]:
            raise RuntimeError("real ledger sequence mismatch (fail closed)")
        copy = dict(entry)
        entry_sha = copy.pop("entry_sha256", None)
        if entry_sha != _sha(copy):
            raise RuntimeError("real ledger entry checksum mismatch (fail closed)")
        sample = entry.get("sample") or {}
        provenance = sample.get("provenance") or {}
        expected = header["sources"].get(entry["plan"]["side"]) or {}
        for key in ("source_label", "source_commit", "source_hashes", "dirty_diff_sha256", "harness_bundle_sha256", "harness_files", "lock_sha256", "baseline_archive_sha256", "parameters"):
            if provenance.get(key) != expected.get(key):
                raise RuntimeError(f"real ledger child provenance {key} mismatch (fail closed)")
        if sample.get("run_kind") != entry["plan"]["run_kind"]:
            raise RuntimeError("real ledger child run kind mismatch (fail closed)")
        if entry["plan"]["stage"] == "semantic":
            if any(key in sample for key in ("candidate_payload_ordered", "window_sequence", "snapshot", "result_snapshot")):
                raise RuntimeError("semantic ledger entry is not compact (fail closed)")
            _load_semantic_sidecar(header, sample)
        elif entry["plan"]["stage"] == "phase0":
            if any(key in sample for key in ("candidate_payload_ordered", "window_sequence", "snapshot", "result_snapshot")):
                raise RuntimeError("phase ledger entry is not compact (fail closed)")


def _persistent_archive(worktree: Path, commit: str, root: Path) -> tuple[Path, str]:
    archive_path = root.with_suffix(".tar")
    if root.exists() or archive_path.exists():
        raise RuntimeError("persistent baseline archive already exists")
    root.mkdir(parents=True)
    with archive_path.open("wb") as output:
        subprocess.run(["git", "-c", f"safe.directory={worktree}", "archive", commit], cwd=worktree, stdout=output, check=True)
    archive_sha = _file_sha256(archive_path)
    shutil.unpack_archive(archive_path, root, format="tar")
    return root, archive_sha


def _run_real_resumable(
    args: Any, runner: Path, worktree: Path, manifest_path: Path, real_attestation: Path,
    static_header: dict[str, Any], parameters: dict[str, Any], optimized_commit: str,
) -> None:
    """Run real formal samples through an immutable, restart-safe ledger."""
    ledger_path = args.result_dir / "manual_bench_windows_real_r7_ledger.json"
    archive_root = args.result_dir / "manual_bench_windows_real_r7_baseline"
    if ledger_path.exists():
        if not args.resume:
            raise RuntimeError("real ledger exists; use --resume after reviewing it")
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        header = ledger.get("header")
        if not isinstance(header, dict):
            raise RuntimeError("real ledger header missing (fail closed)")
        plan = _real_plan(args.warmups, args.runs)
        _validate_real_ledger(ledger, header, plan)
        archive_path = Path(header["baseline_archive"]["path"])
        if not archive_path.is_file() or _file_sha256(archive_path) != header["baseline_archive"]["sha256"]:
            raise RuntimeError("real ledger baseline archive mismatch (fail closed)")
        baseline_root = Path(header["sources"]["baseline"]["source_root"])
        if not baseline_root.is_dir() or _source_hashes(baseline_root) != header["sources"]["baseline"]["source_hashes"]:
            raise RuntimeError("real ledger baseline source mismatch (fail closed)")
        # Recompute an exact invocation header so stale source/harness/lock/sidecar
        # evidence is never reused across a resume.
        if header["static"] != static_header:
            raise RuntimeError("real ledger current header drift (fail closed)")
        expected_by_side = header["sources"]
    else:
        if args.resume:
            raise RuntimeError("--resume requires an existing real ledger")
        baseline_root, archive_sha = _persistent_archive(worktree, args.baseline_commit, archive_root)
        common = {
            "dirty_diff_sha256": static_header["optimized_dirty_diff_sha256"],
            "harness_bundle_sha256": static_header["harness"]["bundle_sha256"],
            "harness_files": static_header["harness"]["files"],
            "lock_path": static_header["lock"]["path"], "lock_sha256": static_header["lock"]["sha256"],
            "baseline_archive_sha256": archive_sha, "parameters": parameters,
        }
        expected_by_side = {
            "baseline": {**common, "source_label": "baseline", "source_root": str(baseline_root), "source_commit": args.baseline_commit, "source_hashes": _source_hashes(baseline_root)},
            "optimized": {**common, "source_label": "optimized", "source_root": str(worktree), "source_commit": optimized_commit, "source_hashes": _source_hashes(worktree)},
        }
        header = {"static": static_header, "baseline_archive": {"path": str(archive_root.with_suffix('.tar')), "sha256": archive_sha}, "sources": expected_by_side}
        plan = _real_plan(args.warmups, args.runs)
        header["ledger_root"] = str(args.result_dir)
        ledger = {"version": "PERF-WINDOWS-R7-REAL-LEDGER-3", "header": header, "plan": plan, "entries": []}
        ledger["ledger_sha256"] = _ledger_digest(ledger)
        _atomic_json(ledger_path, ledger)

    for sequence in range(len(ledger["entries"]), len(plan)):
        item = plan[sequence]
        sample = _run_child(baseline_root if item["side"] == "baseline" else worktree, runner, "real", item["run_kind"], manifest_path, args.env_file, real_attestation, expected_by_side[item["side"]])
        if item["stage"] == "semantic":
            stored = _write_semantic_sidecar(
                args.result_dir / f"manual_bench_windows_real_semantic_{item['side']}_r7.json", sample,
            )
            stored["run_kind"] = sample["run_kind"]
        elif item["stage"] == "phase0":
            stored = _compact_phase_sample(sample)
        else:
            stored = _compact_measurement_sample(sample)
        entry = {"sequence": sequence + 1, "plan": item, "sample": stored, "child_sample_sha256": _sha(sample)}
        entry["entry_sha256"] = _sha(entry)
        ledger["entries"].append(entry)
        ledger["ledger_sha256"] = _ledger_digest(ledger)
        _atomic_json(ledger_path, ledger)
        if ledger_path.stat().st_size > REAL_LEDGER_MAX_BYTES:
            raise RuntimeError("real ledger exceeds bounded evidence cap")
        print(json.dumps({"ledger": str(ledger_path), "completed": sequence + 1, "total": len(plan)}, sort_keys=True), flush=True)

    _finalize_real_ledger(args, ledger, ledger_path)


def _finalize_real_ledger(args: Any, ledger: dict[str, Any], ledger_path: Path) -> None:
    _validate_real_ledger(ledger, ledger["header"], ledger["plan"])
    entries = ledger["entries"]
    semantic = {
        e["plan"]["side"]: _load_semantic_sidecar(ledger["header"], e["sample"])
        for e in entries if e["plan"]["stage"] == "semantic"
    }
    phase = {e["plan"]["side"]: e["sample"] for e in entries if e["plan"]["stage"] == "phase0"}
    measured = {side: [e["sample"] for e in entries if e["plan"]["stage"] == "measured" and e["plan"]["side"] == side] for side in ("baseline", "optimized")}
    baseline_result = semantic["baseline"]["result_snapshot"]
    baseline_hash = _sha(baseline_result)
    equivalent = semantic["optimized"]["snapshot"] == semantic["baseline"]["snapshot"] and all(
        e["sample"].get("result_snapshot_sha256") == baseline_hash
        for e in entries
    )
    walls = {side: [s["wall_seconds"] for s in values] for side, values in measured.items()}
    rss = {side: [s["peak_working_set_bytes"] for s in values] for side, values in measured.items()}
    wall_summary = {side: _quantiles(values) for side, values in walls.items()}
    rss_summary = {side: _quantiles(values) for side, values in rss.items()}
    speedup = wall_summary["baseline"]["median"] / wall_summary["optimized"]["median"]
    rss_ratio = rss_summary["optimized"]["median"] / rss_summary["baseline"]["median"]
    assertions = {"equivalent": {"required": True, "actual": equivalent, "pass": equivalent}, "speed": {"required_floor": REAL_SPEED_FLOOR, "actual": speedup, "pass": speedup >= REAL_SPEED_FLOOR}, "process_peak_rss": {"required_ratio_ceiling": REAL_RSS_RATIO_CEILING, "actual": rss_ratio, "pass": rss_ratio <= REAL_RSS_RATIO_CEILING, "policy_version": RSS_POLICY_VERSION}, "run_counts": {"required_warmups": 3, "actual_warmups": args.warmups, "required_measured": REAL_MIN_RUNS, "actual_measured": args.runs, "pass": args.warmups >= 3 and args.runs >= REAL_MIN_RUNS}}
    semantic_summary = {
        side: {key: semantic[side][key] for key in ("snapshot_sha256", "result_snapshot_sha256", "provenance")}
        | {"sidecar": next(e["sample"] for e in entries if e["plan"]["stage"] == "semantic" and e["plan"]["side"] == side)}
        for side in ("baseline", "optimized")
    }
    result = {"evidence_version": "PERF-WINDOWS-R7", "case": "real", "formal": True, "provenance_header": {**ledger["header"]["static"], "baseline_archive_sha256": ledger["header"]["baseline_archive"]["sha256"], "sources": ledger["header"]["sources"], "real_ledger_path": str(ledger_path), "real_ledger_sha256": ledger["ledger_sha256"]}, "run_ledger": [{key: entry[key] for key in ("sequence", "plan", "entry_sha256", "child_sample_sha256")} for entry in entries], "semantic_equivalent": semantic["optimized"]["snapshot"] == semantic["baseline"]["snapshot"], "result_equivalent_across_all_runs": equivalent, "equivalent": equivalent, "semantic_proof": semantic_summary, "phase0": _phase_comparison(phase), "samples": measured, "raw_wall_seconds": walls, "raw_peak_working_set_bytes": rss, "wall_seconds": wall_summary, "peak_working_set_bytes": rss_summary, "speedup": speedup, "rss_ratio": rss_ratio, "assertions": assertions, "config": ledger["header"]["static"]["parameters"]}
    output = args.result_dir / "manual_bench_windows_real_r7.json"
    _atomic_json(output, result)
    failed = [name for name, assertion in assertions.items() if not assertion["pass"]]
    if failed:
        raise SystemExit("formal gate failed: " + ", ".join(failed))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--case", choices=("dense", "real"), default="dense")
    parser.add_argument(
        "--run-kind", choices=("measurement", "semantic", "phase"),
        default="measurement",
    )
    parser.add_argument("--baseline-commit", default=BASELINE)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument("--result-dir", type=Path, default=Path("generated/perf_windows"))
    parser.add_argument("--assert-equivalent", action="store_true")
    parser.add_argument("--assert-speedup", type=float)
    parser.add_argument("--assert-rss-reduction", type=float)
    parser.add_argument("--assert-rss-max-ratio", type=float)
    parser.add_argument("--diagnostic-only", action="store_true")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(r"D:\Ai_Projects\track\track\v2\.env"),
    )
    parser.add_argument(
        "--legacy-attestation",
        type=Path,
        default=Path("generated/perf_windows/windows_gate_v1_legacy_attestation.json"),
    )
    parser.add_argument("--legacy-attestation-sha256")
    parser.add_argument("--resume", action="store_true", help="resume only an exact real R7 ledger")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(_child(args.case, args.run_kind), sort_keys=True))
        return

    worktree = Path.cwd().resolve()
    runner = Path(__file__).resolve()
    args.result_dir = args.result_dir.resolve()
    args.env_file = args.env_file.resolve()
    args.legacy_attestation = args.legacy_attestation.resolve()
    manifest_path = (worktree / "generated" / "perf_windows" / "gate_manifest.json").resolve()
    _load_frozen_manifest(manifest_path)
    if args.baseline_commit != BASELINE:
        raise SystemExit(f"baseline commit is frozen at {BASELINE}")
    minimum_warmups = DENSE_MIN_WARMUPS if args.case == "dense" else REAL_MIN_WARMUPS
    minimum_runs = DENSE_MIN_RUNS if args.case == "dense" else REAL_MIN_RUNS
    if args.warmups < minimum_warmups or args.runs < minimum_runs:
        raise SystemExit(
            f"{args.case} requires warmups>={minimum_warmups} and runs>={minimum_runs}"
        )
    speed_floor = DENSE_SPEED_FLOOR if args.case == "dense" else REAL_SPEED_FLOOR
    if args.assert_speedup is not None and abs(args.assert_speedup - speed_floor) > 1e-12:
        raise SystemExit(
            f"{args.case} speed assertion is fixed at {speed_floor:.2f}"
        )
    rss_ceiling = DENSE_RSS_RATIO_CEILING if args.case == "dense" else REAL_RSS_RATIO_CEILING
    if args.case == "dense":
        if args.assert_rss_reduction is not None:
            raise SystemExit("dense uses --assert-rss-max-ratio under RSS-1")
        if (
            args.assert_rss_max_ratio is not None
            and abs(args.assert_rss_max_ratio - DENSE_RSS_RATIO_CEILING) > 1e-12
        ):
            raise SystemExit("dense RSS max ratio is fixed at 1.05 under RSS-1")
    else:
        if args.assert_rss_max_ratio is not None:
            raise SystemExit("real retains --assert-rss-reduction 0.30")
        if (
            args.assert_rss_reduction is not None
            and abs(args.assert_rss_reduction - (1.0 - REAL_RSS_RATIO_CEILING)) > 1e-12
        ):
            raise SystemExit("real RSS reduction assertion is fixed at 0.30 (ratio <= 0.70)")

    harness_files, harness_bundle_sha256 = _harness_fingerprint(runner)
    lock_path = worktree / "uv.lock"
    lock_sha256 = _file_sha256(lock_path)
    dirty_diff = _git_output(
        worktree, "diff", "--binary", args.baseline_commit, "--",
        "track/v2/intersect2.py", "track/v2/_geometry.py",
        "tests/v2/test_intersect2.py",
    )
    dirty_diff_sha256 = hashlib.sha256(dirty_diff).hexdigest()
    dirty_paths = _git_output(worktree, "status", "--short").decode(
        "utf-8", errors="replace"
    ).splitlines()
    optimized_commit = _git_output(worktree, "rev-parse", "HEAD").decode().strip()
    parameters = {
        "baseline_commit": args.baseline_commit,
        "case": args.case,
        "seed": SEED,
        "warmups": args.warmups,
        "runs": args.runs,
        "alternating": True,
        "semantic_runs_per_side": 1,
        "phase_runs_per_side": 1,
        "speed_floor": speed_floor,
        "rss_ratio_ceiling": rss_ceiling,
        "rss_policy_version": RSS_POLICY_VERSION,
        "spatial_window_size": [2000.0, 2000.0],
        "spatial_window_overlap": 0.15,
    }
    static_header = {
        "interpreter": {
            "path": str(Path(sys.executable).resolve()),
            "python_version": sys.version,
            "authoritative": True,
        },
        "gate": {
            "manifest_path": str(manifest_path),
            "manifest_sha256": FROZEN_MANIFEST_SHA256,
            "input_snapshot_sha256": FROZEN_INPUT_SNAPSHOT_SHA256,
            "write_once": True,
        },
        "lock": {"path": str(lock_path), "sha256": lock_sha256},
        "harness": {
            "files": harness_files,
            "bundle_sha256": harness_bundle_sha256,
        },
        "optimized_dirty_diff_sha256": dirty_diff_sha256,
        "dirty_paths": dirty_paths,
        "parameters": parameters,
    }
    real_attestation: Path | None = None
    if args.case == "real":
        if not args.legacy_attestation_sha256:
            raise SystemExit("real requires --legacy-attestation-sha256 (fail closed)")
        parameters["legacy_attestation_sha256"] = args.legacy_attestation_sha256.lower()
        parameters["legacy_attestation_path"] = str(args.legacy_attestation)
        real_attestation = args.result_dir / "manual_bench_windows_real_preflight_r7.json"
        evidence = _real_gate_preflight(
            manifest_path, args.env_file, real_attestation,
            args.legacy_attestation, args.legacy_attestation_sha256,
        )
        evidence["provenance_header"] = {
            **static_header,
            "baseline_archive": {
                "status": "NOT_CREATED",
                "reason": "formal gate validation precedes archive creation",
            },
            "run_ledger": [],
        }
        static_header["legacy_attestation"] = evidence["legacy_attestation"]
        real_attestation.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps({
            "real_preflight": str(real_attestation),
            "formal_valid": evidence["validation"]["formal_valid"],
            "error_codes": evidence["validation"]["error_codes"],
        }, sort_keys=True))
        if args.diagnostic_only:
            return
        if not evidence["validation"]["formal_valid"]:
            raise SystemExit(2)
        _run_real_resumable(
            args, runner, worktree, manifest_path, real_attestation, static_header,
            parameters, optimized_commit,
        )
        return
    elif args.diagnostic_only:
        raise SystemExit("--diagnostic-only is valid only with --case real")

    ledger: list[dict[str, Any]] = []
    sequence = 0

    with tempfile.TemporaryDirectory(prefix="perf-windows-baseline-") as tmp:
        baseline_root = Path(tmp) / "baseline"
        archive_sha256 = _archive(worktree, args.baseline_commit, baseline_root)
        common_expected = {
            "dirty_diff_sha256": dirty_diff_sha256,
            "harness_bundle_sha256": harness_bundle_sha256,
            "harness_files": harness_files,
            "lock_path": str(lock_path),
            "lock_sha256": lock_sha256,
            "baseline_archive_sha256": archive_sha256,
            "parameters": parameters,
        }
        expected_by_side = {
            "baseline": {
                **common_expected,
                "source_label": "baseline",
                "source_root": str(baseline_root),
                "source_commit": args.baseline_commit,
                "source_hashes": _source_hashes(baseline_root),
            },
            "optimized": {
                **common_expected,
                "source_label": "optimized",
                "source_root": str(worktree),
                "source_commit": optimized_commit,
                "source_hashes": _source_hashes(worktree),
            },
        }

        def execute(side: str, run_kind: str, stage: str, side_index: int) -> dict[str, Any]:
            nonlocal sequence
            root = baseline_root if side == "baseline" else worktree
            sample = _run_child(
                root, runner, args.case, run_kind, manifest_path, args.env_file,
                real_attestation, expected_by_side[side],
            )
            sequence += 1
            ledger.append({
                "sequence": sequence,
                "stage": stage,
                "side": side,
                "side_index": side_index,
                "run_kind": run_kind,
                "sample_sha256": _sha(sample),
                "snapshot_sha256": sample["snapshot_sha256"],
                "result_snapshot_sha256": sample["result_snapshot_sha256"],
                "wall_seconds": sample["wall_seconds"],
                "peak_working_set_bytes": sample["peak_working_set_bytes"],
                "validated": True,
            })
            return sample

        semantic_samples = {
            side: execute(side, "semantic", "semantic", 1)
            for side in ("baseline", "optimized")
        }
        baseline_semantic = semantic_samples["baseline"]["snapshot"]
        semantic_equivalent = semantic_samples["optimized"]["snapshot"] == baseline_semantic
        phase_samples = {
            side: execute(side, "phase", "phase0", 1)
            for side in ("baseline", "optimized")
        }

        warmup_result_hashes: dict[str, list[str]] = {"baseline": [], "optimized": []}
        for warmup_index in range(args.warmups):
            order = (
                ("baseline", "optimized")
                if warmup_index % 2 == 0 else ("optimized", "baseline")
            )
            for side in order:
                sample = execute(side, "measurement", "warmup", warmup_index + 1)
                warmup_result_hashes[side].append(sample["result_snapshot_sha256"])
        samples: dict[str, list[dict[str, Any]]] = {"baseline": [], "optimized": []}
        for run_index in range(args.runs):
            order = (
                ("baseline", "optimized")
                if run_index % 2 == 0 else ("optimized", "baseline")
            )
            for side in order:
                sample = execute(side, "measurement", "measured", run_index + 1)
                samples[side].append(_compact_measurement_sample(sample))

        baseline_result = semantic_samples["baseline"]["result_snapshot"]
        baseline_result_sha256 = _sha(baseline_result)
        result_equivalent = all(
            sample["result_snapshot_sha256"] == baseline_result_sha256
            for side_samples in samples.values()
            for sample in side_samples
        ) and all(
            sample["result_snapshot"] == baseline_result
            for sample in phase_samples.values()
        ) and all(
            result_hash == baseline_result_sha256
            for hashes in warmup_result_hashes.values()
            for result_hash in hashes
        )
        equivalent = semantic_equivalent and result_equivalent
        source_header = {
            side: {
                key: value
                for key, value in expected.items()
                if key not in {"parameters", "harness_files"}
            }
            for side, expected in expected_by_side.items()
        }

    walls = {side: [item["wall_seconds"] for item in items] for side, items in samples.items()}
    rss = {side: [item["peak_working_set_bytes"] for item in items] for side, items in samples.items()}
    wall_summary = {side: _quantiles(values) for side, values in walls.items()} if equivalent else None
    rss_summary = {side: _quantiles(values) for side, values in rss.items()} if equivalent else None
    speedup = (
        wall_summary["baseline"]["median"] / wall_summary["optimized"]["median"]
        if wall_summary is not None else None
    )
    rss_ratio = (
        rss_summary["optimized"]["median"] / rss_summary["baseline"]["median"]
        if rss_summary is not None else None
    )
    args.result_dir.mkdir(parents=True, exist_ok=True)
    snapshot_paths: dict[str, str] = {}
    snapshot_hashes: dict[str, str] = {}
    for side, sample in semantic_samples.items():
        path = args.result_dir / f"manual_bench_windows_{args.case}_{side}_snapshot_r7.json"
        path.write_text(
            json.dumps(sample["snapshot"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        snapshot_paths[side] = str(path)
        snapshot_hashes[side] = _file_sha256(path)

    assertions = {
        "equivalent": {"required": True, "actual": equivalent, "pass": equivalent},
        "speed": {
            "required_floor": speed_floor,
            "actual": speedup,
            "pass": speedup is not None and speedup >= speed_floor,
        },
        "process_peak_rss": {
            "required_ratio_ceiling": rss_ceiling,
            "actual": rss_ratio,
            "pass": rss_ratio is not None and rss_ratio <= rss_ceiling,
            "metric": "Windows GetProcessMemoryInfo PeakWorkingSetSize median raw bytes",
            "policy_version": RSS_POLICY_VERSION,
        },
        "run_counts": {
            "required_warmups": minimum_warmups,
            "actual_warmups": args.warmups,
            "required_measured": minimum_runs,
            "actual_measured": args.runs,
            "pass": args.warmups >= minimum_warmups and args.runs >= minimum_runs,
        },
    }
    implementation_or_evidence_failure = not (
        assertions["equivalent"]["pass"] and assertions["run_counts"]["pass"]
    )
    hard_threshold_failures = [
        name for name in ("speed", "process_peak_rss")
        if assertions[name]["actual"] is not None and not assertions[name]["pass"]
    ]
    result = {
        "evidence_version": "PERF-WINDOWS-R7",
        "case": args.case,
        "formal": True,
        "provenance_header": {
            **static_header,
            "baseline_archive_sha256": source_header["baseline"]["baseline_archive_sha256"],
            "sources": source_header,
        },
        "run_ledger": ledger,
        "semantic_equivalent": semantic_equivalent,
        "result_equivalent_across_all_runs": result_equivalent,
        "equivalent": equivalent,
        "semantic_snapshots": {
            "paths": snapshot_paths,
            "file_sha256": snapshot_hashes,
            "semantic_sha256": {
                side: sample["snapshot_sha256"]
                for side, sample in semantic_samples.items()
            },
        },
        "phase0": _phase_comparison(phase_samples),
        "samples": {
            side: side_samples for side, side_samples in samples.items()
        },
        "raw_wall_seconds": walls,
        "raw_peak_working_set_bytes": rss,
        "wall_seconds": wall_summary,
        "peak_working_set_bytes": rss_summary,
        "speedup": speedup,
        "rss_ratio": rss_ratio,
        "assertions": assertions,
        "failure_classification": {
            "implementation_or_evidence_failure": implementation_or_evidence_failure,
            "hard_threshold_failures": hard_threshold_failures,
            "rss_threshold_only": (
                hard_threshold_failures == ["process_peak_rss"]
                and not implementation_or_evidence_failure
            ),
            "rss_floor_diagnostic_required_for_floor_attribution": True,
        },
        "config": parameters,
    }
    output = args.result_dir / f"manual_bench_windows_{args.case}_r7.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(output), "equivalent": equivalent, "speedup": speedup, "rss_ratio": rss_ratio}, sort_keys=True))
    failed = [name for name, assertion in assertions.items() if not assertion["pass"]]
    if failed:
        raise SystemExit("formal gate failed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
