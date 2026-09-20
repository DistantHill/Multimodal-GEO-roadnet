"""Verify and time the complete Method 3 spatial-window stage.

Examples::

    # Dense-window exploratory comparison (does not claim the merge gate).
    uv run python track/v2/manual_thin_benchmark/manual_bench_cross_track_inner.py \
        --processing-srid 32650 --window-index 1753 --version WORKTREE_ID

    # Required gate: three full-stage reference/optimized runs.
    uv run python track/v2/manual_thin_benchmark/manual_bench_cross_track_inner.py \
        --processing-srid 32650 --repetitions 3 \
        --write-baseline cross_track_500k.json --require-target --version WORKTREE_ID

The script is read-only against the dedicated ``track_test`` database. It
compares a dense same-processing-CRS reference and the bounded-grid implementation across
the entire selected spatial-window stage: DB query, grouping, extraction, and
cross-window segment-pair de-duplication. It also checks the downstream
cross-track DBSCAN cluster payload before reporting timing results.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import io
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ctypes import wintypes

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from track.v2.cross_track import (
    extract_cross_track_candidates_reference as _extract_orthogonal_cross_track_candidates_reference,
)
from track.v2.candidate_extraction import (
    extract_cross_track_candidates as _extract_orthogonal_cross_track_candidates,
)
from track.v2.detect import (
    IntersectConfig,
    SpatialWindowIterator,
    _cluster_orthogonal_cross_track_candidates,
    _extract_cross_track_windows,
)
from track.v2.db.queries import _query_spatial_extent
from track.v2.db.source_table import validate_source_table
from track.v2.manual_thin_benchmark._thin_bench_helpers import connect_test_db
from track.v2.models import ProjectionConfig, SpatialContext
from track.v2.projection import CoordinateTransformer, create_spatial_context


_GENERATED_DIR = _PROJECT_ROOT / "generated"
_TARGET_SPEEDUP = 5.0
_MIN_GATE_POINTS = 500_000
_XY_TOLERANCE_M = 1e-6
_LONLAT_TOLERANCE_DEG = 1e-9


def _write_json(path: Path, value: object) -> None:
    """Write one deterministic benchmark artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


class _QuietLiveStatus:
    """Discard progress updates so measured runs do not print inner progress."""

    def update(self, **_: object) -> None:
        pass


def _process_peak_rss_mib() -> float | None:
    """Return the Windows process peak working set without a new dependency."""
    if sys.platform != "win32":
        return None

    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCountersEx), wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    success = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb,
    )
    if not success:
        return None
    return counters.PeakWorkingSetSize / (1024 * 1024)


def _candidate_records(candidates: list[object]) -> list[dict[str, object]]:
    return [candidate.as_dict() for candidate in candidates]  # type: ignore[attr-defined]


def _compare_records(
    reference: object,
    optimized: object,
    *,
    path: str = "",
    differences: dict[str, float] | None = None,
) -> dict[str, float]:
    """Strict structure/order comparison with unit-specific numeric budgets."""
    result = {} if differences is None else differences
    if isinstance(reference, dict) and isinstance(optimized, dict):
        if list(reference) != list(optimized):
            raise ValueError(f"{path}: field structure/order mismatch")
        for key in reference:
            _compare_records(
                reference[key], optimized[key], path=f"{path}.{key}", differences=result,
            )
        return result
    if isinstance(reference, list) and isinstance(optimized, list):
        if len(reference) != len(optimized):
            raise ValueError(f"{path}: member count mismatch")
        for index, (left, right) in enumerate(zip(reference, optimized)):
            _compare_records(left, right, path=f"{path}[{index}]", differences=result)
        return result
    if isinstance(reference, float) or isinstance(optimized, float):
        left, right = float(reference), float(optimized)  # type: ignore[arg-type]
        if not math.isfinite(left) or not math.isfinite(right):
            raise ValueError(f"{path}: non-finite numeric value")
        field = path.rsplit(".", 1)[-1]
        tolerance = (
            _XY_TOLERANCE_M
            if field in {"x", "y", "centroid_x", "centroid_y", "radius_m"}
            else _LONLAT_TOLERANCE_DEG
            if field in {"lon", "lat", "centroid_lon", "centroid_lat"}
            else 1e-12
        )
        delta = abs(left - right)
        result[field] = max(result.get(field, 0.0), delta)
        if delta > tolerance:
            raise ValueError(
                f"{path}: delta {delta:g} exceeds absolute tolerance {tolerance:g}"
            )
        return result
    if reference != optimized:
        raise ValueError(f"{path}: identity/member/order mismatch")
    return result


