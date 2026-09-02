"""Exclusive DBSCAN phase diagnostic for D-DB-PHASE-01.

This is deliberately separate from ``manual_bench_dbscan.py``: it does not
change the formal comparison harness or its immutable samples ledger.  The
diagnostic uses a baseline archive and the current worktree in independent
children, then records source and formal-evidence hashes in a new artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


BASELINE = "c12a06abe3262501f5bb20713c4280aa9a99f34e"
TORCH_PYTHON = Path(os.environ.get("TRACK_BENCH_PYTHON", sys.executable))


_CHILD = r'''
import contextlib, io, json, time
from dataclasses import asdict, fields
from track.v2.intersect2 import (
    _NOISE, _UNVISITED, _cluster_dbscan, _distance_m, _neighbors_indices,
    _run_dbscan, _to_lonlat, _with_source_provenance,
)
try:
    from track.v2.intersect2 import _build_dbscan_grid, _grid_neighbors_indices
except ImportError:
    _build_dbscan_grid = _grid_neighbors_indices = None
from track.v2.models import HotspotCluster, PointRef, TurnEvent

request = json.loads(input())
xs, ys = request["xs"], request["ys"]
eps_m, min_samples = request["eps_m"], request["min_samples"]
events = [TurnEvent(i, f"phase-{i}", float(i), 116.0, 39.0, x, y, 0.0,
                    90.0, 90.0, 1.0, point_ref=PointRef("phase", str(i), stable=True))
          for i, (x, y) in enumerate(zip(xs, ys))]
# Canonicalize the transient first field across archived/current models.
calculate_index_field = fields(TurnEvent)[0].name

def cluster_record(cluster):
    record = asdict(cluster)
    for event in record["events"]:
        event["event_calculate_index"] = event.pop(calculate_index_field)
    return record

def instrumented_historical_state_machine():
    """Historical state machine with the source tree's actual neighbor path."""
    labels = [_UNVISITED] * len(xs)
    cluster_id = 0
    grid = None
    grid_build_s = 0.0
    neighbor_path = "scan"
    uses_queued_epoch = len(xs) > 256 and _build_dbscan_grid is not None and _grid_neighbors_indices is not None
    # The c12 archive does not expose these private grid helpers.  The current
    # tree does, and follows its production n>256 gate before choosing them.
    if len(xs) > 256 and _build_dbscan_grid is not None and _grid_neighbors_indices is not None:
        build_start = time.perf_counter()
        grid = _build_dbscan_grid(xs, ys, eps_m)
        grid_build_s = time.perf_counter() - build_start
        if grid is not None:
            neighbor_path = "grid"
    neighbor_query_s = 0.0
    expansion_s = 0.0
    queued_epoch = [0] * len(xs) if uses_queued_epoch else None
    def neighbors_for(index):
        if grid is None:
            return _neighbors_indices(xs, ys, index, eps_m)
        return _grid_neighbors_indices(xs, ys, index, eps_m, grid)
    total_start = time.perf_counter()
    for index in range(len(xs)):
        if labels[index] != _UNVISITED:
            continue
        query_start = time.perf_counter()
        neighbors = neighbors_for(index)
        neighbor_query_s += time.perf_counter() - query_start
        if len(neighbors) < min_samples:
            labels[index] = _NOISE
            continue
        expansion_start = time.perf_counter()
        labels[index] = cluster_id
        seeds = [candidate for candidate in neighbors if candidate != index]
        epoch = cluster_id + 1
        if queued_epoch is not None:
            for seed in seeds:
                queued_epoch[seed] = epoch
        expansion_s += time.perf_counter() - expansion_start
        while seeds:
            expansion_start = time.perf_counter()
            current = seeds.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_id
            if labels[current] != _UNVISITED:
                expansion_s += time.perf_counter() - expansion_start
                continue
            labels[current] = cluster_id
            expansion_s += time.perf_counter() - expansion_start
            query_start = time.perf_counter()
            current_neighbors = neighbors_for(current)
            neighbor_query_s += time.perf_counter() - query_start
            if len(current_neighbors) >= min_samples:
                expansion_start = time.perf_counter()
                for candidate in current_neighbors:
                    if labels[candidate] not in (_UNVISITED, _NOISE):
                        continue
                    if queued_epoch is None:
                        if candidate not in seeds:
                            seeds.append(candidate)
                    elif queued_epoch[candidate] != epoch:
                        seeds.append(candidate)
                        queued_epoch[candidate] = epoch
                expansion_s += time.perf_counter() - expansion_start
        cluster_id += 1
    total_s = time.perf_counter() - total_start
    return labels, {"neighbor_path": neighbor_path,
                    "expansion_path": "queued_epoch" if queued_epoch is not None else "historical_list_membership",
                    "grid_build_exclusive_s": grid_build_s,
                    "dbscan_total_wall_s": total_s,
                    "dbscan_with_index_wall_s": total_s + grid_build_s,
                    "neighbor_query_exclusive_s": neighbor_query_s,
                    "cluster_expansion_exclusive_s": expansion_s,
                    "outer_loop_unattributed_s": total_s - neighbor_query_s - expansion_s,
                    "cluster_count": cluster_id}

