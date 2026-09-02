"""Strict baseline/optimized DBSCAN comparison for PERF-DBSCAN-001.

The parent process never imports ``track``.  Each measurement child gets exactly
one source tree through PYTHONPATH, so an archive baseline cannot be accidentally
mixed with the worktree implementation.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


GENERATOR_VERSION = "mixed-cluster-noise-v1"
GATE_CASE_ID = "dbscan_gate_v1"
RSS_ACCEPTANCE_DECISION_VERSION = "RSS-1"
RSS_MAX_RATIO = 1.05
GATE_SEED = 20260810
GATE_N = 5_000
GATE_EPS_M = 12.0
GATE_MIN_SAMPLES = 5
GATE_CONTRACT = {"rng": "random.Random", "seed": GATE_SEED,
    "centers": [[-800.0, -400.0], [-200.0, 500.0], [350.0, -250.0], [900.0, 700.0]],
    "cluster_n": 900, "cluster_offset_uniform": [-8.0, 8.0],
    "noise_count": GATE_N - 4 * 900, "noise_uniform": [-2000.0, 2000.0]}


@dataclass(frozen=True)
class Case:
    case_id: str
    xs: list[float]
    ys: list[float]
    eps_m: float
    min_samples: int


def _gate_case() -> Case:
    # A local deterministic generator avoids source-data and encoding dependencies.
    # It intentionally contains compact clusters, boundary-near points and noise.
    import random

    rng = random.Random(GATE_SEED)
    xs: list[float] = []
    ys: list[float] = []
    centers = [(-800.0, -400.0), (-200.0, 500.0), (350.0, -250.0), (900.0, 700.0)]
    cluster_n = 900
    for cx, cy in centers:
        for _ in range(cluster_n):
            xs.append(cx + rng.uniform(-8.0, 8.0))
            ys.append(cy + rng.uniform(-8.0, 8.0))
    for _ in range(GATE_N - len(xs)):
        xs.append(rng.uniform(-2_000.0, 2_000.0))
        ys.append(rng.uniform(-2_000.0, 2_000.0))
    return Case(GATE_CASE_ID, xs, ys, GATE_EPS_M, GATE_MIN_SAMPLES)


def _cases() -> list[Case]:
    eps = 1.0
    return [
        Case("empty", [], [], eps, 1),
        Case("single", [0.0], [0.0], eps, 1),
        Case("sparse_noise", [0.0, 3.0, 6.0], [0.0, 0.0, 0.0], eps, 2),
        Case("separate_clusters", [0, .1, .2, 10, 10.1, 10.2], [0, 0, 0, 0, 0, 0], .5, 2),
        Case("density_chain", [0, .9, 1.8, 2.7, 3.6], [0, 0, 0, 0, 0], 1.0, 2),
        Case("duplicates_negative", [-1, -1, -1, -4, -4], [-1, -1, -1, 3, 3], .1, 2),
        Case("eps_boundary", [0.0, 1.0, math.nextafter(1.0, math.inf)], [0, 0, 0], 1.0, 1),
        Case("cross_zero_boundary", [-1e-300, 1.0, math.nextafter(1.0, math.inf), math.nextafter(-1e-300, -math.inf)], [0.0] * 4, 1.0, 1),
        Case("invalid_nan", [0.0, math.nan], [0.0, 0.0], 1.0, 1),
        Case("invalid_eps", [0.0, 0.5], [0.0, 0.0], -1.0, 2),
        Case("dense", [float(i % 50) / 100 for i in range(2_500)], [float(i // 50) / 100 for i in range(2_500)], 2.0, 1),
        _gate_case(),
    ]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _dirty_digest(worktree: Path) -> str:
    """Bind measurements to both tracked and untracked implementation bytes."""
    tracked = subprocess.run(["git", "diff", "--binary", "HEAD", "--", "track/v2/intersect2.py"], cwd=worktree, capture_output=True, check=True).stdout
    return _sha256_bytes(tracked + (worktree / "track/v2/intersect2.py").read_bytes())


def _gate_manifest(case: Case) -> dict[str, object]:
    payload = json.dumps({"xs": case.xs, "ys": case.ys}, separators=(",", ":")).encode()
    return {
        "case_id": case.case_id,
        "generator_version": GENERATOR_VERSION,
        "seed": GATE_SEED,
        "n": GATE_N,
        "eps_m": case.eps_m,
        "min_samples": case.min_samples,
        "input_sha256": _sha256_bytes(payload),
    }


_CHILD = r'''
import ctypes, ctypes.wintypes, hashlib, json, os, sys, time, io, contextlib
from dataclasses import asdict, fields
from pathlib import Path
from track.v2.intersect2 import _cluster_dbscan, _neighbors_indices, _run_dbscan
try:
    from track.v2.intersect2 import _build_dbscan_grid, _grid_neighbors_indices
except ImportError:
    _build_dbscan_grid = _grid_neighbors_indices = None
from track.v2.models import PointRef, TurnEvent

def peak_working_set_size():
    class PMC(ctypes.Structure):
        _fields_=[("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters=PMC(); counters.cb=ctypes.sizeof(PMC)
    get_info=ctypes.windll.psapi.GetProcessMemoryInfo
    get_info.argtypes=(ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.wintypes.DWORD)
    get_info.restype=ctypes.wintypes.BOOL
    handle=ctypes.windll.kernel32.GetCurrentProcess()
    ok=get_info(handle, ctypes.byref(counters), counters.cb)
    if not ok: raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)

request=json.loads(sys.stdin.read())
tree=Path.cwd()
t0=time.perf_counter()
mode=request.get("mode")
diagnostic=None
if mode == "import_only":
    labels=[]
elif mode == "phase_diagnostic":
    stages={}
    start=time.perf_counter(); grid=_build_dbscan_grid(request["xs"], request["ys"], request["eps_m"]) if _build_dbscan_grid else None; stages["index_build_s"]=time.perf_counter()-start
    start=time.perf_counter()
    neighbor_count=sum(len(_grid_neighbors_indices(request["xs"], request["ys"], i, request["eps_m"], grid)) if _grid_neighbors_indices else len(_neighbors_indices(request["xs"], request["ys"], i, request["eps_m"])) for i in range(len(request["xs"])))
    stages["neighbor_query_full_s"]=time.perf_counter()-start
    start=time.perf_counter(); labels=_run_dbscan(request["xs"], request["ys"], eps_m=request["eps_m"], min_samples=request["min_samples"]); stages["dbscan_inclusive_s"]=time.perf_counter()-start
    events=[TurnEvent(i, f"bench-{i}", float(i), 116.0, 39.0, x, y, 0.0, 90.0, 90.0, 1.0, point_ref=PointRef("bench", str(i), stable=True)) for i,(x,y) in enumerate(zip(request["xs"],request["ys"]))]
    start=time.perf_counter(); clusters=_cluster_dbscan(events, 116.0, 39.0, eps_m=request["eps_m"], min_samples=request["min_samples"]); stages["wrapper_inclusive_s"]=time.perf_counter()-start
    # Canonicalize the transient first field across archived/current models.
    calculate_index_field=fields(TurnEvent)[0].name
    def cluster_record(cluster):
        record=asdict(cluster)
        for event in record["events"]:
            event["event_calculate_index"]=event.pop(calculate_index_field)
        return record
    start=time.perf_counter(); serialized=json.dumps([cluster_record(c) for c in clusters], sort_keys=True, separators=(",",":")); stages["serialization_s"]=time.perf_counter()-start
    diagnostic={"verbose": "build, full neighbor sweep, DBSCAN inclusive, wrapper inclusive, serialization", "stages": stages, "neighbor_count": neighbor_count, "serialized_bytes": len(serialized), "n": len(request["xs"])}
    print("VERBOSE phase diagnostic: build, full neighbor sweep, DBSCAN inclusive, wrapper inclusive, serialization")
else:
    repeats=request.get("repeats", 1)
    # DBSCAN's progress print is not part of the algorithm measurement and
    # would otherwise dominate tiny-case batches.
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(repeats):
            labels=_run_dbscan(request["xs"], request["ys"], eps_m=request["eps_m"], min_samples=request["min_samples"])
wall=time.perf_counter()-t0
digest=hashlib.sha256((tree / "track/v2/intersect2.py").read_bytes()).hexdigest()
print(json.dumps({"labels": labels, "wall_s": wall, "per_call_wall_s": wall / request.get("repeats", 1), "repeats": request.get("repeats", 1), "peak_working_set_size": peak_working_set_size(), "source_tree": str(tree), "source_digest": digest, "sys_executable": sys.executable, "sys_version": sys.version, "source_commit": request["source_commit"], "lock_digest": request["lock_digest"], "diagnostic": diagnostic}, separators=(",", ":")))
'''


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def _run_child(python: str, tree: Path, request: dict[str, object]) -> dict[str, object]:
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(tree), "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
    result = subprocess.run([python, "-c", _CHILD], input=json.dumps(request), text=True, capture_output=True, cwd=tree, env=env)
    if result.returncode:
        raise RuntimeError(f"measurement child failed ({result.returncode}): {result.stderr}")
    return json.loads(result.stdout.splitlines()[-1])


def _archive_baseline(worktree: Path, commit: str, destination: Path) -> Path:
    archive = subprocess.run(["git", "-c", f"safe.directory={worktree}", "archive", commit], cwd=worktree, capture_output=True, check=True).stdout
    import tarfile
    tar_path = destination / "baseline.tar"
    tar_path.write_bytes(archive)
    with tarfile.open(tar_path) as tar:
        tar.extractall(destination / "tree", filter="data")
    return destination / "tree"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument("--alternate", action="store_true")
    parser.add_argument("--rss", action="store_true")
    parser.add_argument("--assert-equivalent", action="store_true")
    parser.add_argument("--assert-speedup", type=float)
    parser.add_argument(
        "--assert-rss-max-ratio",
        type=float,
        default=RSS_MAX_RATIO,
        help=("approved RSS-1 gate: optimized/baseline independent-child "
              "PeakWorkingSetSize median must not exceed this ratio"),
    )
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="continue complete baseline/optimized sample pairs")
    parser.add_argument("--diagnose-import-only", action="store_true")
    parser.add_argument("--phase-diagnostic", action="store_true")
    parser.add_argument("--library-evidence", action="store_true", help="record sklearn then cKDTree decision evidence")
    parser.add_argument("--matrix-only", action="store_true", help="run regression matrix without touching formal ledger/results")
    parser.add_argument("--matrix-cases", nargs="*", help="optional case IDs for a smoke subset")
    args = parser.parse_args()
    if args.runs < 11 and not args.matrix_only:
        raise SystemExit("--runs must be at least 11")
    if args.assert_rss_max_ratio != RSS_MAX_RATIO:
        raise SystemExit(
            f"RSS-1 approval freezes --assert-rss-max-ratio at {RSS_MAX_RATIO:.2f}; "
            f"got {args.assert_rss_max_ratio!r}"
        )
    worktree = Path(__file__).resolve().parents[3]
    case = _gate_case()
    args.result_dir.mkdir(parents=True, exist_ok=True)
    if args.library_evidence:
        checks = []
        for name, statement in (("sklearn.cluster.DBSCAN", "from sklearn.cluster import DBSCAN"),
                                ("scipy.spatial.cKDTree", "from scipy.spatial import cKDTree")):
            command = [sys.executable, "-c", f"{statement}; import importlib.metadata as m; print(m.version('{name.split('.')[0]}'))"]
            completed = subprocess.run(command, text=True, capture_output=True)
            checks.append({"candidate": name, "command": command, "exit_code": completed.returncode,
                           "stdout": completed.stdout, "stderr": completed.stderr,
                           "decision": "unavailable; continue" if completed.returncode else "available; requires strict gate comparison"})
        distributions = {}
        from importlib import metadata
        for distribution in ("scikit-learn", "scipy"):
            try:
                dist = metadata.distribution(distribution)
                distributions[distribution] = {"version": dist.version, "location": str(dist.locate_file("")),
                    "license": dist.metadata.get("License"), "license_expression": dist.metadata.get("License-Expression"),
                    "metadata_path": str(dist._path)}
            except metadata.PackageNotFoundError:
                distributions[distribution] = {"installed": False}
        (args.result_dir / "mature_library_evidence.json").write_text(json.dumps({"interpreter": sys.executable, "python_version": sys.version, "lock_digest": _sha256_path(worktree / "uv.lock"), "distributions": distributions, "checks": checks}, indent=2) + "\n", encoding="utf-8")
        return 0
    samples_path = args.result_dir / "samples.ndjson"
    manifest = _gate_manifest(case)
    frozen_manifest_path = args.result_dir / "gate_manifest.json"
    if not frozen_manifest_path.exists():
        raise SystemExit("frozen gate_manifest.json is missing; fail closed rather than re-freezing it")
    frozen_manifest = json.loads(frozen_manifest_path.read_text(encoding="utf-8"))
    if frozen_manifest != manifest:
        raise SystemExit("frozen gate manifest/input contract differs from this harness; fail closed")
    lock_digest = _sha256_bytes((worktree / "uv.lock").read_bytes())
    header = {
        "record_type": "header", "version": 2, "baseline_commit": args.baseline_commit,
        "frozen_manifest_sha256": _sha256_path(frozen_manifest_path),
        "input_sha256": manifest["input_sha256"], "lock_digest": lock_digest,
        "generator_contract": GATE_CONTRACT,
        "harness_digest": _sha256_path(Path(__file__)), "optimized_source_digest": _sha256_path(worktree / "track/v2/intersect2.py"),
        "optimized_dirty_digest": _dirty_digest(worktree), "interpreter": sys.executable,
        "python_version": sys.version, "warmups": args.warmups, "runs": args.runs,
        "alternate": args.alternate, "rss": args.rss,
        "rss_max_ratio": args.assert_rss_max_ratio,
        "rss_acceptance_decision_version": RSS_ACCEPTANCE_DECISION_VERSION,
    }
    if args.resume and args.matrix_only:
        raise SystemExit("--matrix-only cannot resume the formal ledger")
    if args.resume:
        if not samples_path.exists():
            raise SystemExit("--resume requires an existing ordered ledger")
        lines = samples_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise SystemExit("--resume ledger has no immutable header")
        if json.loads(lines[0]) != header:
            raise SystemExit("--resume provenance/parameters/interpreter changed; fail closed")
    elif not args.matrix_only:
        samples_path.write_text(json.dumps(header, sort_keys=True) + "\n", encoding="utf-8")
    request = {**asdict(case), "lock_digest": lock_digest}
    with tempfile.TemporaryDirectory(prefix="perf-dbscan-baseline-") as temp:
        baseline_tree = _archive_baseline(worktree, args.baseline_commit, Path(temp))
        request_baseline = {**request, "source_commit": args.baseline_commit}
        request_optimized = {**request, "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()}
        if args.diagnose_import_only:
            diagnostics = {"baseline": [], "optimized": []}
            for _ in range(args.runs):
                diagnostics["baseline"].append(_run_child(sys.executable, baseline_tree, {**request_baseline, "mode": "import_only"}))
                diagnostics["optimized"].append(_run_child(sys.executable, worktree, {**request_optimized, "mode": "import_only"}))
            (args.result_dir / "import_only.json").write_text(json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8")
            return 0
        if args.phase_diagnostic:
            diagnostic = {
                "method": "Independent phase measurements: grid build and a full neighbor sweep are standalone; DBSCAN and wrapper values are inclusive. Do not add overlapping values.",
                "baseline": _run_child(sys.executable, baseline_tree, {**request_baseline, "mode": "phase_diagnostic"}),
                "optimized": _run_child(sys.executable, worktree, {**request_optimized, "mode": "phase_diagnostic"}),
            }
            (args.result_dir / "phase_diagnostic.json").write_text(json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8")
            return 0
        baseline: list[dict[str, object]] = []
        optimized: list[dict[str, object]] = []
        if args.resume:
            for line in samples_path.read_text(encoding="utf-8").splitlines()[1:]:
                record = json.loads(line)
                (baseline if record["variant"] == "baseline" else optimized).append(record["sample"])
        if len(baseline) != len(optimized):
            raise SystemExit("samples.ndjson has an incomplete pair; remove its final line before --resume")
        if not args.matrix_only:
            for _ in range(args.warmups):
                _run_child(sys.executable, baseline_tree, request_baseline)
                _run_child(sys.executable, worktree, request_optimized)
        for i in range(len(baseline), 0 if args.matrix_only else args.runs):
            order = (("baseline", baseline_tree, request_baseline, baseline), ("optimized", worktree, request_optimized, optimized)) if not args.alternate or i % 2 == 0 else (("optimized", worktree, request_optimized, optimized), ("baseline", baseline_tree, request_baseline, baseline))
            for variant, tree, request_item, bucket in order:
                sample = _run_child(sys.executable, tree, request_item)
                bucket.append(sample)
                with samples_path.open("a", encoding="utf-8") as samples:
                    samples.write(json.dumps({"record_type": "sample", "run": i + 1, "variant": variant, "sample": sample}) + "\n")
    reference_labels = baseline[0]["labels"] if baseline else []
    equivalent = bool(baseline) and all(item["labels"] == reference_labels for item in baseline + optimized)
    def summary(items: list[dict[str, object]]) -> dict[str, object]:
        walls = [float(item["wall_s"]) for item in items]
        rss = [int(item["peak_working_set_size"]) for item in items]
        return {"wall_s": walls, "peak_working_set_size": rss, "median_wall_s": statistics.median(walls), "p10_wall_s": _percentile(walls, .1), "p90_wall_s": _percentile(walls, .9), "median_peak_working_set_size": statistics.median(rss), "children": items}
    current_formal = None
    if not args.matrix_only:
        current_formal = {"header": header, "equivalent": equivalent,
            "baseline": summary(baseline), "optimized": summary(optimized)}
        current_formal["speedup"] = (current_formal["baseline"]["median_wall_s"] /
                                      current_formal["optimized"]["median_wall_s"])
    # Mandatory regression matrix: archive and worktree are compared case by
    # case, including exceptions and their timing.  It is intentionally outside
    # the timed gate loop so each row remains independently inspectable.
    matrix: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="perf-dbscan-matrix-") as temp:
        matrix_baseline_tree = _archive_baseline(worktree, args.baseline_commit, Path(temp))
        selected_cases = [case for case in _cases() if not args.matrix_cases or case.case_id in args.matrix_cases]
        unknown_cases = set(args.matrix_cases or ()) - {case.case_id for case in _cases()}
        if unknown_cases:
            raise SystemExit(f"unknown matrix case(s): {sorted(unknown_cases)}")
        for matrix_case in selected_cases:
            item = {**asdict(matrix_case), "lock_digest": lock_digest}
            # Batch tiny cases in a child so process creation cannot dominate;
            # dense and gate stay single-call to retain their memory shape.
            # Gate is represented by the formal 11-run comparison; never
            # remeasure it in the matrix.  matrix-only requires that evidence.
            if matrix_case.case_id == GATE_CASE_ID:
                if current_formal is not None:
                    formal = current_formal
                else:
                    formal_path = args.result_dir / "comparison.json"
                    if not formal_path.exists():
                        raise SystemExit("gate matrix row requires existing formal comparison.json")
                    formal = json.loads(formal_path.read_text(encoding="utf-8"))
                    # Matrix-only may reuse formal evidence only when the
                    # complete immutable header is byte-for-byte equivalent:
                    # generator contract and every invocation parameter are
                    # provenance, not advisory metadata.
                    previous = formal.get("header", {})
                    if previous != header:
                        raise SystemExit("matrix-only formal comparison provenance differs; fail closed")
                matrix.append({"case_id": GATE_CASE_ID, "reused_formal_11": True,
                               "equivalent": formal["equivalent"], "speedup": formal["speedup"],
                               "baseline": formal["baseline"], "optimized": formal["optimized"]})
                continue
            # Non-empty small scans are below reliable timer/scheduling
            # resolution when measured once.  Use the same fixed batch for
            # every <=256-point microcase; reported values are per call.
            repeats = 100_000 if 0 < len(matrix_case.xs) <= 256 else 1
            def capture(tree: Path, source_commit: str) -> dict[str, object]:
                try:
                    for _ in range(3):
                        _run_child(sys.executable, tree, {**item, "source_commit": source_commit, "repeats": repeats})
                    samples = [_run_child(sys.executable, tree, {**item, "source_commit": source_commit, "repeats": repeats}) for _ in range(11)]
                    return {"ok": True, "samples": samples,
                            "raw_per_call_wall_s": [sample["per_call_wall_s"] for sample in samples],
                            "median_per_call_wall_s": statistics.median(sample["per_call_wall_s"] for sample in samples)}
                except Exception as error:
                    return {"ok": False, "exception_type": type(error).__name__, "message": str(error)}
            base_capture = capture(matrix_baseline_tree, args.baseline_commit)
            opt_capture = capture(worktree, request_optimized["source_commit"])
            equivalent_case = base_capture == opt_capture if not base_capture.get("ok") else all(
                base["labels"] == opt["labels"] for base, opt in zip(base_capture["samples"], opt_capture.get("samples", [])))
            matrix.append({"case_id": matrix_case.case_id, "baseline": base_capture, "optimized": opt_capture,
                           "repeats_per_child": repeats, "equivalent": equivalent_case,
                           "speedup": (base_capture["median_per_call_wall_s"] / opt_capture["median_per_call_wall_s"] if equivalent_case and base_capture.get("ok") else None)})
    matrix_path = args.result_dir / "case_matrix.json"
    matrix_path.write_text(json.dumps({"header": header, "cases": matrix}, indent=2) + "\n", encoding="utf-8")
    if args.matrix_only:
        print(json.dumps({"case_matrix": str(matrix_path), "matrix_only": True,
                          "equivalent": all(row["equivalent"] for row in matrix)}))
        return 0
    result = {"header": header, "manifest": frozen_manifest, "equivalent": equivalent and all(row["equivalent"] for row in matrix), "baseline": current_formal["baseline"], "optimized": current_formal["optimized"], "case_matrix": str(matrix_path)}
    result["speedup"] = current_formal["speedup"]
    result["rss_ratio"] = result["optimized"]["median_peak_working_set_size"] / result["baseline"]["median_peak_working_set_size"]
    result["rss_max_ratio"] = args.assert_rss_max_ratio
    result["rss_acceptance_decision_version"] = RSS_ACCEPTANCE_DECISION_VERSION
    result["rss_pass"] = result["rss_ratio"] <= args.assert_rss_max_ratio
    output = args.result_dir / "comparison.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"comparison": str(output), "speedup": result["speedup"], "rss_ratio": result["rss_ratio"], "rss_max_ratio": args.assert_rss_max_ratio, "equivalent": result["equivalent"]}))
    exempt = {"empty", "invalid_nan", "invalid_eps"}
    regressions = [row["case_id"] for row in matrix if row["case_id"] not in exempt and row["speedup"] is not None and row["speedup"] < .95]
    failures = []
    if args.assert_equivalent and not result["equivalent"]:
        failures.append("equivalence")
    if args.assert_speedup is not None and result["speedup"] < args.assert_speedup:
        failures.append("gate_speedup")
    if result["rss_ratio"] > args.assert_rss_max_ratio:
        failures.append("rss_max_ratio")
    if regressions:
        failures.append("non_gate_regression")
    if failures:
        print(json.dumps({"hard_failures": failures, "non_gate_regressions": regressions,
                          "speedup": result["speedup"], "rss_ratio": result["rss_ratio"]}))
        return 10 + len(failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
