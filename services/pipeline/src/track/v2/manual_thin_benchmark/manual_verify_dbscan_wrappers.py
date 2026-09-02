"""Archive-to-worktree strict wrapper snapshot gate for PERF-DBSCAN-001."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


_CHILD = r'''
import hashlib, json, sys
from dataclasses import asdict, fields
from pathlib import Path
from track.v2.intersect2 import (
    _cluster_dbscan, _cluster_orthogonal_cross_track_candidates,
)
from track.v2.models import CrossTrackCandidate, PointRef, TurnEvent

origin_lon, origin_lat = 116.0, 39.0
turns = [TurnEvent(i, f"turn-{i}", 1000.0+i, origin_lon, origin_lat,
                   float(i), float(i), 0.0, 90.0, 90.0, 1.0,
                   point_ref=PointRef("turn_source", str(i), stable=True),
                   source_ids=("turn_source",), from_source_id="turn_source",
                   from_source_row_key=str(i), to_source_id="turn_source",
                   to_source_row_key=str(i)) for i in range(4)]
cross = [CrossTrackCandidate(PointRef("a_source", str(i), stable=True), f"a|b-{i}",
                   3000.0+i, origin_lon, origin_lat, float(i), float(i), 0.0, 10.0,
                   "a_source", f"{i}a,{i}b", "b_source", f"{i}c,{i}d", origin_lon, origin_lat)
                   for i in range(4)]

# Canonicalize the transient first field across archived/current models.
calculate_index_field = fields(TurnEvent)[0].name
def cluster_record(cluster):
    record = asdict(cluster)
    for event in record["events"]:
        event["event_calculate_index"] = event.pop(calculate_index_field)
    return record

value = {
 "turn": [cluster_record(cluster) for cluster in _cluster_dbscan(turns, origin_lon, origin_lat, eps_m=100.0, min_samples=3)],
 "cross_track": [cluster_record(cluster) for cluster in _cluster_orthogonal_cross_track_candidates(cross, origin_lon, origin_lat, eps_m=100.0, min_samples=3)],
}
tree=Path.cwd()
print(json.dumps({"snapshot": value, "source_tree": str(tree),
 "source_digest": hashlib.sha256((tree / "track/v2/intersect2.py").read_bytes()).hexdigest(),
 "lock_digest": hashlib.sha256((tree / "uv.lock").read_bytes()).hexdigest(),
 "sys_executable": sys.executable, "sys_version": sys.version}, sort_keys=True, separators=(",", ":")))
'''


def _archive(worktree: Path, commit: str, temp: Path) -> Path:
    raw = subprocess.run(["git", "-c", f"safe.directory={worktree}", "archive", commit], cwd=worktree, capture_output=True, check=True).stdout
    tar_path = temp / "baseline.tar"
    tar_path.write_bytes(raw)
    with tarfile.open(tar_path) as archive:
        archive.extractall(temp / "baseline", filter="data")
    return temp / "baseline"


def _snapshot(python: str, tree: Path) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tree)
    result = subprocess.run([python, "-c", _CHILD], cwd=tree, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.splitlines()[-1])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    worktree = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="perf-dbscan-wrapper-") as directory:
        baseline = _archive(worktree, args.baseline_commit, Path(directory))
        baseline_snapshot = _snapshot(sys.executable, baseline)
        optimized_snapshot = _snapshot(sys.executable, worktree)
        baseline_source_digest = _sha(baseline / "track/v2/intersect2.py")
        baseline_tree = str(baseline.resolve()).casefold()
    optimized_tree = str(worktree.resolve()).casefold()
    expected_python = str(Path(sys.executable).resolve()).casefold()
    provenance_ok = (
        str(Path(str(baseline_snapshot["sys_executable"])).resolve()).casefold() == expected_python
        and str(Path(str(optimized_snapshot["sys_executable"])).resolve()).casefold() == expected_python
        and baseline_snapshot["lock_digest"] == optimized_snapshot["lock_digest"] == _sha(worktree / "uv.lock")
        and str(Path(str(baseline_snapshot["source_tree"])).resolve()).casefold() == baseline_tree
        and str(Path(str(optimized_snapshot["source_tree"])).resolve()).casefold() == optimized_tree
        and baseline_snapshot["source_digest"] == baseline_source_digest
        and optimized_snapshot["source_digest"] == _sha(worktree / "track/v2/intersect2.py")
    )
    equivalent = baseline_snapshot["snapshot"] == optimized_snapshot["snapshot"]
    diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--", "track/v2/intersect2.py"], cwd=worktree, capture_output=True, check=True).stdout
    result = {"baseline_commit": args.baseline_commit, "parameters": {"eps_m": 100.0, "min_samples": 3,
              "origin_lon": 116.0, "origin_lat": 39.0, "items_per_wrapper": 4},
              "interpreter": sys.executable, "python_version": sys.version,
              "helper_digest": _sha(Path(__file__)), "lock_digest": _sha(worktree / "uv.lock"),
              "optimized_dirty_digest": hashlib.sha256(diff + (worktree / "track/v2/intersect2.py").read_bytes()).hexdigest(),
              "baseline": baseline_snapshot, "optimized": optimized_snapshot,
              "provenance_ok": provenance_ok, "equivalent": equivalent}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"equivalent": equivalent, "provenance_ok": provenance_ok, "result": str(args.result)}))
    return 0 if equivalent and provenance_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