def _cluster_records(
    candidates: list[object], transformer: CoordinateTransformer, cfg: IntersectConfig,
) -> list[dict[str, object]]:
    with contextlib.redirect_stdout(io.StringIO()):
        clusters = _cluster_orthogonal_cross_track_candidates(
            candidates,  # type: ignore[arg-type]
            transformer,
            eps_m=cfg.cross_track_dbscan_eps_m,
            min_samples=cfg.cross_track_dbscan_min_samples,
        )
    return [cluster.as_dict() for cluster in clusters]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _print_step_summary(label: str, wall_s: list[float], telemetries: list[dict[str, int | float]]) -> None:
    median_wall = statistics.median(wall_s)
    aggregate_wall = sum(wall_s)
    totals = {
        key: sum(t.get(key, 0.0) for t in telemetries)
        for key in ("query_seconds", "group_sort_seconds", "candidate_extraction_seconds", "dedupe_seconds")
    }
    points = sum(t.get("queried_rows", 0.0) for t in telemetries)
    windows = sum(t.get("windows", 0.0) for t in telemetries)
    empty_windows = sum(t.get("empty_windows", 0.0) for t in telemetries)
    unattributed = max(0.0, aggregate_wall - sum(totals.values()))
    print(
        f"{label} median_wall_s={median_wall:.3f} p50_s={_percentile(wall_s, 50):.3f} "
        f"p95_s={_percentile(wall_s, 95):.3f} p99_s={_percentile(wall_s, 99):.3f} "
        f"aggregate_wall_s={aggregate_wall:.3f} windows={windows:,.0f} "
        f"empty_windows={empty_windows:,.0f} points={points:,.0f}"
    )
    for name, value in totals.items():
        share = (value / aggregate_wall * 100.0) if aggregate_wall else 0.0
        per_window_ms = (value / windows * 1_000.0) if windows else 0.0
        per_point_us = (value / points * 1_000_000.0) if points else 0.0
        print(
            f"  {name}={value:.3f}s ({share:.1f}%) "
            f"per_window_ms={per_window_ms:.3f} per_point_us={per_point_us:.3f}"
        )
    unattributed_share = (unattributed / aggregate_wall * 100.0) if aggregate_wall else 0.0
    print(f"  unattributed_s={unattributed:.3f}s ({unattributed_share:.1f}%)")
    print(
        f"  wall_per_window_ms={(aggregate_wall / windows * 1_000.0) if windows else 0.0:.3f} "
        f"wall_per_point_us={(aggregate_wall / points * 1_000_000.0) if points else 0.0:.3f}"
    )


def _baseline_path(filename: str) -> Path:
    requested = Path(filename)
    if requested.name != filename or requested.suffix.lower() != ".json":
        raise ValueError("--write-baseline accepts a JSON filename only; it is always written under generated/")
    return _GENERATED_DIR / requested


