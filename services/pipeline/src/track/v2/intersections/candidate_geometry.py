"""Candidate geometry in the processing CRS (metres), with equal event weights."""
from __future__ import annotations

import numpy as np

from ..exceptions import V2Error


def candidate_circle(xy: np.ndarray) -> tuple[np.ndarray, float]:
    """Return the geometric median and the empirical 95% distance quantile.

    Modified Weiszfeld updates handle iterates coinciding with one or more
    members. Repeated coordinates retain their multiplicity in the objective.
    Stop when the centre moves at most one micrometre; fail explicitly if the
    iteration limit is reached rather than persisting an unconverged centre.

    The radius is the smallest observed distance enclosing at least 95% of
    members (inverse empirical CDF), not an interpolated sample quantile.
    Radius trimming does not remove members from the cluster.
    """
    points = np.asarray(xy, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) == 0:
        raise ValueError("candidate geometry requires a nonempty (n, 2) array")
    if not np.isfinite(points).all():
        raise ValueError("candidate geometry requires finite coordinates")

    # Work relative to a local origin to avoid summing projected UTM offsets.
    origin = np.median(points, axis=0)
    offsets = points - origin
    center = np.zeros(2, dtype=np.float64)
    for _ in range(10_000):
        delta = offsets - center
        distances = np.hypot(delta[:, 0], delta[:, 1])
        nonzero = distances > 0
        coincident = len(points) - np.count_nonzero(nonzero)
        if not np.any(nonzero):
            break
        directions = delta[nonzero] / distances[nonzero, None]
        residual = directions.sum(axis=0)
        residual_norm = float(np.hypot(*residual))
        # Subgradient optimality at a member, including duplicate members.
        if residual_norm <= coincident:
            break
        step = residual / np.sum(1.0 / distances[nonzero])
        step *= 1.0 - coincident / residual_norm
        center += step
        if np.hypot(*step) <= 1e-6:
            break
    else:
        raise V2Error("candidate geometric median did not converge")

    center = origin + center
    distances = np.linalg.norm(points - center, axis=1)
    radius = float(np.quantile(distances, 0.95, method="inverted_cdf"))
    return center, radius