def assemble_wrapper_from_labels(labels):
    """The _cluster_dbscan wrapper section, excluding DBSCAN itself."""
    clusters = []
    for cid in sorted(set(label for label in labels if label >= 0)):
        cluster_items = [events[index] for index, label in enumerate(labels) if label == cid]
        if not cluster_items:
            continue
        cx = sum(event.x for event in cluster_items) / len(cluster_items)
        cy = sum(event.y for event in cluster_items) / len(cluster_items)
        radius = max(_distance_m((cx, cy), (event.x, event.y)) for event in cluster_items)
        lon, lat = _to_lonlat(cx, cy, 116.0, 39.0)
        cluster = HotspotCluster(cluster_id=cid, centroid_x=cx, centroid_y=cy,
                                 centroid_lon=lon, centroid_lat=lat, radius_m=radius,
                                 cluster_ref=f"cluster_turn_{cid}", events=tuple(cluster_items),
                                 source="turn")
        clusters.append(_with_source_provenance(cluster, source_counts={"turn": len(cluster_items)}))
    return clusters

labels, dbscan = instrumented_historical_state_machine()
wrapper_start = time.perf_counter()
clusters = assemble_wrapper_from_labels(labels)
wrapper_s = time.perf_counter() - wrapper_start
serialization_start = time.perf_counter()
serialized = json.dumps([cluster_record(cluster) for cluster in clusters], sort_keys=True, separators=(",", ":"))
serialization_s = time.perf_counter() - serialization_start

# Equivalence checks are deliberately outside phase timers, so recorded phases
# remain mutually exclusive.  Reference calls preserve log silence only.
with contextlib.redirect_stdout(io.StringIO()):
    reference_labels = _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples)
    reference_clusters = _cluster_dbscan(events, 116.0, 39.0, eps_m=eps_m, min_samples=min_samples)
reference_serialized = json.dumps([cluster_record(cluster) for cluster in reference_clusters], sort_keys=True, separators=(",", ":"))
print(json.dumps({"labels_equivalent": labels == reference_labels,
                  "wrapper_equivalent": serialized == reference_serialized,
                  "phase_timing": {**dbscan,
                                   "wrapper_assembly_exclusive_s": wrapper_s,
                                   "serialization_exclusive_s": serialization_s},
                  "serialized_bytes": len(serialized)}, separators=(",", ":")))
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _current_dirty_digest(worktree: Path) -> str:
    """Match manual_bench_dbscan.py's formal dirty-digest contract exactly."""
    tracked = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", "track/v2/intersect2.py"],
        cwd=worktree, capture_output=True, check=True,
    ).stdout
    return _sha256_bytes(tracked + (worktree / "track/v2/intersect2.py").read_bytes())