def _source_point_count(conn: Any, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def _source_input_sha256(conn: Any, table: str) -> str:
    """Hash the ordered frozen benchmark inputs without retaining trajectories."""
    digest = hashlib.sha256()
    contract = validate_source_table(conn, table)
    extra = "extra" if contract.has_extra else "'{}'::jsonb"
    with conn.cursor(name="cross_track_input_hash") as cur:
        cur.execute(
            f"SELECT id::text, vehicle_id, timestamp, lon, lat, heading, speed, "
            f"{extra}, encode(ST_AsEWKB(geom), 'hex') "
            f"FROM {contract.quoted_table} ORDER BY id::text"
        )
        for row in cur:
            digest.update(json.dumps(
                list(row), default=str, sort_keys=True, allow_nan=False,
                separators=(",", ":"),
            ).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _run_stage(
    conn: Any,
    cfg: IntersectConfig,
    extent: tuple[float, float, float, float],
    spatial_context: SpatialContext,
    transformer: CoordinateTransformer,
    extractor: Callable[..., tuple[list[object], int, int]],
    *,
    windows_override: list[object],
) -> tuple[list[object], float, dict[str, int | float]]:
    telemetry: dict[str, int | float] = {}
    started = time.perf_counter()
    candidates = _extract_cross_track_windows(
        conn,
        cfg,
        extent,
        spatial_context,
        transformer,
        live_status=_QuietLiveStatus(),
        candidate_extractor=extractor,  # type: ignore[arg-type]
        windows_override=windows_override,  # type: ignore[arg-type]
        telemetry=telemetry,
    )
    return candidates, time.perf_counter() - started, telemetry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-index", type=int, default=0,
        help="One-based spatial window index; 0 processes the selected range.",
    )
    parser.add_argument(
        "--max-windows", type=int, default=0,
        help="Maximum windows when --window-index is 0; 0 means every window.",
    )
    parser.add_argument("--repetitions", type=int, default=3, help="Reference/optimized stage repetitions.")
    parser.add_argument(
        "--write-baseline",
        metavar="FILENAME.json",
        help="Write the first full-stage equivalence snapshot below generated/.",
    )
    parser.add_argument(
        "--require-target",
        action="store_true",
        help=f"Fail unless full selected stage is equivalent and median speedup is at least {_TARGET_SPEEDUP:g}x.",
    )
    parser.add_argument(
        "--processing-srid",
        type=int,
        required=True,
        help="Projected metre-based EPSG code valid for the complete benchmark data region.",
    )
    parser.add_argument(
        "--version", required=True,
        help="Code/build identifier recorded in the benchmark report.",
    )
    args = parser.parse_args()
    if args.window_index < 0 or args.max_windows < 0 or args.repetitions < 1:
        parser.error("window limits must be non-negative and repetitions must be at least one")
    if args.require_target and (args.window_index or args.max_windows):
        parser.error("--require-target requires the complete spatial-window stage (no window limit)")
    if args.require_target and args.repetitions < 3:
        parser.error("--require-target requires at least three repetitions")
    if args.write_baseline and (args.window_index or args.max_windows):
        parser.error("--write-baseline requires the complete spatial-window stage")
    if args.require_target and not args.write_baseline:
        args.write_baseline = "cross_track_500k.json"

    spatial_context = create_spatial_context(
        ProjectionConfig(processing_srid=args.processing_srid)
    )
    transformer = CoordinateTransformer(spatial_context)
    cfg = IntersectConfig()
    conn = connect_test_db()
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        extent = _query_spatial_extent(
            conn,
            table=cfg.db_table,
            spatial_context=spatial_context,
        )
        if extent is None:
            print("No source points found.", file=sys.stderr)
            return 2
        source_points = _source_point_count(conn, cfg.db_table)
        source_input_sha256 = _source_input_sha256(conn, cfg.db_table)
        if args.require_target and source_points < _MIN_GATE_POINTS:
            print(
                f"merge_gate_failed: source point count {source_points:,} "
                f"is below {_MIN_GATE_POINTS:,}",
                file=sys.stderr,
            )
            return 1
        windows = list(SpatialWindowIterator(
            extent,
            size=cfg.spatial_window_size,
            overlap=cfg.spatial_window_overlap,
        ).iter_windows())
        if args.window_index:
            if args.window_index > len(windows):
                parser.error(f"window index must be within 1..{len(windows)}")
            selected = [windows[args.window_index - 1]]
        elif args.max_windows:
            selected = windows[:args.max_windows]
        else:
            selected = windows

        print(
            f"source_points={source_points:,} "
            f"selected_windows={len(selected):,}/{len(windows):,} "
            f"repetitions={args.repetitions} "
            f"target_gate={'on' if args.require_target else 'off'}"
        )
        reference_times: list[float] = []
        optimized_times: list[float] = []
        reference_timings: list[dict[str, int | float]] = []
        optimized_timings: list[dict[str, int | float]] = []
        peak_rss_mib: list[float] = []
        first_snapshot: dict[str, object] | None = None

        for repetition in range(args.repetitions):
            order = (
                ("reference", _extract_orthogonal_cross_track_candidates_reference),
                ("optimized", _extract_orthogonal_cross_track_candidates),
            )
            if repetition % 2:
                order = tuple(reversed(order))
            results: dict[str, tuple[list[object], float, dict[str, int | float]]] = {}
            for label, extractor in order:
                results[label] = _run_stage(
                    conn,
                    cfg,
                    extent,
                    spatial_context,
                    transformer,
                    extractor,
                    windows_override=selected,
                )
            reference, reference_wall, reference_timing = results["reference"]
            optimized, optimized_wall, optimized_timing = results["optimized"]
            reference_records = _candidate_records(reference)
            optimized_records = _candidate_records(optimized)
            try:
                candidate_differences = _compare_records(reference_records, optimized_records)
            except ValueError as exc:
                print(f"repetition={repetition + 1}: candidate mismatch: {exc}", file=sys.stderr)
                return 1
            reference_clusters = _cluster_records(reference, transformer, cfg)
            optimized_clusters = _cluster_records(optimized, transformer, cfg)
            try:
                cluster_differences = _compare_records(reference_clusters, optimized_clusters)
            except ValueError as exc:
                print(f"repetition={repetition + 1}: downstream cluster mismatch: {exc}", file=sys.stderr)
                return 1
            if first_snapshot is None:
                first_snapshot = {
                    "candidate_records": reference_records,
                    "cluster_records": reference_clusters,
                    "timing_reference": reference_timing,
                    "timing_optimized": optimized_timing,
                    "absolute_tolerances": {
                        "xy_m": _XY_TOLERANCE_M,
                        "lonlat_degree": _LONLAT_TOLERANCE_DEG,
                    },
                    "candidate_max_absolute_differences": candidate_differences,
                    "cluster_max_absolute_differences": cluster_differences,
                }
            reference_times.append(reference_wall)
            optimized_times.append(optimized_wall)
            reference_timings.append(reference_timing)
            optimized_timings.append(optimized_timing)
            peak_rss = _process_peak_rss_mib()
            if peak_rss is not None:
                peak_rss_mib.append(peak_rss)
            speedup = reference_wall / optimized_wall if optimized_wall else float("inf")
            print(
                f"repetition={repetition + 1} reference_wall_s={reference_wall:.3f} "
                f"optimized_wall_s={optimized_wall:.3f} speedup={speedup:.2f}x "
                f"candidates={len(optimized_records):,} clusters={len(optimized_clusters):,}"
            )

        _print_step_summary("reference", reference_times, reference_timings)
        _print_step_summary("optimized", optimized_times, optimized_timings)
        median_speedup = statistics.median(reference_times) / statistics.median(optimized_times)
        peak_rss_summary = f" process_peak_rss_mib={max(peak_rss_mib):.1f}" if peak_rss_mib else ""
        print(f"median_speedup={median_speedup:.2f}x equivalent=true{peak_rss_summary}")

        if args.write_baseline:
            try:
                output_path = _baseline_path(args.write_baseline)
            except ValueError as error:
                parser.error(str(error))
            _write_json(output_path, {
                "schema": "track.v2-cross-track-same-crs-benchmark-v1",
                "review_status": "pending_review",
                "version": args.version,
                "processing_srid": spatial_context.processing_srid,
                "input_sha256": source_input_sha256,
                "input_snapshot": "postgres_repeatable_read_read_only",
                "extent": extent,
                "active_point_count": source_points,
                "window_count": len(windows),
                "selected_windows": len(selected),
                "config": cfg.as_dict(),
                "repetitions": args.repetitions,
                "reference_wall_s": reference_times,
                "optimized_wall_s": optimized_times,
                "median_speedup": median_speedup,
                "process_peak_rss_mib": peak_rss_mib,
                "snapshot": first_snapshot,
            })
            print(f"baseline_written={output_path}")

        if args.require_target and median_speedup < _TARGET_SPEEDUP:
            print(
                f"merge_gate_failed: median speedup {median_speedup:.2f}x is below {_TARGET_SPEEDUP:g}x",
                file=sys.stderr,
            )
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
