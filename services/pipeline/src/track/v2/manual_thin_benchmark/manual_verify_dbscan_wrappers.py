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
import hashlib, inspect, json, os, sys
from dataclasses import asdict, fields
from pathlib import Path
from track.v2.intersect2 import (
    _cluster_dbscan, _cluster_orthogonal_cross_track_candidates,
)
from track.v2.models import CrossTrackCandidate, PointRef, TurnEvent

origin_lon, origin_lat = 116.0, 39.0
fixed_crs = "transformer" in inspect.signature(_cluster_dbscan).parameters
if fixed_crs:
    from track.v2.models import ProjectionConfig
    from track.v2.projection import CoordinateTransformer, create_spatial_context
    transformer = CoordinateTransformer(
        create_spatial_context(ProjectionConfig(processing_srid=32650))
    )
    base_x, base_y = transformer.forward(origin_lon, origin_lat)
else:
    transformer = None
    base_x = base_y = 0.0

def coordinates(index):
    x, y = base_x + float(index), base_y + float(index)
    if transformer is None:
        return origin_lon, origin_lat, float(index), float(index)
    lon, lat = transformer.inverse(x, y)
    return lon, lat, x, y

turns, cross = [], []
calculate_index_field = fields(TurnEvent)[0].name
cross_fields = {field.name for field in fields(CrossTrackCandidate)}
for i in range(4):
    lon, lat, x, y = coordinates(i)
    turn_kwargs = dict(
        vehicle_id=f"turn-{i}", timestamp=1000.0+i,
        lon=lon, lat=lat, x=x, y=y, in_bearing=0.0, out_bearing=90.0,
        turn_deg=90.0, score=1.0,
        point_ref=PointRef("turn_source", str(i), stable=True),
        source_ids=("turn_source",), from_source_id="turn_source",
        from_source_row_key=str(i), to_source_id="turn_source",
        to_source_row_key=str(i),
    )
    turn_kwargs[calculate_index_field] = i
    turns.append(TurnEvent(**turn_kwargs))
    cross_kwargs = dict(
        point_ref=PointRef("a_source", str(i), stable=True),
        vehicle_id=f"a|b-{i}", timestamp=3000.0+i,
        lon=lon, lat=lat, x=x, y=y, heading=0.0, speed=10.0,
        from_source_id="a_source", from_source_row_key=f"{i}a,{i}b",
        to_source_id="b_source", to_source_row_key=f"{i}c,{i}d",
    )
    if "origin_lon" in cross_fields:
        cross_kwargs.update(origin_lon=origin_lon, origin_lat=origin_lat)
    cross.append(CrossTrackCandidate(**cross_kwargs))

def cluster(fn, items, **kwargs):
    if transformer is None:
        return fn(items, origin_lon, origin_lat, **kwargs)
    return fn(items, transformer, **kwargs)

def cluster_record(cluster):
    record = asdict(cluster)
    cluster_index_field = fields(type(cluster))[0].name
    record["cluster_calculate_index"] = record.pop(cluster_index_field)
    for event in record["events"]:
        event["event_calculate_index"] = event.pop(calculate_index_field)
    return record

value = {
 "turn": [cluster_record(item) for item in cluster(_cluster_dbscan, turns, eps_m=100.0, min_samples=3)],
 "cross_track": [cluster_record(item) for item in cluster(_cluster_orthogonal_cross_track_candidates, cross, eps_m=100.0, min_samples=3)],
}
tree=Path(os.environ["BENCH_TREE"])
source_file=Path(os.environ["BENCH_SOURCE_FILE"])
manifest=Path(os.environ["BENCH_MANIFEST"])
print(json.dumps({"snapshot": value, "source_tree": str(tree),
 "source_digest": hashlib.sha256(source_file.read_bytes()).hexdigest(),
 "manifest_digest": hashlib.sha256(manifest.read_bytes()).hexdigest(),
 "sys_executable": sys.executable, "sys_version": sys.version}, sort_keys=True, separators=(",", ":")))
