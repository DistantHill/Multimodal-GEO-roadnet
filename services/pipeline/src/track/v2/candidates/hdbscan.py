"""HDBSCAN with the existing DBSCAN coordinate/label interface."""
from __future__ import annotations

import math
import time
import numpy as np

from ..telemetry.heartbeat import operation_heartbeat


def run_hdbscan(
    xs: list[float], ys: list[float], *, eps_m: float, min_samples: int,
    live_status=None, stage: str = "DBSCAN", perf=None,
) -> list[int]:
    """Map eps_m to cluster_selection_epsilon and return canonical integer labels.

    min_samples includes the point itself and also sets min_cluster_size,
    whose HDBSCAN lower bound is two. Noise labels are always -1.
    """
    if len(xs) != len(ys):
        raise ValueError("HDBSCAN requires matching x/y lengths")
    if not math.isfinite(eps_m) or eps_m < 0:
        raise ValueError("HDBSCAN eps_m must be finite and nonnegative")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ValueError("HDBSCAN min_samples must be a positive integer")
    if len(xs) == 0:
        return []
    coordinates = np.column_stack((xs, ys))
    finite = np.isfinite(coordinates).all(axis=1)
    labels = [-1] * len(xs)
    if np.count_nonzero(finite) < max(2, min_samples):
        return labels
    from sklearn.cluster import HDBSCAN

    started = time.perf_counter()
    with operation_heartbeat(f"HDBSCAN：{stage}"):
        raw_labels = HDBSCAN(
            min_cluster_size=max(2, min_samples), min_samples=min_samples,
            cluster_selection_epsilon=eps_m, allow_single_cluster=True,
            metric="euclidean", n_jobs=1, copy=False,
        ).fit_predict(coordinates[finite])
    canonical = {}
    for index, label in zip(np.flatnonzero(finite), raw_labels):
        if label >= 0:
            labels[int(index)] = canonical.setdefault(int(label), len(canonical))
    if live_status is not None:
        live_status.update(
            detail=f"HDBSCAN：{len(xs):,} 点，{len(canonical):,} 簇",
            stage=f"Detect: HDBSCAN ({stage})", progress=1.0,
            elapsed=time.perf_counter() - started,
        )
    return labels
