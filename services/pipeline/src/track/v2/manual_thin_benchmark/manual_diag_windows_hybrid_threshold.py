"""Fixed, DB-free threshold diagnostic for bounded cross-track hybrid dispatch."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

from track.v2._geometry import _iter_bounded_segment_intersections

SEED = 20260811
THRESHOLDS = (64, 256, 1024, 4096, 16384)
CAP = 262_144
SHAPES = {"small": (8, 8), "medium": (32, 32), "large": (128, 256)}


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fixture(n: int, m: int) -> tuple[np.ndarray, ...]:
    rng = np.random.default_rng(SEED + n * 1000 + m)
    a1 = rng.normal(size=(n, 2)); b1 = rng.normal(size=(m, 2))
    return a1, a1 + rng.normal(size=(n, 2)), rng.uniform(0, 360, n), b1, b1 + rng.normal(size=(m, 2)), rng.uniform(0, 360, m)


def _run(arrays: tuple[np.ndarray, ...], threshold: int) -> tuple[float, list[tuple[int, float, float]]]:
    started = time.perf_counter()
    output: list[tuple[int, float, float]] = []
    for offset, x, y, flat in _iter_bounded_segment_intersections(*arrays, 60.0, 120.0, max_matrix_elements=CAP, dense_direct_threshold=threshold):
        output.extend((offset + int(i), float(xx), float(yy)) for i, xx, yy in zip(flat, x, y))
    return time.perf_counter() - started, output


def main() -> None:
    results: dict[str, object] = {"version": "WINDOWS-HYBRID-THRESHOLD-1", "seed": SEED, "thresholds": THRESHOLDS, "cap": CAP, "runs": 7, "shapes": {}, "decision_rule": "smallest threshold whose weighted median is within 5 percent of best"}
    scores: dict[int, list[float]] = {item: [] for item in THRESHOLDS}
    for name, shape in SHAPES.items():
        arrays = _fixture(*shape)
        direct = [_run(arrays, CAP) for _ in range(7)]
        broad = [_run(arrays, 0) for _ in range(7)]
        baseline = direct[0][1]
        shape_result: dict[str, object] = {"shape": shape, "direct_median": float(np.median([x[0] for x in direct])), "broad_median": float(np.median([x[0] for x in broad])), "thresholds": {}}
        for threshold in THRESHOLDS:
            samples = [_run(arrays, threshold) for _ in range(7)]
            if any(sample[1] != baseline for sample in samples) or broad[0][1] != baseline:
                raise RuntimeError("strict hit/order/xy equivalence failure")
            raw = [sample[0] for sample in samples]
            median = float(np.median(raw)); scores[threshold].append(median)
            shape_result["thresholds"][str(threshold)] = {"raw_seconds": raw, "median_seconds": median, "output_sha256": _sha(baseline)}
        results["shapes"][name] = shape_result
    weighted = {item: float(np.median(scores[item])) for item in THRESHOLDS}
    best = min(weighted.values())
    chosen = min(item for item, value in weighted.items() if value <= best * 1.05)
    results["weighted_medians_seconds"] = weighted; results["chosen_threshold"] = chosen
    results["source_hashes"] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in ("track/v2/_geometry.py", "track/v2/intersect2.py", __file__)}
    output = Path("generated/perf_windows/manual_diag_windows_hybrid_threshold.json")
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "chosen_threshold": chosen, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