'''


_SOURCE_ROOT = Path("services/pipeline/src")
_SOURCE_FILE = _SOURCE_ROOT / "track/v2/intersect2.py"
_DEPENDENCY_MANIFEST = Path("services/pipeline/pyproject.toml")


def _archive(worktree: Path, commit: str, temp: Path) -> Path:
    raw = subprocess.run(["git", "-c", f"safe.directory={worktree}", "archive", commit], cwd=worktree, capture_output=True, check=True).stdout
    tar_path = temp / "baseline.tar"
    tar_path.write_bytes(raw)
    with tarfile.open(tar_path) as archive:
        archive.extractall(temp / "baseline", filter="data")
    return temp / "baseline"


def _snapshot(python: str, tree: Path) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tree / _SOURCE_ROOT)
    env["BENCH_TREE"] = str(tree)
    env["BENCH_SOURCE_FILE"] = str(tree / _SOURCE_FILE)
    env["BENCH_MANIFEST"] = str(tree / _DEPENDENCY_MANIFEST)
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
    parser.add_argument(
        "--worktree",
        type=Path,
        help="explicit repository root (avoids requiring git in a slim runtime)",
    )
    parser.add_argument(
        "--baseline-tree",
        type=Path,
        help="pre-exported baseline repository root",
    )
    args = parser.parse_args()
    worktree = (
        args.worktree.resolve()
        if args.worktree is not None
        else Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip())
    )

    def compare(
        baseline: Path,
    ) -> tuple[dict[str, object], dict[str, object], str, str, str]:
        baseline_snapshot = _snapshot(sys.executable, baseline)
        optimized_snapshot = _snapshot(sys.executable, worktree)
        baseline_source_digest = _sha(baseline / _SOURCE_FILE)
        baseline_manifest_digest = _sha(baseline / _DEPENDENCY_MANIFEST)
        baseline_tree = str(baseline.resolve()).casefold()
        return (
            baseline_snapshot,
            optimized_snapshot,
            baseline_source_digest,
            baseline_manifest_digest,
            baseline_tree,
        )

    if args.baseline_tree is not None:
        (
            baseline_snapshot,
            optimized_snapshot,
            baseline_source_digest,
            baseline_manifest_digest,
            baseline_tree,
        ) = compare(args.baseline_tree.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="perf-dbscan-wrapper-") as directory:
            baseline = _archive(worktree, args.baseline_commit, Path(directory))
            (
                baseline_snapshot,
                optimized_snapshot,
                baseline_source_digest,
                baseline_manifest_digest,
                baseline_tree,
            ) = compare(baseline)
    optimized_tree = str(worktree.resolve()).casefold()
    expected_python = str(Path(sys.executable).resolve()).casefold()
    provenance_ok = (
        str(Path(str(baseline_snapshot["sys_executable"])).resolve()).casefold() == expected_python
        and str(Path(str(optimized_snapshot["sys_executable"])).resolve()).casefold() == expected_python
        and baseline_snapshot["manifest_digest"] == baseline_manifest_digest
        and optimized_snapshot["manifest_digest"] == _sha(worktree / _DEPENDENCY_MANIFEST)
        and str(Path(str(baseline_snapshot["source_tree"])).resolve()).casefold() == baseline_tree
        and str(Path(str(optimized_snapshot["source_tree"])).resolve()).casefold() == optimized_tree
        and baseline_snapshot["source_digest"] == baseline_source_digest
        and optimized_snapshot["source_digest"] == _sha(worktree / _SOURCE_FILE)
    )
    equivalent = baseline_snapshot["snapshot"] == optimized_snapshot["snapshot"]
    diff = (
        b"explicit-worktree-no-git\0"
        if args.worktree is not None
        else subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--", str(_SOURCE_FILE)],
            cwd=worktree,
            capture_output=True,
            check=True,
        ).stdout
    )
    result = {"baseline_commit": args.baseline_commit, "parameters": {"eps_m": 100.0, "min_samples": 3,
              "origin_lon": 116.0, "origin_lat": 39.0, "items_per_wrapper": 4},
              "interpreter": sys.executable, "python_version": sys.version,
              "helper_digest": _sha(Path(__file__)),
              "dependency_manifest_digest": _sha(worktree / _DEPENDENCY_MANIFEST),
              "optimized_dirty_digest": hashlib.sha256(diff + (worktree / _SOURCE_FILE).read_bytes()).hexdigest(),
              "baseline": baseline_snapshot, "optimized": optimized_snapshot,
              "provenance_ok": provenance_ok, "equivalent": equivalent}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"equivalent": equivalent, "provenance_ok": provenance_ok, "result": str(args.result)}))
    return 0 if equivalent and provenance_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
