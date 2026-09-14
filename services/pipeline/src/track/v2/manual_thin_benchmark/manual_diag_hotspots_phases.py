"""Phase-0 hotspot timing diagnostic with fail-closed provenance.

This helper is deliberately separate from the formal performance gate.  It
decomposes one frozen gate execution into mutually exclusive phases without
changing the formal harness hash or the production implementation.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from track.v2.detect import (
    IntersectConfig,
    _compute_getis_ord_g_star,
    _getis_local_counts_grid,
    _select_hotspot_events,
)
from track.v2.manual_thin_benchmark import manual_bench_hotspots as formal
from track.v2.models import TurnEvent


PHASES = (
    "grid_index_seconds",
    "radius_count_seconds",
    "z_score_seconds",
    "selection_seconds",
    "serialization_seconds",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def _same_executable(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.abspath(actual)) == os.path.normcase(
        os.path.abspath(expected)
    )


def _project_root() -> Path:
    return PROJECT_ROOT


def _run_exclusive_phases(
    events: list[TurnEvent], cfg: IntersectConfig
) -> tuple[dict[str, float], dict[str, Any]]:
    """Mirror the production math while timing non-overlapping phase bodies."""
    cell_size = abs(cfg.g_star_radius_m)
    if len(events) <= 256 or cell_size == 0.0 or not math.isfinite(cell_size):
        raise RuntimeError("frozen Phase-0 gate must exercise the production grid path")

    phase_started = time.perf_counter()
    cell_bounds: list[tuple[int, int, int, int]] = []
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, event in enumerate(events):
        if not math.isfinite(event.x / cell_size) or not math.isfinite(
            event.y / cell_size
        ):
            raise RuntimeError("frozen Phase-0 input is not grid representable")
        min_cell_x = math.floor(
            math.nextafter((event.x - cell_size) / cell_size, -math.inf)
        )
        max_cell_x = math.floor(
            math.nextafter((event.x + cell_size) / cell_size, math.inf)
        )
        min_cell_y = math.floor(
            math.nextafter((event.y - cell_size) / cell_size, -math.inf)
        )
        max_cell_y = math.floor(
            math.nextafter((event.y + cell_size) / cell_size, math.inf)
        )
        if max_cell_x - min_cell_x + 1 > 4 or max_cell_y - min_cell_y + 1 > 4:
            raise RuntimeError("frozen Phase-0 input would use production scan fallback")
        cell_bounds.append((min_cell_x, max_cell_x, min_cell_y, max_cell_y))
        cells[(math.floor(event.x / cell_size), math.floor(event.y / cell_size))].append(index)
    grid_index_seconds = time.perf_counter() - phase_started

    phase_started = time.perf_counter()
    radius_sq = cfg.g_star_radius_m * cfg.g_star_radius_m
    local_counts: list[float] = []
    for index, event in enumerate(events):
        min_cell_x, max_cell_x, min_cell_y, max_cell_y = cell_bounds[index]
        count = 0.0
        for neighbor_x in range(min_cell_x, max_cell_x + 1):
            for neighbor_y in range(min_cell_y, max_cell_y + 1):
                for other_index in cells.get((neighbor_x, neighbor_y), ()):
                    other = events[other_index]
                    dx = other.x - event.x
                    dy = other.y - event.y
                    if dx * dx + dy * dy <= radius_sq:
                        count += 1.0
        local_counts.append(count)
    radius_count_seconds = time.perf_counter() - phase_started

    phase_started = time.perf_counter()
    mean = sum(local_counts) / len(events)
    variance = (
        sum(value * value for value in local_counts) / len(events) - mean * mean
    )
    std = math.sqrt(max(variance, 0.0))
    if std == 0.0:
        z_scores = {event.event_calculate_index: 0.0 for event in events}
    else:
        z_scores = {
            event.event_calculate_index: (count - mean) / std
            for event, count in zip(events, local_counts)
        }
    z_score_seconds = time.perf_counter() - phase_started

    phase_started = time.perf_counter()
    selected_indexes = [
        event.event_calculate_index
        for event in events
        if z_scores.get(event.event_calculate_index, 0.0) >= cfg.g_star_min_z
        and abs(event.turn_deg) >= cfg.min_turn_deg
    ]
    selection_seconds = time.perf_counter() - phase_started

    phase_started = time.perf_counter()
    serialized = json.dumps(
        {
            "z_scores": [
                [
                    event.event_calculate_index,
                    z_scores[event.event_calculate_index],
                ]
                for event in events
            ],
            "selected": selected_indexes,
        },
        separators=(",", ":"),
    )
    serialization_seconds = time.perf_counter() - phase_started

    timings = {
        "grid_index_seconds": grid_index_seconds,
        "radius_count_seconds": radius_count_seconds,
        "z_score_seconds": z_score_seconds,
        "selection_seconds": selection_seconds,
        "serialization_seconds": serialization_seconds,
    }
    outputs = {
        "local_counts": local_counts,
        "z_scores": z_scores,
        "selected_indexes": selected_indexes,
        "serialized_sha256": _sha256_bytes(serialized.encode()),
    }
    return timings, outputs


def _cumulative(exclusive: dict[str, float]) -> dict[str, float]:
    elapsed = 0.0
    cumulative: dict[str, float] = {}
    for phase in PHASES:
        elapsed += exclusive[phase]
        cumulative[phase] = elapsed
    return cumulative


def _main(args: argparse.Namespace) -> int:
    if not _same_executable(sys.executable, args.expected_python):
        raise RuntimeError(
            f"interpreter mismatch: {sys.executable} != {args.expected_python}"
        )
    if args.runs < 1:
        raise ValueError("--runs must be at least 1")

    root = _project_root()
    result_dir = (root / args.result_dir).resolve()
    manifest_path = result_dir / "gate_manifest.json"
    sidecar_path = result_dir / "real_turn_events_snapshot.json"
    compact_path = result_dir / "benchmark_compact.json"
    formal_path = Path(formal.__file__).resolve()
    source_path = root / "track" / "v2" / "detect.py"
    lock_path = root / "uv.lock"
    required = (manifest_path, sidecar_path, compact_path, formal_path, source_path, lock_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"required frozen evidence missing: {missing}")

    source_sha = _sha256_file(source_path)
    formal_sha = _sha256_file(formal_path)
    manifest_sha = _sha256_file(manifest_path)
    sidecar_sha = _sha256_file(sidecar_path)
    lock_sha = _sha256_file(lock_path)
    compact_sha = _sha256_file(compact_path)
    compact = json.loads(compact_path.read_text(encoding="utf-8"))
    header = compact["header"]
    expected_header = {
        "optimized_source_sha256": source_sha,
        "harness_sha256": formal_sha,
        "manifest_sha256": manifest_sha,
        "real_snapshot_sha256": sidecar_sha,
        "lock_sha256": lock_sha,
    }
    mismatches = {
        key: {"compact": header.get(key), "current": value}
        for key, value in expected_header.items()
        if header.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"formal benchmark provenance mismatch: {mismatches}")
    expected_acceptance = {
        "decision_version": formal.RSS_ACCEPTANCE_DECISION_VERSION,
        "rss_metric": "independent_child_peak_working_set_ratio",
        "rss_max_ratio": formal.RSS_MAX_RATIO,
        "structural_memory_guard_version": formal.STRUCTURAL_MEMORY_GUARD_VERSION,
    }
    if header.get("acceptance") != expected_acceptance:
        raise RuntimeError(
            "formal benchmark acceptance provenance mismatch: "
            f"{header.get('acceptance')} != {expected_acceptance}"
        )
    structural_memory_guard = header.get("structural_memory_guard")
    if not isinstance(structural_memory_guard, dict) or not structural_memory_guard.get("passed"):
        raise RuntimeError("formal benchmark structural memory guard is absent or failed")
    if not _same_executable(header["interpreter"], args.expected_python):
        raise RuntimeError("formal benchmark interpreter differs from requested interpreter")

    rows, config_values = formal._event_rows(formal.GATE_CASE_ID)
    input_sha = _sha256_bytes(_json_bytes(rows))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("gate_id") != formal.GATE_CASE_ID:
        raise RuntimeError("frozen manifest gate id differs from formal harness")
    if manifest.get("input_sha256") != input_sha:
        raise RuntimeError("frozen manifest input hash differs from formal generator")
    if manifest.get("event_count") != len(rows):
        raise RuntimeError("frozen manifest event count differs from formal generator")

    events = [TurnEvent(*row) for row in rows]
    cfg = IntersectConfig(**config_values)
    _run_exclusive_phases(events, cfg)  # one untimed warmup

    measured_runs: list[dict[str, Any]] = []
    last_outputs: dict[str, Any] | None = None
    for run_index in range(args.runs):
        exclusive, outputs = _run_exclusive_phases(events, cfg)
        cumulative = _cumulative(exclusive)
        measured_runs.append(
            {
                "run": run_index + 1,
                "exclusive_seconds": exclusive,
                "cumulative_seconds": cumulative,
                "total_exclusive_seconds": cumulative[PHASES[-1]],
            }
        )
        if last_outputs is not None and outputs != last_outputs:
            raise AssertionError("diagnostic outputs changed between measured runs")
        last_outputs = outputs

    if last_outputs is None:
        raise AssertionError("no measured diagnostic output")
    validation_started = time.perf_counter()
    production_phase_timings: dict[str, float] = {}
    production_counts = _getis_local_counts_grid(
        events, cfg.g_star_radius_m, phase_timings=production_phase_timings
    )
    production_z_scores = _compute_getis_ord_g_star(events, cfg.g_star_radius_m)
    production_selected = _select_hotspot_events(events, cfg)
    validation_seconds = time.perf_counter() - validation_started
    if production_counts != last_outputs["local_counts"]:
        raise AssertionError("diagnostic radius counts differ from production")
    if production_z_scores != last_outputs["z_scores"]:
        raise AssertionError("diagnostic z-scores differ from production")
    if [
        event.event_calculate_index for event in production_selected
    ] != last_outputs["selected_indexes"]:
        raise AssertionError("diagnostic selection differs from production")

    median_exclusive = {
        phase: statistics.median(
            run["exclusive_seconds"][phase] for run in measured_runs
        )
        for phase in PHASES
    }
    median_cumulative = _cumulative(median_exclusive)
    artifact = {
        "schema": "hotspots_phase0_diagnostic_v1",
        "input": {
            "case_id": formal.GATE_CASE_ID,
            "event_count": len(events),
            "input_sha256": input_sha,
            "source": "formal harness deterministic gate generator",
            "real_sidecar_usage": "provenance-bound but not timed",
        },
        "provenance": {
            "interpreter": sys.executable,
            "python_version": sys.version,
            "optimized_commit": header["optimized_commit"],
            "production_source_sha256": source_sha,
            "formal_harness_sha256": formal_sha,
            "formal_compact_sha256": compact_sha,
            "formal_acceptance": expected_acceptance,
            "formal_structural_memory_guard": structural_memory_guard,
            "frozen_manifest_sha256": manifest_sha,
            "real_sidecar_sha256": sidecar_sha,
            "lock_sha256": lock_sha,
            "diagnostic_helper_sha256": _sha256_file(Path(__file__).resolve()),
        },
        "timing_model": {
            "relationship": "exclusive phases execute serially and do not overlap; cumulative values are prefix sums of the exclusive phases",
            "exclusive_order": list(PHASES),
            "grid_index_scope": "cell-bound calculation plus cell-index construction",
            "radius_count_scope": "candidate-cell enumeration plus exact squared-distance comparisons",
            "z_score_scope": "mean, variance, standard deviation, and calculation-index score mapping from precomputed counts",
            "selection_scope": "pure threshold and turn-angle filtering from precomputed scores",
            "serialization_scope": "JSON construction and encoding of scores and selected calculation indexes",
            "excluded_overlap": "production validation reruns grid, z-score, and selection after measurement; validation_seconds is excluded from all phase and total values",
        },
        "parameters": {"warmups": 1, "runs": args.runs},
        "runs": measured_runs,
        "summary": {
            "median_exclusive_seconds": median_exclusive,
            "median_cumulative_seconds": median_cumulative,
            "median_total_exclusive_seconds": median_cumulative[PHASES[-1]],
        },
        "validation": {
            "production_counts_exact": True,
            "production_z_scores_exact": True,
            "production_selection_exact": True,
            "production_grid_helper_phase_timings": production_phase_timings,
            "validation_seconds_excluded_from_phase_totals": validation_seconds,
            "local_counts_sha256": _sha256_bytes(
                _json_bytes(last_outputs["local_counts"])
            ),
            "z_scores_sha256": _sha256_bytes(
                _json_bytes(
                    [
                        [
                            event.event_calculate_index,
                            last_outputs["z_scores"][event.event_calculate_index],
                        ]
                        for event in events
                    ]
                )
            ),
            "selected_indexes_sha256": _sha256_bytes(
                _json_bytes(last_outputs["selected_indexes"])
            ),
            "serialized_sha256": last_outputs["serialized_sha256"],
        },
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    output = result_dir / "phase0_diagnostic.json"
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"result": str(output), "sha256": _sha256_file(output)},
            sort_keys=True,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--result-dir", default="generated/perf_hotspots")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(_main(_parse_args()))
