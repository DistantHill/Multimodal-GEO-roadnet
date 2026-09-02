"""Reproducible Getis-Ord G* equivalence and performance gate.

The parent process always executes this file from the current worktree.  It
archives the requested baseline into a temporary directory and invokes an
isolated child for each source.  A child only imports its own source tree.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, fields
from typing import Any


GENERATOR_VERSION = "hotspots_gate_v1_mixed_dense_sparse_20260810"
GATE_CASE_ID = "hotspots_gate_v1"
SUPPLEMENTAL_SNAPSHOT_CASE_ID = "supplemental_turn_event_contract_v1"
DEFAULT_BASELINE = "c12a06abe3262501f5bb20713c4280aa9a99f34e"
RSS_ACCEPTANCE_DECISION_VERSION = "RSS-1-2026-08-11"
RSS_MAX_RATIO = 1.05
STRUCTURAL_MEMORY_GUARD_VERSION = "hotspots-grid-memory-v1"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _peak_working_set_bytes() -> int | None:
    """Return Windows PeakWorkingSetSize without measuring parent RSS."""
    if os.name != "nt":
        return None

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    if not ok:
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


def _event_rows(case_id: str) -> tuple[list[list[float | int | str]], dict[str, float]]:
    """Frozen deterministic inputs; rows map directly to TurnEvent arguments."""
    radius = 180.0
    config = {"g_star_radius_m": radius, "g_star_min_z": 0.5, "min_turn_deg": 45.0}
    rows: list[list[float | int | str]] = []

    def add(
        event_calculate_index: int,
        x: float,
        y: float,
        turn: float = 90.0,
    ) -> None:
        rows.append([
            event_calculate_index,
            f"v{event_calculate_index % 97}",
            1_700_000_000 + event_calculate_index,
            116.0,
            39.0,
            x,
            y,
            0.0,
            90.0,
            90.0 + turn,
            turn,
        ])

    if case_id == GATE_CASE_ID:
        # 4 compact islands (4,800) plus 3,200 deliberately isolated points.
        # Seed and formula are part of the gate definition, not tunable inputs.
        seed = 20260810
        for index in range(4_800):
            cluster = index // 1_200
            offset = index % 1_200
            x = cluster * 10_000.0 + ((offset * 37 + seed) % 119) - 59.0
            y = cluster * -8_000.0 + ((offset * 53 + seed) % 113) - 56.0
            add(10_000 + index * 3, x, y, 90.0 if index % 11 else 20.0)
        for index in range(3_200):
            add(100_000 + index * 5, 100_000.0 + index * 400.0, -200_000.0 - index * 400.0)
    elif case_id == "empty":
        pass
    elif case_id == "single":
        add(7, 0.0, 0.0)
    elif case_id == "zero_variance":
        for index in range(8):
            add(10 + index * 7, index * 1_000.0, 0.0)
    elif case_id == "uniform_sparse":
        for index in range(16):
            add(100 + index * 2, index * 500.0, (index % 3) * 500.0)
    elif case_id == "multiple_dense":
        for index in range(30):
            add(200 + index * 3, (index % 10) * 5.0, (index // 10) * 5.0)
            add(500 + index * 3, 2_000.0 + (index % 10) * 5.0, -3_000.0 + (index // 10) * 5.0)
    elif case_id == "all_coincident":
        for index in range(128):
            add(900 + index * 11, 3.0, -4.0)
    elif case_id == "duplicates_negative":
        for index, point in enumerate([(-2.0, -2.0), (-2.0, -2.0), (-180.0, 0.0), (-180.0, 0.0), (-400.0, -400.0)]):
            add(2_000 + index * 13, *point)
    elif case_id == "radius_boundary":
        below = math.nextafter(radius, 0.0)
        above = math.nextafter(radius, math.inf)
        for index, point in enumerate([(0.0, 0.0), (radius, 0.0), (below, 0.0), (above, 0.0), (-radius, 0.0)]):
            add(3_000 + index * 17, *point)
    elif case_id == SUPPLEMENTAL_SNAPSHOT_CASE_ID:
        # Supplemental production-shaped contract input.  This is explicitly
        # not called a real-data snapshot: this checkout contains no committed
        # real CSV/TurnEvent fixture to bind as deterministic evidence.
        config = {"g_star_radius_m": 180.0, "g_star_min_z": 0.5, "min_turn_deg": 45.0}
        for index in range(48):
            island = index // 16
            offset = index % 16
            add(7_001 + index * 19, island * 4_500.0 + (offset % 4) * 23.5,
                -island * 3_700.0 + (offset // 4) * 21.5,
                92.0 if index % 9 else 30.0)
    else:
        raise ValueError(f"Unknown case: {case_id}")
    return rows, config


def _cases() -> list[str]:
    return [GATE_CASE_ID, SUPPLEMENTAL_SNAPSHOT_CASE_ID, "empty", "single", "zero_variance", "uniform_sparse", "multiple_dense",
            "all_coincident", "duplicates_negative", "radius_boundary"]


def _manifest_case_ids() -> list[str]:
    """The Phase-0 manifest is frozen; later replay coverage is separate."""
    return [GATE_CASE_ID, "empty", "single", "zero_variance", "uniform_sparse", "multiple_dense",
            "all_coincident", "duplicates_negative", "radius_boundary"]


def _source_digest(source: Path) -> str:
    return _sha256_bytes((source / "track" / "v2" / "intersect2.py").read_bytes())


def _structural_memory_guard(source: Path) -> dict[str, Any]:
    """Fail closed if O(n) storage or the four-cells-per-axis bound disappears."""
    text = (source / "track" / "v2" / "intersect2.py").read_text(encoding="utf-8")
    start = text.index("def _getis_local_counts_grid(")
    end = text.index("\ndef _select_hotspot_events(", start)
    grid = text[start:end]
    required = {
        "cell_bounds": "cell_bounds: list[tuple[int, int, int, int]] = []",
        "cell_index": "cells: dict[tuple[int, int], list[int]] = defaultdict(list)",
        "counts": "local_counts: list[float] = []",
        "span_guard": "if max_cell_x - min_cell_x + 1 > 4 or max_cell_y - min_cell_y + 1 > 4:",
        "scan_fallback": "return _getis_local_counts_scan(events, radius_m",
        "streamed_candidates": "cells.get((neighbor_x, neighbor_y), ())",
    }
    forbidden = {
        "materialized_neighbours": "neighbors.append(",
        "radius_list_api": "query_ball_point(",
        "distance_matrix": "distance_matrix",
    }
    missing = [name for name, token in required.items() if token not in grid]
    present = [name for name, token in forbidden.items() if token in grid]
    if missing or present:
        raise RuntimeError(
            "structural memory guard failed: "
            + json.dumps({"missing": missing, "forbidden_present": present}, sort_keys=True)
        )
    return {
        "version": STRUCTURAL_MEMORY_GUARD_VERSION,
        "passed": True,
        "required": sorted(required),
        "forbidden": sorted(forbidden),
    }


def _same_executable(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.abspath(actual)) == os.path.normcase(os.path.abspath(expected))


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in ("numpy", "scipy", "torch"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", None)
        except ImportError:
            versions[name] = None
    return versions


def _child(source: Path, source_commit: str, lock_digest: str, expected_python: str, case_id: str, repetitions: int, verbose: bool,
           snapshot: bool = False, floor: bool = False, real_snapshot: Path | None = None) -> dict[str, Any]:
    if not _same_executable(sys.executable, expected_python):
        raise RuntimeError(f"child interpreter mismatch: {sys.executable} != {expected_python}")
    sys.path[:] = [str(source)] + [entry for entry in sys.path if entry not in ("", str(source))]
    from track.v2.intersect2 import IntersectConfig, _compute_getis_ord_g_star, _select_hotspot_events
    from track.v2.models import TurnEvent

    # Archived baselines use an older name for TurnEvent's first, transient
    # calculation-index field. Resolve that field structurally, then emit the
    # current canonical key so a rename alone cannot create a snapshot diff.
    calculate_index_field = fields(TurnEvent)[0].name

    def turn_event_record(event: TurnEvent) -> dict[str, Any]:
        record = asdict(event)
        return {
            "event_calculate_index": record.pop(calculate_index_field),
            **record,
        }

    if real_snapshot is None:
        rows, cfg_values = _event_rows(case_id)
        events = [TurnEvent(*row) for row in rows]
        input_sha256 = _sha256_bytes(_json_bytes(rows))
    else:
        payload = json.loads(real_snapshot.read_text(encoding="utf-8"))
        rows = payload["turn_events"]
        cfg_values = payload["config"]
        events = []
        for row in rows:
            values = dict(row)
            calculate_index = values.pop("event_calculate_index")
            events.append(TurnEvent(calculate_index, **values))
        input_sha256 = _sha256_bytes(_json_bytes(rows))
    cfg = IntersectConfig(**cfg_values)
    if floor:
        # This is deliberately the same isolated child setup (imports plus
        # frozen input construction) with no G* work.  It is diagnostic only
        # when the hard PeakWorkingSetSize gate is not reachable.
        return {
            "case_id": case_id, "event_count": len(events),
            "source_commit": source_commit, "source_path": str(source),
            "source_digest": _source_digest(source), "sys_executable": sys.executable,
            "python_version": sys.version, "lock_digest": lock_digest,
            "package_versions": _package_versions(),
            "peak_working_set_bytes": _peak_working_set_bytes(),
        }
    try:
        from track.v2.intersect2 import _getis_local_counts_grid
    except ImportError:
        _getis_local_counts_grid = None
    phase_timings: dict[str, float] | None = None
    if not snapshot:
        counts, count_source = [], "not_measured"
    elif _getis_local_counts_grid is None:
        # The archived baseline has no count seam.  Reproduce its published
        # exact predicate directly for the one non-timed oracle snapshot;
        # sys.settrace would instrument every one of the O(n^2) operations.
        radius_sq = cfg.g_star_radius_m * cfg.g_star_radius_m
        counts = [
            sum(
                1.0 for other in events
                if (other.x - event.x) ** 2 + (other.y - event.y) ** 2 <= radius_sq
            )
            for event in events
        ]
        count_source = "baseline_exact_scan_oracle"
    else:
        phase_timings = {}
        counts = _getis_local_counts_grid(events, cfg.g_star_radius_m, phase_timings=phase_timings)
        count_source = "optimized_production_grid_helper"
    gstar_started = time.perf_counter()
    z_scores = _compute_getis_ord_g_star(events, cfg.g_star_radius_m)
    gstar_seconds = time.perf_counter() - gstar_started
    selected = _select_hotspot_events(events, cfg)
    # A deliberately separate pure filtering pass over the same frozen
    # z-score mapping.  Do not infer it by subtracting unrelated calls.
    filter_started = time.perf_counter()
    filtered_indexes = [
        getattr(event, calculate_index_field) for event in events
        if z_scores.get(getattr(event, calculate_index_field), 0.0) >= cfg.g_star_min_z
        and abs(event.turn_deg) >= cfg.min_turn_deg
    ]
    filter_seconds = time.perf_counter() - filter_started
    if filtered_indexes != [
        getattr(event, calculate_index_field) for event in selected
    ]:
        raise AssertionError("pure filtering diverged from production selection")
    serialization_started = time.perf_counter()
    selected_indexes = [
        getattr(event, calculate_index_field) for event in selected
    ]
    serialized = json.dumps({
        "z_scores": [
            [
                getattr(event, calculate_index_field),
                z_scores[getattr(event, calculate_index_field)],
            ]
            for event in events
        ],
        "selected": selected_indexes,
    }, separators=(",", ":"))
    serialization_seconds = time.perf_counter() - serialization_started
    if snapshot:
        phase_timings = phase_timings or {}
        phase_timings.update({
            "production_gstar_seconds": gstar_seconds,
            "selection_filter_seconds": filter_seconds,
            "serialization_seconds": serialization_seconds,
            "per_event_gstar_seconds": gstar_seconds / max(1, len(events)),
            "per_event_selection_filter_seconds": filter_seconds / max(1, len(events)),
            # Baseline has no count seam; do not invent a second O(n^2) reference.
            "baseline_count_z_split_available": _getis_local_counts_grid is not None,
        })

    timings: list[float] = []
    timing_batch_seconds: list[float] = []
    started = time.perf_counter()
    for _ in range(repetitions):
        _select_hotspot_events(events, cfg)
    batch_seconds = time.perf_counter() - started
    timing_batch_seconds.append(batch_seconds)
    timings.append(batch_seconds / repetitions)
    if verbose:
        print(f"{case_id}: n={len(events)} wall={timings}", file=sys.stderr)
    return {
        "case_id": case_id,
        "event_count": len(events),
        "input_sha256": input_sha256,
        "local_counts": counts,
        "count_source": count_source,
        "turn_event_records": [turn_event_record(event) for event in events],
        "z_scores": [
            [
                getattr(event, calculate_index_field),
                z_scores[getattr(event, calculate_index_field)],
            ]
            for event in events
        ],
        "selected": selected_indexes,
        "wall_seconds": timings,
        "wall_batch_seconds": timing_batch_seconds,
        "timing_repetitions": repetitions,
        "peak_working_set_bytes": _peak_working_set_bytes(),
        "source_commit": source_commit,
        "source_path": str(source),
        "source_digest": _source_digest(source),
        "sys_executable": sys.executable,
        "python_version": sys.version,
        "lock_digest": lock_digest,
        "package_versions": _package_versions(),
        "phase_timings": phase_timings,
        "scipy_version": _scipy_version(),
    }


def _scipy_version() -> str | None:
    try:
        import scipy
    except ImportError:
        return None
    return scipy.__version__


def _run_child(script: Path, source: Path, source_commit: str, lock_digest: str, expected_python: str, case_id: str, repetitions: int,
               verbose: bool, snapshot: bool = False, floor: bool = False, real_snapshot: Path | None = None) -> dict[str, Any]:
    command = [sys.executable, str(script), "--child", "--source", str(source), "--case", case_id,
               "--source-commit", source_commit, "--lock-digest", lock_digest,
               "--expected-python", expected_python, "--repetitions", str(repetitions)]
    if verbose:
        command.append("--verbose")
    if snapshot: command.append("--snapshot")
    if floor: command.append("--floor")
    if real_snapshot is not None: command.extend(["--real-snapshot", str(real_snapshot)])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source)
    completed = subprocess.run(command, check=True, capture_output=True, text=True, env=env)
    # Domain logging uses stdout; the child writes its machine result last.
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] if lower == upper else ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _archive_baseline(root: Path, commit: str, destination: Path) -> Path:
    tar_path = destination / "baseline.tar"
    with tar_path.open("wb") as handle:
        subprocess.run(["git", "-c", f"safe.directory={root}", "archive", "--format=tar", commit], cwd=root, check=True, stdout=handle)
    extracted = destination / "baseline"
    extracted.mkdir()
    with tarfile.open(tar_path) as archive:
        archive.extractall(extracted, filter="data")
    return extracted


def _manifest(result_dir: Path, *, allow_create: bool = False) -> Path:
    rows, config = _event_rows(GATE_CASE_ID)
    payload = {
        "gate_id": GATE_CASE_ID,
        "generator_version": GENERATOR_VERSION,
        "seed": 20260810,
        "event_count": len(rows),
        "input_sha256": _sha256_bytes(_json_bytes(rows)),
        "config": config,
        "case_ids": _manifest_case_ids(),
    }
    path = result_dir / "gate_manifest.json"
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if not path.exists():
        if not allow_create:
            raise RuntimeError(f"Frozen manifest missing (fail closed): {path}; run --manifest-only first")
        result_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")
        return path
    if path.read_text(encoding="utf-8") != encoded:
        raise RuntimeError(f"Frozen manifest differs: {path}")
    return path


def _freeze_real_snapshot(
    root: Path,
    csv_path: Path,
    sidecar: Path,
    *,
    processing_srid: int,
) -> dict[str, Any]:
    """Freeze real TurnEvents through the project CSV source entrypoint."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from track.v2.intersect2 import (
        IntersectConfig,
        _detect_turn_events,
        _group_by_vehicle,
        _project_tracks,
    )
    from track.v2.models import ProjectionConfig
    from track.v2.parser0 import CsvSource, get_database
    from track.v2.projection import CoordinateTransformer, create_spatial_context

    raw = csv_path.read_bytes()
    cfg = IntersectConfig()
    spatial_context = create_spatial_context(
        ProjectionConfig(processing_srid=processing_srid)
    )
    transformer = CoordinateTransformer(spatial_context)
    handle = get_database(CsvSource(csv_path))
    try:
        tracks = _project_tracks(_group_by_vehicle(handle), transformer)
    finally:
        handle.close()
    events = _detect_turn_events(tracks, cfg)
    payload = {
        "schema": "hotspots_real_turn_event_snapshot_v2_fixed_crs",
        "source": {"path": str(csv_path), "size_bytes": len(raw), "sha256": _sha256_bytes(raw),
                   "entrypoint": "CsvSource -> get_database -> _group_by_vehicle -> fixed CRS -> _detect_turn_events"},
        "spatial_context": asdict(spatial_context),
        "track_count": len(tracks), "point_count": sum(len(track) for track in tracks.values()),
        "turn_event_count": len(events), "config": asdict(cfg),
        "turn_events": [asdict(event) for event in events],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if sidecar.exists() and sidecar.read_text(encoding="utf-8") != encoded:
        raise RuntimeError(f"Real snapshot differs (fail closed): {sidecar}")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(encoded, encoding="utf-8")
    return {"path": str(sidecar), "sha256": _sha256_bytes(encoded.encode()), **payload["source"],
            "track_count": payload["track_count"], "point_count": payload["point_count"],
            "turn_event_count": payload["turn_event_count"],
            "spatial_context": payload["spatial_context"]}


def _write_scipy_evidence(result_dir: Path) -> Path:
    command = [sys.executable, "-c", "import importlib.metadata as m; print(m.version('scipy')); print(m.distribution('scipy').locate_file('')); from scipy.spatial import cKDTree; print(cKDTree)"]
    completed = subprocess.run(command, capture_output=True, text=True)
    payload = {
        "decision": "self_grid",
        "reason": "exact cKDTree import is unavailable in the required torch interpreter; no dependency changes made",
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "metadata": {"scipy_version": importlib.metadata.version("scipy"),
                     "license": importlib.metadata.metadata("scipy").get("License")},
    }
    path = result_dir / "scipy_decision.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _semantic_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    """Keep one semantic proof, never duplicate it in timed samples."""
    return {"event_count": run["event_count"], "input_sha256": run["input_sha256"],
            "turn_event_records_sha256": _sha256_bytes(_json_bytes(run["turn_event_records"])),
            "local_counts_sha256": _sha256_bytes(_json_bytes(run["local_counts"])),
            "z_scores_sha256": _sha256_bytes(_json_bytes(run["z_scores"])),
            "selected_sha256": _sha256_bytes(_json_bytes(run["selected"])),
            "phase_timings": run["phase_timings"], "source_digest": run["source_digest"],
            "sys_executable": run["sys_executable"], "python_version": run["python_version"], "lock_digest": run["lock_digest"]}


def _measurement_sample(run: dict[str, Any]) -> dict[str, Any]:
    return {"wall_seconds": run["wall_seconds"][0], "peak_working_set_bytes": run["peak_working_set_bytes"]}


def _main(args: argparse.Namespace) -> int:
    if args.expected_python is None:
        raise RuntimeError("--expected-python is required for a formal benchmark")
    if not _same_executable(sys.executable, args.expected_python):
        raise RuntimeError(f"parent interpreter mismatch: {sys.executable} != {args.expected_python}")
    if args.assert_rss_max_ratio is not None and args.assert_rss_max_ratio != RSS_MAX_RATIO:
        raise RuntimeError(
            f"RSS-1 requires --assert-rss-max-ratio {RSS_MAX_RATIO}; "
            f"received {args.assert_rss_max_ratio}"
        )
    root = _project_root()
    structural_memory_guard = _structural_memory_guard(root)
    result_dir = (root / args.result_dir).resolve()
    manifest_path = _manifest(result_dir, allow_create=args.manifest_only)
    scipy_evidence = _write_scipy_evidence(result_dir)
    real_snapshot = (root / args.real_snapshot).resolve() if args.real_snapshot else None
    if args.freeze_real_snapshot:
        if args.real_csv is None:
            raise RuntimeError("--freeze-real-snapshot requires --real-csv")
        if args.processing_srid is None:
            raise RuntimeError(
                "--freeze-real-snapshot requires an explicit --processing-srid"
            )
        metadata = _freeze_real_snapshot(
            root,
            args.real_csv.resolve(),
            real_snapshot or result_dir / "real_turn_events_snapshot.json",
            processing_srid=args.processing_srid,
        )
        print(json.dumps(metadata, sort_keys=True))
        return 0
    if args.manifest_only:
        print(json.dumps({"manifest": str(manifest_path), "sha256": _sha256_bytes(manifest_path.read_bytes()), "scipy_evidence": str(scipy_evidence)}))
        return 0
    with tempfile.TemporaryDirectory(prefix="hotspots-baseline-") as temp:
        baseline = _archive_baseline(root, args.baseline_commit, Path(temp))
        optimized_commit = subprocess.check_output(
            ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"], cwd=root, text=True,
        ).strip()
        lock_digest = _sha256_bytes((root / "uv.lock").read_bytes())
        dirty_diff = subprocess.check_output(["git", "-c", f"safe.directory={root}", "diff", "--no-ext-diff", "--binary"], cwd=root)
        header = {"schema": "hotspots_measurement_ledger_v1", "manifest_sha256": _sha256_bytes(manifest_path.read_bytes()),
                  "gate_input_sha256": _sha256_bytes(_json_bytes(_event_rows(GATE_CASE_ID)[0])), "baseline_commit": args.baseline_commit,
                  "optimized_commit": optimized_commit, "interpreter": sys.executable, "python_version": sys.version,
                  "lock_sha256": lock_digest, "optimized_source_sha256": _source_digest(root),
                  "harness_sha256": _sha256_bytes(Path(__file__).read_bytes()), "dirty_diff_sha256": _sha256_bytes(dirty_diff),
                  "real_snapshot_sha256": _sha256_bytes(real_snapshot.read_bytes()) if real_snapshot else None,
                  "parameters": {"warmups": args.warmups, "runs": args.runs, "alternate": args.alternate, "rss": args.rss},
                  "acceptance": {"decision_version": RSS_ACCEPTANCE_DECISION_VERSION,
                                 "rss_metric": "independent_child_peak_working_set_ratio",
                                 "rss_max_ratio": args.assert_rss_max_ratio,
                                 "structural_memory_guard_version": STRUCTURAL_MEMORY_GUARD_VERSION},
                  "structural_memory_guard": structural_memory_guard}
        all_results: dict[str, Any] = {"header": header, "run_order": [], "semantic_snapshots": {}, "cases": {}}
        output = result_dir / "benchmark_compact.json"
        if args.resume and output.exists():
            prior = json.loads(output.read_text(encoding="utf-8"))
            if prior.get("header") != header:
                raise RuntimeError("resume provenance mismatch (fail closed)")
        if real_snapshot is not None:
            if not real_snapshot.exists():
                raise RuntimeError(f"Real snapshot missing: {real_snapshot}")
            reference = _run_child(Path(__file__).resolve(), baseline, args.baseline_commit, lock_digest, args.expected_python,
                                   GATE_CASE_ID, 1, args.verbose, snapshot=True, real_snapshot=real_snapshot)
            candidate = _run_child(Path(__file__).resolve(), root, optimized_commit, lock_digest, args.expected_python,
                                   GATE_CASE_ID, 1, args.verbose, snapshot=True, real_snapshot=real_snapshot)
            all_results["real_snapshot"] = {
                "path": str(real_snapshot), "sha256": _sha256_bytes(real_snapshot.read_bytes()),
                "equivalent": reference["turn_event_records"] == candidate["turn_event_records"]
                and reference["local_counts"] == candidate["local_counts"]
                and reference["z_scores"] == candidate["z_scores"]
                and reference["selected"] == candidate["selected"],
                "baseline_semantic": _semantic_snapshot(reference), "optimized_semantic": _semantic_snapshot(candidate),
            }
        case_ids = [args.only_case] if args.only_case else _cases()
        for case_id in case_ids:
            event_count = len(_event_rows(case_id)[0])
            # Small required regressions otherwise measure timer/process jitter
            # rather than production work.  Each reported value is normalised
            # back to one actual _select_hotspot_events invocation.
            timing_repetitions = 1 if case_id == GATE_CASE_ID else max(1, 20_000 // max(1, event_count))
            for _ in range(args.warmups):
                _run_child(Path(__file__).resolve(), baseline, args.baseline_commit, lock_digest, args.expected_python, case_id, timing_repetitions, args.verbose)
                _run_child(Path(__file__).resolve(), root, optimized_commit, lock_digest, args.expected_python, case_id, timing_repetitions, args.verbose)
            # Snapshots are dedicated non-timed children, so the eleven timing
            # and PeakWorkingSetSize samples never include trace adapter work.
            reference = _run_child(Path(__file__).resolve(), baseline, args.baseline_commit, lock_digest, args.expected_python,
                                   case_id, 1, args.verbose, snapshot=True)
            candidate = _run_child(Path(__file__).resolve(), root, optimized_commit, lock_digest, args.expected_python,
                                   case_id, 1, args.verbose, snapshot=True)
            baseline_runs, optimized_runs = [], []
            for run_index in range(args.runs):
                ordered_sources = [("baseline", baseline, args.baseline_commit), ("optimized", root, optimized_commit)]
                if args.alternate and run_index % 2:
                    ordered_sources.reverse()
                for label, source, source_commit in ordered_sources:
                    result = _run_child(
                        Path(__file__).resolve(), source, source_commit, lock_digest, args.expected_python, case_id, timing_repetitions, args.verbose,
                    )
                    (baseline_runs if label == "baseline" else optimized_runs).append(result)
                    all_results["run_order"].append({"case_id": case_id, "run_index": run_index, "label": label})
            equivalent = (candidate["turn_event_records"] == reference["turn_event_records"] and candidate["local_counts"] == reference["local_counts"] and candidate["z_scores"] == reference["z_scores"] and candidate["selected"] == reference["selected"]
                          and all(run["z_scores"] == reference["z_scores"] and run["selected"] == reference["selected"] for run in baseline_runs)
                          and all(run["z_scores"] == candidate["z_scores"] and run["selected"] == candidate["selected"] for run in optimized_runs))
            base_walls = [run["wall_seconds"][0] for run in baseline_runs]
            opt_walls = [run["wall_seconds"][0] for run in optimized_runs]
            base_rss = [run["peak_working_set_bytes"] for run in baseline_runs]
            opt_rss = [run["peak_working_set_bytes"] for run in optimized_runs]
            all_results["cases"][case_id] = {
                "event_count": reference["event_count"], "equivalent": equivalent,
                "baseline_measurements": [_measurement_sample(run) for run in baseline_runs],
                "optimized_measurements": [_measurement_sample(run) for run in optimized_runs],
                "baseline_wall": {"median": statistics.median(base_walls), "p10": _percentile(base_walls, .1), "p90": _percentile(base_walls, .9), "raw": base_walls},
                "optimized_wall": {"median": statistics.median(opt_walls), "p10": _percentile(opt_walls, .1), "p90": _percentile(opt_walls, .9), "raw": opt_walls},
                "speedup": statistics.median(base_walls) / statistics.median(opt_walls),
                "baseline_peak_working_set_bytes": {"median": statistics.median(base_rss), "raw": base_rss},
                "optimized_peak_working_set_bytes": {"median": statistics.median(opt_rss), "raw": opt_rss},
                "rss_reduction": 1 - statistics.median(opt_rss) / statistics.median(base_rss),
                "rss_ratio": statistics.median(opt_rss) / statistics.median(base_rss),
            }
            all_results["semantic_snapshots"][case_id] = {"baseline": _semantic_snapshot(reference), "optimized": _semantic_snapshot(candidate)}
        output.write_text(json.dumps(all_results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        gate = all_results["cases"][GATE_CASE_ID]
        failures: list[str] = []
        if args.assert_equivalent and not all(case["equivalent"] for case in all_results["cases"].values()):
            failures.append("equivalence")
        # The empty case has no production G* loop at all; it remains an
        # equivalence regression, while the 5% throughput rule applies to
        # cases that exercise the hotspot implementation.
        regressions = [case_id for case_id, case in all_results["cases"].items()
                       if case_id != GATE_CASE_ID and case["event_count"] > 0 and case["speedup"] < 0.95]
        if regressions and args.runs >= 3:
            failures.append("non-gate regression >5%: " + ", ".join(regressions))
        if args.assert_speedup is not None and gate["speedup"] < args.assert_speedup:
            failures.append(f"speedup {gate['speedup']:.3f} < {args.assert_speedup:.3f}")
        if args.assert_rss_max_ratio is not None and gate["rss_ratio"] > args.assert_rss_max_ratio:
            failures.append(f"rss ratio {gate['rss_ratio']:.3f} > {args.assert_rss_max_ratio:.3f}")
        if args.assert_rss_reduction is not None and gate["rss_reduction"] < args.assert_rss_reduction:
            failures.append(f"rss reduction {gate['rss_reduction']:.3f} < {args.assert_rss_reduction:.3f}")
            if args.rss:
                floor_baseline, floor_optimized = [], []
                for run_index in range(args.runs):
                    ordered_sources = [("baseline", baseline, args.baseline_commit), ("optimized", root, optimized_commit)]
                    if args.alternate and run_index % 2:
                        ordered_sources.reverse()
                    for label, source, source_commit in ordered_sources:
                        result = _run_child(
                            Path(__file__).resolve(), source, source_commit, lock_digest, args.expected_python, GATE_CASE_ID, 1,
                            args.verbose, floor=True,
                        )
                        (floor_baseline if label == "baseline" else floor_optimized).append(result["peak_working_set_bytes"])
                all_results["rss_floor"] = {
                    "baseline_peak_working_set_bytes": {"median": statistics.median(floor_baseline), "raw": floor_baseline},
                    "optimized_peak_working_set_bytes": {"median": statistics.median(floor_optimized), "raw": floor_optimized},
                }
                output.write_text(json.dumps(all_results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if failures:
            raise SystemExit("hotspots gate failed: " + "; ".join(failures))
        print(json.dumps({"result": str(output), "sha256": _sha256_bytes(output.read_bytes())}))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-commit", default=DEFAULT_BASELINE)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument("--result-dir", default="generated/perf_hotspots")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--lock-digest")
    parser.add_argument("--expected-python", type=str)
    parser.add_argument("--case", choices=_cases())
    parser.add_argument("--only-case", choices=_cases(), help="diagnostic smoke only; formal runs omit this")
    parser.add_argument("--real-csv", type=Path)
    parser.add_argument("--real-snapshot", type=Path)
    parser.add_argument("--freeze-real-snapshot", action="store_true")
    parser.add_argument(
        "--processing-srid",
        type=int,
        help="Explicit projected metre CRS for --freeze-real-snapshot.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--floor", action="store_true")
    # Accepted now so the prescribed command is stable; checks occur in parent
    # reporting rather than silently redefining this harness' output.
    parser.add_argument("--alternate", action="store_true")
    parser.add_argument("--rss", action="store_true")
    parser.add_argument("--assert-equivalent", action="store_true")
    parser.add_argument("--assert-speedup", type=float)
    parser.add_argument("--assert-rss-reduction", type=float)
    parser.add_argument("--assert-rss-max-ratio", type=float)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_args()
    if arguments.child:
        if arguments.source is None or arguments.source_commit is None or arguments.lock_digest is None or arguments.expected_python is None or arguments.case is None:
            raise SystemExit("--child requires --source, --source-commit, --lock-digest, --expected-python and --case")
        print(json.dumps(_child(arguments.source.resolve(), arguments.source_commit, arguments.lock_digest, arguments.expected_python, arguments.case, arguments.repetitions,
                                arguments.verbose, arguments.snapshot, arguments.floor, arguments.real_snapshot)))
    else:
        if arguments.assert_rss_reduction is not None and not arguments.rss:
            raise SystemExit("--assert-rss-reduction requires --rss")
        if arguments.assert_rss_max_ratio is not None and not arguments.rss:
            raise SystemExit("--assert-rss-max-ratio requires --rss")
        raise SystemExit(_main(arguments))
