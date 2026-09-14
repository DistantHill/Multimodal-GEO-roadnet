"""Isolated import-plus-frozen-input RSS floor diagnostic (no intersection algorithm)."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import median


BASELINE = "c12a06abe3262501f5bb20713c4280aa9a99f34e"
EXPECTED_MANIFEST = "A6AE0F98392ED1F0695588C8BCA1B8443D9068927AAC8D5F369341D4C5261F03"
EXPECTED_INPUT = "176e4df049dd55139602166c1d2a936368c7ce1c19f49448ceee105a17aa6bcc"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def peak_working_set() -> int:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]
    counters = Counters(); counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("Psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


def child(manifest_path: Path) -> dict:
    start = time.perf_counter()
    import track.v2.detect as detect
    import track.v2._geometry as geometry
    from track.v2.models import PointRef, TrajectoryPoint
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if digest(manifest_path).upper() != EXPECTED_MANIFEST:
        raise RuntimeError("floor diagnostic frozen manifest digest mismatch")
    if manifest.get("input_snapshot_sha256") != EXPECTED_INPUT:
        raise RuntimeError("floor diagnostic frozen input digest mismatch")
    fixture = manifest["dense_fixture"]
    if hashlib.sha256(json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != manifest["dense_fixture_sha256"]:
        raise RuntimeError("floor diagnostic dense fixture digest mismatch")
    # Frozen data construction only: deliberately never invokes a candidate/exact function.
    tracks = {}
    for row, segment in enumerate(fixture["segments"]):
        a_lon, a_lat = geometry._to_lonlat(*segment["a"], 116.0, 39.0)
        b_lon, b_lat = geometry._to_lonlat(*segment["b"], 116.0, 39.0)
        vehicle = segment["vehicle"]
        tracks[vehicle] = [
            TrajectoryPoint(PointRef("windows_gate_v1", f"{vehicle}-0", stable=True), vehicle, row * 2.0, a_lon, a_lat, segment["heading"], 10.0),
            TrajectoryPoint(PointRef("windows_gate_v1", f"{vehicle}-1", stable=True), vehicle, row * 2.0 + 1.0, b_lon, b_lat, segment["heading"], 10.0),
        ]
    return {"sys_executable": sys.executable, "python_version": sys.version, "wall_seconds": time.perf_counter() - start,
            "peak_working_set_bytes": peak_working_set(), "source": {"detect": str(Path(detect.__file__).resolve()), "detect_sha256": digest(Path(detect.__file__)), "geometry": str(Path(geometry.__file__).resolve()), "geometry_sha256": digest(Path(geometry.__file__))},
            "frozen_input": {"manifest_sha256": digest(manifest_path).upper(), "input_snapshot_sha256": manifest["input_snapshot_sha256"], "vehicles": len(tracks), "points": sum(map(len, tracks.values())), "algorithm_called": False}}


def archive(worktree: Path, dest: Path) -> None:
    dest.mkdir(); tar = dest / "source.tar"
    with tar.open("wb") as out:
        subprocess.run(["git", "-c", f"safe.directory={worktree}", "archive", BASELINE], cwd=worktree, stdout=out, check=True)
    shutil.unpack_archive(tar, dest, "tar"); tar.unlink()


def run(root: Path, runner: Path, manifest: Path) -> dict:
    env = os.environ.copy(); env["PYTHONPATH"] = str(root)
    cp = subprocess.run([sys.executable, str(runner), "--child", "--manifest", str(manifest)], cwd=root, env=env, capture_output=True, text=True)
    if cp.returncode: raise RuntimeError(cp.stderr + cp.stdout)
    return json.loads(cp.stdout.splitlines()[-1])


def main() -> None:
    p = argparse.ArgumentParser(); p.add_argument("--child", action="store_true"); p.add_argument("--manifest", type=Path); p.add_argument("--runs", type=int, default=11); p.add_argument("--output", type=Path, default=Path("generated/perf_windows/manual_bench_windows_dense_floor_r6.json")); args = p.parse_args()
    if args.child: print(json.dumps(child(args.manifest), sort_keys=True)); return
    worktree = Path.cwd().resolve(); manifest = worktree / "generated/perf_windows/gate_manifest.json"; runner = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix="perf-windows-floor-") as temp:
        baseline = Path(temp) / "baseline"; archive(worktree, baseline)
        samples = {"baseline": [], "optimized": []}
        for run_index in range(args.runs):
            order = (("baseline", baseline), ("optimized", worktree)) if run_index % 2 == 0 else (("optimized", worktree), ("baseline", baseline))
            for side, root in order: samples[side].append(run(root, runner, manifest))
    rss = {side: [s["peak_working_set_bytes"] for s in values] for side, values in samples.items()}
    wall = {side: [s["wall_seconds"] for s in values] for side, values in samples.items()}
    result = {"evidence_version": "PERF-WINDOWS-R7-FLOOR-DIAGNOSTIC", "diagnostic_only": True, "diagnostic": "dense import+frozen-input floor; no algorithm invoked", "runs": args.runs, "alternating": True, "samples": samples, "median_rss": {side: median(values) for side, values in rss.items()}, "rss_ratio": median(rss["optimized"]) / median(rss["baseline"]), "wall_seconds": wall}
    args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rss_ratio": result["rss_ratio"]}, sort_keys=True))


if __name__ == "__main__": main()