def _baseline_source_digest(worktree: Path) -> str:
    # The formal child imports the file from ``git archive`` extraction, not
    # ``git show``.  On this Windows worktree their checkout attributes can
    # produce different bytes, so hash the identical archive route.
    with tempfile.TemporaryDirectory(prefix="perf-dbscan-baseline-digest-") as temporary:
        tree = _archive(worktree, Path(temporary))
        return _sha256(tree / "track/v2/intersect2.py")


def _gate_case() -> dict[str, object]:
    import random

    rng = random.Random(20260810)
    xs: list[float] = []
    ys: list[float] = []
    for cx, cy in ((-800.0, -400.0), (-200.0, 500.0), (350.0, -250.0), (900.0, 700.0)):
        for _ in range(900):
            xs.append(cx + rng.uniform(-8.0, 8.0))
            ys.append(cy + rng.uniform(-8.0, 8.0))
    for _ in range(5_000 - len(xs)):
        xs.append(rng.uniform(-2_000.0, 2_000.0))
        ys.append(rng.uniform(-2_000.0, 2_000.0))
    return {"xs": xs, "ys": ys, "eps_m": 12.0, "min_samples": 5}


def _archive(worktree: Path, destination: Path) -> Path:
    archive = subprocess.run(
        ["git", "-c", f"safe.directory={worktree}", "archive", BASELINE],
        cwd=worktree, capture_output=True, check=True,
    ).stdout
    archive_path = destination / "baseline.tar"
    archive_path.write_bytes(archive)
    with tarfile.open(archive_path) as tar:
        tar.extractall(destination / "tree", filter="data")
    return destination / "tree"


def _run_child(python: str, tree: Path, request: dict[str, object]) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update({"PYTHONPATH": str(tree), "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
    child = subprocess.run([python, "-c", _CHILD], input=json.dumps(request), text=True,
                           capture_output=True, cwd=tree, env=environment)
    if child.returncode:
        raise RuntimeError(f"diagnostic child exit {child.returncode}: {child.stderr}")
    return json.loads(child.stdout.splitlines()[-1])


def _fail_closed(message: str) -> None:
    raise SystemExit(f"formal provenance mismatch (fail closed): {message}")


def _validate_formal_provenance(
    *, result_dir: Path, worktree: Path, gate_payload: dict[str, object], input_digest: str,
) -> tuple[dict[str, object], dict[str, Path]]:
    """Bind this diagnostic to the exact successful formal gate evidence."""
    required = {
        "formal_comparison": result_dir / "comparison.json",
        "gate_manifest": result_dir / "gate_manifest.json",
        "formal_harness": worktree / "track/v2/manual_thin_benchmark/manual_bench_dbscan.py",
        "lock": worktree / "uv.lock",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        _fail_closed(f"missing required evidence: {missing}")
    try:
        comparison = json.loads(required["formal_comparison"].read_text(encoding="utf-8"))
        manifest = json.loads(required["gate_manifest"].read_text(encoding="utf-8"))
        header = comparison["header"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        _fail_closed(f"unparseable comparison/header: {error}")
    source_digest = _sha256(worktree / "track/v2/intersect2.py")
    dirty_digest = _current_dirty_digest(worktree)
    optimized_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
    expected_header = {
        "record_type": "header", "version": 2, "baseline_commit": BASELINE,
        "frozen_manifest_sha256": _sha256(required["gate_manifest"]),
        "input_sha256": input_digest, "lock_digest": _sha256(required["lock"]),
        "harness_digest": _sha256(required["formal_harness"]),
        "optimized_source_digest": source_digest, "optimized_dirty_digest": dirty_digest,
        "interpreter": sys.executable,
        "python_version": sys.version, "warmups": 3, "runs": 11,
        "alternate": True, "rss": True,
        "rss_max_ratio": 1.05,
        "rss_acceptance_decision_version": "RSS-1",
        "generator_contract": {
            "rng": "random.Random", "seed": 20260810,
            "centers": [[-800.0, -400.0], [-200.0, 500.0], [350.0, -250.0], [900.0, 700.0]],
            "cluster_n": 900, "cluster_offset_uniform": [-8.0, 8.0],
            "noise_count": 1400, "noise_uniform": [-2000.0, 2000.0],
        },
    }
    for key, expected in expected_header.items():
        if header.get(key) != expected:
            _fail_closed(f"header.{key}={header.get(key)!r}, expected {expected!r}")
    expected_manifest = {
        "case_id": "dbscan_gate_v1", "generator_version": "mixed-cluster-noise-v1",
        "seed": 20260810, "n": 5000, "eps_m": gate_payload["eps_m"],
        "min_samples": gate_payload["min_samples"], "input_sha256": input_digest,
    }
    if manifest != expected_manifest:
        _fail_closed("frozen gate manifest parameters or input differ")
    if comparison.get("manifest") != manifest:
        _fail_closed("comparison formal gate manifest differs from frozen manifest")
    if comparison.get("equivalent") is not True:
        _fail_closed("formal comparison is not strictly equivalent")
    child_expectations = {
        "baseline": {"source_commit": BASELINE, "source_digest": _baseline_source_digest(worktree)},
        "optimized": {"source_commit": optimized_commit, "source_digest": source_digest},
    }
    for variant, child_expected in child_expectations.items():
        samples = comparison.get(variant, {}).get("children")
        if not isinstance(samples, list) or len(samples) != 11:
            _fail_closed(f"formal {variant} gate does not contain 11 samples")
        for sample in samples:
            for key, expected in {
                **child_expected, "sys_executable": sys.executable,
                "sys_version": sys.version, "lock_digest": expected_header["lock_digest"],
            }.items():
                if sample.get(key) != expected:
                    _fail_closed(f"formal {variant} child {key} differs")
    return comparison, required


def _run_negative_provenance_self_test(worktree: Path, result_dir: Path, expected_python: Path) -> None:
    """A stale comparison header must fail and must not produce an artifact."""
    mutations = {
        "header_dirty_digest": lambda item: item["header"].__setitem__("optimized_dirty_digest", "stale-dirty-digest"),
        "comparison_manifest": lambda item: item["manifest"].__setitem__("input_sha256", "stale-input-digest"),
        "baseline_child_source_digest": lambda item: item["baseline"]["children"][0].__setitem__("source_digest", "stale-baseline-digest"),
        "optimized_child_source_commit": lambda item: item["optimized"]["children"][0].__setitem__("source_commit", "stale-optimized-commit"),
        "optimized_child_sys_version": lambda item: item["optimized"]["children"][0].__setitem__("sys_version", "stale-python-version"),
        "header_rss_max_ratio": lambda item: item["header"].__setitem__("rss_max_ratio", 1.06),
    }
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="perf-dbscan-phase-stale-") as temporary:
        for name, mutate in mutations.items():
            stale_dir = Path(temporary) / name
            stale_dir.mkdir()
            shutil.copy2(result_dir / "comparison.json", stale_dir / "comparison.json")
            shutil.copy2(result_dir / "gate_manifest.json", stale_dir / "gate_manifest.json")
            stale = json.loads((stale_dir / "comparison.json").read_text(encoding="utf-8"))
            mutate(stale)
            (stale_dir / "comparison.json").write_text(json.dumps(stale), encoding="utf-8")
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--result-dir", str(stale_dir),
                 "--expected-python", str(expected_python)], cwd=worktree, text=True, capture_output=True,
            )
            artifact = stale_dir / "phase_diagnostic_exclusive_r15.json"
            if child.returncode == 0 or artifact.exists():
                raise RuntimeError(f"negative provenance self-test did not fail closed: {name}")
            outcomes.append({"mutation": name, "stale_child_exit_code": child.returncode,
                             "artifact_written": artifact.exists()})
    print(json.dumps({"negative_provenance_self_test": outcomes}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--expected-python", type=Path, default=TORCH_PYTHON)
    parser.add_argument("--self-test-provenance", action="store_true")
    args = parser.parse_args()
    if Path(sys.executable).resolve() != args.expected_python.resolve():
        raise SystemExit(f"wrong interpreter: {sys.executable}; expected {args.expected_python}")
    worktree = Path(__file__).resolve().parents[3]
    result_dir = args.result_dir.resolve()
    gate_payload = _gate_case()
    input_digest = hashlib.sha256(json.dumps({"xs": gate_payload["xs"], "ys": gate_payload["ys"]}, separators=(",", ":")).encode()).hexdigest()
    comparison, required = _validate_formal_provenance(
        result_dir=result_dir, worktree=worktree, gate_payload=gate_payload, input_digest=input_digest,
    )
    if args.self_test_provenance:
        _run_negative_provenance_self_test(worktree, result_dir, args.expected_python)
    with tempfile.TemporaryDirectory(prefix="perf-dbscan-phase-r12-") as temporary:
        baseline_tree = _archive(worktree, Path(temporary))
        baseline = _run_child(sys.executable, baseline_tree, gate_payload)
        optimized = _run_child(sys.executable, worktree, gate_payload)
    if not (baseline["labels_equivalent"] and baseline["wrapper_equivalent"] and optimized["labels_equivalent"] and optimized["wrapper_equivalent"]):
        raise SystemExit("instrumented state machine diverged from source implementation")
    output = {
        "artifact_version": "dbscan-phase-exclusive-v3",
        "interpreter": sys.executable,
        "python_version": sys.version,
        "baseline_commit": BASELINE,
        "optimized_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip(),
        "hashes": {name: _sha256(path) for name, path in required.items()},
        "formal_provenance_checked": {
            "comparison_equivalent": comparison["equivalent"],
            "formal_samples_per_variant": 11,
            "header_fields_checked": sorted(("record_type", "version", "baseline_commit", "frozen_manifest_sha256", "input_sha256", "lock_digest", "generator_contract", "harness_digest", "optimized_source_digest", "optimized_dirty_digest", "interpreter", "python_version", "warmups", "runs", "alternate", "rss", "rss_max_ratio", "rss_acceptance_decision_version")),
        },
        "rss_acceptance": {
            "decision_version": "RSS-1",
            "max_ratio": 1.05,
            "formal_rss_ratio": comparison["rss_ratio"],
            "formal_rss_pass": comparison.get("rss_pass") is True,
        },
        "optimized_source_sha256": _sha256(worktree / "track/v2/intersect2.py"),
        "diagnostic_helper_sha256": _sha256(Path(__file__)),
        "gate_input_sha256": input_digest,
        "timing_contract": {
            "neighbor_path": "baseline archive uses scan; current tree uses grid when its production n>256 gate builds one, otherwise scan fallback",
            "expansion_path": "baseline archive uses historical list membership; current n>256 source uses queued_epoch while preserving LIFO and label transitions",
            "grid_build_exclusive_s": "current tree _build_dbscan_grid before DBSCAN; baseline is zero because c12 has no grid",
            "dbscan_with_index_wall_s": "grid build plus DBSCAN control flow; compare this inclusive total to formal DBSCAN timing",
            "neighbor_query_exclusive_s": "only the selected source-tree neighbor call: scan or grid",
            "cluster_expansion_exclusive_s": "seed/list/label transitions, excluding neighbor calls",
            "wrapper_assembly_exclusive_s": "_cluster_dbscan assembly after labels, excluding DBSCAN",
            "serialization_exclusive_s": "JSON serialization after wrapper assembly",
            "outer_loop_unattributed_s": "dbscan total minus query and expansion; phases do not overlap",
            "reference_checks": "unmeasured equivalence verification after phase timing",
        },
        "baseline": baseline,
        "optimized": optimized,
    }
    artifact = result_dir / "phase_diagnostic_exclusive_r15.json"
    artifact.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(artifact), "baseline_equivalent": True, "optimized_equivalent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
